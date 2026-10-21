"""Order restack inputs from near focus to far focus using EXIF metadata."""

import json
import logging
import math
import os
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from faststack.io.executable_validator import validate_executable_path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FocusPosition:
    """Focus metadata used to place one frame along the focus axis."""

    path: Path
    step: Optional[float] = None
    near_step: Optional[float] = None
    infinity_step: Optional[float] = None
    distance_m: Optional[float] = None


def _path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _number(record: dict[str, Any], *names: str) -> Optional[float]:
    for name in names:
        value = record.get(name)
        if value is None or isinstance(value, bool):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _ifd_entries(
    data: bytes, offset: int, byte_order: str
) -> list[tuple[int, int, int, bytes]]:
    """Read the bounded subset of TIFF IFD fields needed for OM MakerNotes."""
    endian = "<" if byte_order == "II" else ">"
    if offset < 0 or offset + 2 > len(data):
        return []
    count = struct.unpack_from(f"{endian}H", data, offset)[0]
    if count > 4096 or offset + 2 + count * 12 > len(data):
        return []
    entries = []
    for index in range(count):
        entry_offset = offset + 2 + index * 12
        tag, field_type, item_count = struct.unpack_from(
            f"{endian}HHI", data, entry_offset
        )
        entries.append(
            (tag, field_type, item_count, data[entry_offset + 8 : entry_offset + 12])
        )
    return entries


def _inline_short(value: bytes, field_type: int, byte_order: str) -> float:
    endian = "<" if byte_order == "II" else ">"
    signed = field_type == 8  # TIFF SSHORT; Olympus uses it for focus steps.
    code = "h" if signed else "H"
    return float(struct.unpack_from(f"{endian}{code}", value)[0])


def _inline_uint(value: bytes, byte_order: str) -> int:
    endian = "<" if byte_order == "II" else ">"
    return int(struct.unpack(f"{endian}I", value)[0])


def _read_om_system_orf_focus(path: Path) -> Optional[FocusPosition]:
    """Read OM SYSTEM's nested FocusInfo IFD without an external dependency."""
    if path.suffix.casefold() != ".orf":
        return None
    try:
        with path.open("rb") as raw_file:
            # OM-1 MakerNotes and FocusInfo live near the start of the ORF.
            data = raw_file.read(1024 * 1024)
    except OSError:
        return None

    marker = b"OM SYSTEM\0\0\0"
    maker_base = data.find(marker)
    if maker_base < 0:
        return None
    header = maker_base + len(marker)
    byte_order_raw = data[header : header + 2]
    if byte_order_raw not in (b"II", b"MM"):
        return None
    byte_order = byte_order_raw.decode("ascii")

    # OM SYSTEM's MakerNote header is byte-order + 0x0004, immediately
    # followed by its root IFD. Tag 0x2050 points to the FocusInfo IFD using
    # an offset relative to the MakerNote start.
    root_ifd = header + 4
    focus_ifd = None
    for tag, _field_type, item_count, value in _ifd_entries(
        data, root_ifd, byte_order
    ):
        if tag == 0x2050 and item_count == 1:
            focus_ifd = maker_base + _inline_uint(value, byte_order)
            break
    if focus_ifd is None:
        return None

    values: dict[int, float] = {}
    for tag, field_type, item_count, value in _ifd_entries(
        data, focus_ifd, byte_order
    ):
        if tag in (0x0301, 0x0303, 0x0304) and item_count == 1:
            if field_type in (3, 8):  # SHORT / SSHORT
                values[tag] = _inline_short(value, field_type, byte_order)

    step = values.get(0x0301)
    near_step = values.get(0x0304)
    infinity_step = values.get(0x0303)
    if step is None:
        return None
    return FocusPosition(
        path=path,
        step=step,
        near_step=near_step,
        infinity_step=infinity_step,
    )


def _read_focus_with_exiftool(paths: list[Path]) -> list[FocusPosition]:
    executable = shutil.which("exiftool") or shutil.which("exiftool.exe")
    if not executable:
        return []
    is_valid, error = validate_executable_path(executable, allow_custom_paths=True)
    if not is_valid:
        log.info("ExifTool is unavailable for Restack focus ordering: %s", error)
        return []

    args = [
        executable,
        "-json",
        "-n",
        "-FocusStepCount",
        "-FocusStepInfinity",
        "-FocusStepNear",
        "-FocusDistance",
        "-FocusDistance2",
        "-SubjectDistance",
        "--",
        *(str(path) for path in paths),
    ]
    try:
        process = subprocess.Popen(
            args,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            log.warning("ExifTool timed out while ordering Restack inputs.")
            return []
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("Could not run ExifTool for Restack focus ordering: %s", exc)
        return []
    if process.returncode != 0:
        log.info(
            "ExifTool could not read Restack focus metadata: %s", stderr.strip()
        )
        return []

    try:
        raw_records = json.loads(stdout)
    except (TypeError, ValueError):
        log.info("ExifTool returned invalid JSON for Restack focus ordering.")
        return []
    if not isinstance(raw_records, list):
        return []

    by_path = {
        _path_key(record["SourceFile"]): record
        for record in raw_records
        if isinstance(record, dict) and isinstance(record.get("SourceFile"), str)
    }
    positions = []
    for path in paths:
        record = by_path.get(_path_key(path))
        if record is None:
            return []
        distance = _number(record, "FocusDistance", "FocusDistance2", "SubjectDistance")
        # Olympus uses zero as its infinity sentinel. A zero standard EXIF
        # SubjectDistance can instead mean "unknown", so only promote zero
        # when the same MakerNote exposes an infinity focus-step reference.
        if distance is not None and distance <= 0:
            distance = (
                math.inf if _number(record, "FocusStepInfinity") is not None else None
            )
        positions.append(
            FocusPosition(
                path=path,
                step=_number(record, "FocusStepCount"),
                near_step=_number(record, "FocusStepNear"),
                infinity_step=_number(record, "FocusStepInfinity"),
                distance_m=distance,
            )
        )
    return positions


def _step_sort_reverse(positions: list[FocusPosition]) -> Optional[bool]:
    direction_votes = []
    for position in positions:
        near = position.near_step
        infinity = position.infinity_step
        if near is not None and infinity is not None and near != infinity:
            direction_votes.append(near > infinity)
    if direction_votes and all(vote == direction_votes[0] for vote in direction_votes):
        # True means the larger motor count is nearer, hence descending order.
        return direction_votes[0]

    pairs = [
        (position.step, position.distance_m)
        for position in positions
        if position.step is not None
        and position.distance_m is not None
        and math.isfinite(position.distance_m)
        and position.distance_m > 0
    ]
    if len(pairs) < 2 or len({distance for _, distance in pairs}) < 2:
        return None
    mean_step = sum(step for step, _ in pairs) / len(pairs)
    mean_log_distance = sum(math.log(distance) for _, distance in pairs) / len(pairs)
    covariance = sum(
        (step - mean_step) * (math.log(distance) - mean_log_distance)
        for step, distance in pairs
    )
    if covariance == 0:
        return None
    # Negative covariance means motor counts decrease toward infinity.
    return covariance < 0


def _order_from_positions(
    positions: list[FocusPosition],
) -> tuple[list[Path], Optional[str]]:
    if len(positions) < 2:
        return [position.path for position in positions], None

    if all(position.step is not None for position in positions):
        reverse = _step_sort_reverse(positions)
        steps = {position.step for position in positions}
        if reverse is not None and len(steps) > 1:
            ordered = sorted(
                positions,
                key=lambda position: float(position.step),
                reverse=reverse,
            )
            direction = "descending" if reverse else "ascending"
            return [position.path for position in ordered], (
                f"EXIF focus-step order ({direction}, near to far)"
            )

    if all(position.distance_m is not None for position in positions):
        distances = {position.distance_m for position in positions}
        if len(distances) > 1:
            ordered = sorted(positions, key=lambda position: position.distance_m)
            return [position.path for position in ordered], (
                "EXIF focus-distance order (near to far)"
            )

    return [position.path for position in positions], None


def order_focus_stack_inputs(
    input_files: list[Path],
) -> tuple[list[Path], Optional[str]]:
    """Return Restack inputs in near-to-far focus order when EXIF permits it.

    The result is deliberately conservative: every frame must have comparable
    metadata and the focus-step direction must be known. Otherwise the caller's
    deterministic filename order is returned unchanged.
    """
    paths = list(input_files)
    if len(paths) < 2:
        return paths, None

    om_positions = [_read_om_system_orf_focus(path) for path in paths]
    if all(position is not None for position in om_positions):
        ordered, method = _order_from_positions(
            [position for position in om_positions if position is not None]
        )
        if method is not None:
            return ordered, f"OM SYSTEM {method}"

    exiftool_positions = _read_focus_with_exiftool(paths)
    if len(exiftool_positions) == len(paths):
        return _order_from_positions(exiftool_positions)

    return paths, None
