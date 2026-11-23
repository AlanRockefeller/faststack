"""Re-render the current JPG from its RAW with automatic white balance (W).

Flash shots often come out of the camera with a colour cast too strong for
the JPG-domain white balance tools to fix. The RAW still has the full colour
information, so this hands it to ``rawtherapee-cli`` with automatic white
balance, writes the result as an 8-bit JPG and puts it in place of the
camera JPG. The old JPG is kept as ``<name>-backup.jpg`` (like any other
FastStack edit), so Ctrl+Z puts it back.

RawTherapee runs on a worker thread; the file swap, the undo entry and the
sidecar update happen on the controller's Qt thread once it finishes, and
only if the JPG is still the file that was developed and nothing else is
writing it.

Mixed into AppController (faststack/app.py); every method runs on the
controller instance.
"""

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np
from PIL import Image, ImageOps
from PySide6.QtCore import Qt, Slot

from faststack.config import config
from faststack.imaging.editor import _safe_replace, create_backup_file
from faststack.imaging.optional_deps import get_cv2
from faststack.io.child_process import child_environment, log_tool_output
from faststack.io.executable_validator import validate_executable_path
from faststack.io.indexer import JPG_EXTENSIONS

log = logging.getLogger(__name__)

# Undo action type recorded for a RAW white balance re-render.
RAW_WHITE_BALANCE_ACTION = "raw_white_balance"

# RawTherapee's [White Balance] Setting. "Auto" is the value every version
# understands: up to 5.8 it is the automatic method, and 5.9+ maps it to its
# default automatic method (temperature correlation). "autold" (RGB grey) and
# "autitcgreen" (temperature correlation) pick one explicitly on 5.9+.
DEFAULT_RAW_AWB_METHOD = "Auto"
DEFAULT_RAW_AWB_JPEG_QUALITY = 95
_RAW_AWB_TIMEOUT_SECONDS = 180

# Below this aspect ratio an image counts as square: no portrait/landscape.
_SQUARE_ASPECT = 1.05
# Rotation matching compares small greyscale copies of the two images.
_MATCH_DECODE_BOX = (256, 256)
_MATCH_COMPARE_SIZE = (48, 48)
# A correlation at least this high is a confident match.
_MATCH_MIN_CORRELATION = 0.5
# A JPG cropped in FastStack is matched as a crop: found inside each turn of
# RawTherapee's full frame. Crops keep the camera's pixel scale, and
# RawTherapee's frame is only a little bigger or smaller than the camera's.
_CROP_MATCH_BOX = (384, 384)
_CROP_MATCH_SCALES = (0.94, 0.97, 1.0, 1.03, 1.06)
_CROP_MATCH_BLUR = 1.5  # Gaussian sigma, in small-copy pixels
# Within this (per side) the JPG is taken to be the whole frame.
_FULL_FRAME_TOLERANCE = 0.06
_CROP_MIN_TEMPLATE_PX = 16
# The best turn must score at least this, and this much above the next one.
_CROP_MIN_CORRELATION = 0.6
_CROP_MIN_MARGIN = 0.1


@dataclass
class RawWhiteBalanceOperation:
    """One RawTherapee auto white balance run for one JPG."""

    operation_id: str
    jpg_key: str
    jpg_path: Path
    raw_path: Path
    jpg_fingerprint: Optional[tuple]
    directory_key: Optional[str]
    sidecar: Any
    process: Optional[subprocess.Popen] = None
    worker: Optional[threading.Thread] = None
    cancel_requested: bool = False
    staged_path: Optional[Path] = None


@dataclass
class RawWhiteBalanceResult:
    operation_id: str
    jpg_key: str
    success: bool
    error: Optional[str] = None
    staged_path: Optional[Path] = None


def build_raw_awb_profile(method: str) -> str:
    """Partial RawTherapee profile that only switches white balance to auto.

    Applied with ``-p`` on top of ``-d`` (the user's default RAW profile), so
    every other setting keeps what RawTherapee would normally use.
    """
    return f"[White Balance]\nEnabled=true\nSetting={method}\n"


def build_raw_awb_command(
    rt_exe: str,
    raw_path: Path,
    output_path: Path,
    profile_path: Path,
    *,
    quality: int,
) -> list[str]:
    """argv for rawtherapee-cli: default profile + auto WB, 8-bit 4:4:4 JPEG."""
    return [
        rt_exe,
        "-o",
        str(output_path),
        f"-j{quality}",
        "-js3",
        "-Y",
        "-d",
        "-p",
        str(profile_path),
        "-c",  # must be last: everything after it is an input file
        str(raw_path),
    ]


def _raw_awb_method() -> str:
    method = str(config.get("rawtherapee", "awb_method") or "").strip()
    # One word, as RawTherapee spells its WB entries; anything else would be
    # injected into the profile file.
    if not method or "\n" in method or "\r" in method or "=" in method:
        return DEFAULT_RAW_AWB_METHOD
    return method


def _raw_awb_jpeg_quality() -> int:
    try:
        quality = int(
            config.get("rawtherapee", "awb_jpeg_quality")
            or DEFAULT_RAW_AWB_JPEG_QUALITY
        )
    except (TypeError, ValueError):
        return DEFAULT_RAW_AWB_JPEG_QUALITY
    return max(1, min(100, quality))


def _small_grey(
    path: Path, *, displayed: bool, box: tuple[int, int] = _MATCH_DECODE_BOX
) -> Image.Image:
    """A small greyscale copy, turned as a viewer shows it when ``displayed``."""
    with Image.open(path) as img:
        img.draft("L", box)  # JPEG: decode at reduced scale
        if displayed:
            img = ImageOps.exif_transpose(img)
        small = img.convert("L")
    small.thumbnail(box)
    return small


def _displayed_size(path: Path) -> tuple[int, int]:
    """Full-resolution (width, height) as a viewer shows the image."""
    with Image.open(path) as img:
        width, height = img.size
        orientation = img.getexif().get(0x0112, 1)
    return (height, width) if orientation in (5, 6, 7, 8) else (width, height)


def _is_whole_frame(size: tuple[int, int], frame: tuple[int, int]) -> bool:
    """Whether ``size`` is ``frame``'s size, either way round, give or take
    the small difference between the camera's frame and RawTherapee's."""
    for width, height in (frame, frame[::-1]):
        if (
            abs(size[0] - width) <= _FULL_FRAME_TOLERANCE * width
            and abs(size[1] - height) <= _FULL_FRAME_TOLERANCE * height
        ):
            return True
    return False


def _crop_match_scores(original_jpg: Path, new_jpg: Path) -> dict[int, float]:
    """For each turn of the new JPG, how well the current JPG matches some
    part of it at the camera's pixel scale (normalised cross-correlation).
    Turns the current JPG cannot fit inside are left out."""
    cv2 = get_cv2()
    if cv2 is None:
        return {}
    ref_w, ref_h = _displayed_size(original_jpg)
    reference = np.asarray(
        _small_grey(original_jpg, displayed=True, box=_CROP_MATCH_BOX), np.float32
    )
    candidate = _small_grey(new_jpg, displayed=True, box=_CROP_MATCH_BOX)
    # Small-copy pixels per full-resolution pixel of the new JPG.
    k = max(candidate.size) / max(_displayed_size(new_jpg))
    # A light blur on both sides: the scale is only known to a few percent,
    # and fine detail that lands a pixel off would otherwise not match.
    blurred = cv2.GaussianBlur(
        np.asarray(candidate, np.float32), (0, 0), _CROP_MATCH_BLUR
    )
    scores: dict[int, float] = {}
    for angle in (0, 90, 180, 270):
        search = np.ascontiguousarray(np.rot90(blurred, angle // 90))
        for scale in _CROP_MATCH_SCALES:
            tw, th = round(ref_w * k * scale), round(ref_h * k * scale)
            if min(tw, th) < _CROP_MIN_TEMPLATE_PX:
                continue
            if tw > search.shape[1] or th > search.shape[0]:
                continue
            template = cv2.resize(reference, (tw, th), interpolation=cv2.INTER_AREA)
            if float(template.std()) < 1.0:
                continue  # featureless: says nothing about which way is up
            template = cv2.GaussianBlur(template, (0, 0), _CROP_MATCH_BLUR)
            result = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
            result = result[np.isfinite(result)]
            if result.size:
                value = min(1.0, float(result.max()))
                scores[angle] = max(scores.get(angle, -1.0), value)
    return scores


def _crop_rotation(original_jpg: Path, new_jpg: Path) -> Optional[int]:
    """The turn that shows the cropped current JPG as part of the new one,
    or None when no turn clearly wins."""
    scores = _crop_match_scores(original_jpg, new_jpg)
    log.debug("RAW white balance crop match scores: %s", scores)
    if not scores:
        return None
    best = max(scores, key=scores.get)
    runner_up = max((v for a, v in scores.items() if a != best), default=-1.0)
    if (
        scores[best] >= _CROP_MIN_CORRELATION
        and scores[best] - runner_up >= _CROP_MIN_MARGIN
    ):
        return best
    return None


def _aspect_class(size: tuple[int, int]) -> Optional[bool]:
    """True portrait, False landscape, None (near) square."""
    width, height = size
    if min(width, height) <= 0:
        return None
    if max(width, height) / min(width, height) < _SQUARE_ASPECT:
        return None
    return height > width


def _correlation(a: Image.Image, b: Image.Image) -> float:
    va = np.asarray(a.resize(_MATCH_COMPARE_SIZE, Image.BILINEAR), np.float64)
    vb = np.asarray(b.resize(_MATCH_COMPARE_SIZE, Image.BILINEAR), np.float64)
    va = va - va.mean()
    vb = vb - vb.mean()
    denom = float(np.sqrt((va * va).sum() * (vb * vb).sum()))
    return float((va * vb).sum() / denom) if denom > 0 else 0.0


# PIL's rotate() angle (counter-clockwise) -> the matching lossless transpose.
_ROTATE_CCW = {
    90: Image.Transpose.ROTATE_90,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_270,
}


def match_output_rotation(original_jpg: Path, new_jpg: Path) -> int:
    """Counter-clockwise turn (0/90/180/270) that makes the new JPG show the
    photo the way the current JPG shows it.

    RawTherapee turns its output by the RAW's orientation tag, which is wrong
    whenever the camera guessed wrong (shooting straight down) or the JPG was
    turned in FastStack afterwards. The current JPG is what the user sees,
    so the new one is matched to it picture against picture. A current JPG
    smaller than the new one's frame (cropped, or a smaller camera JPG) is
    first looked for inside that frame; whole pictures are compared when
    that does not clearly settle it. (Comparing whole pictures first gets a
    crop that kept the frame's shape confidently wrong.) Raises RuntimeError
    when the pictures do not say which way is up and the shapes disagree.
    """
    if not _is_whole_frame(_displayed_size(original_jpg), _displayed_size(new_jpg)):
        angle = _crop_rotation(original_jpg, new_jpg)
        if angle is not None:
            return angle
    reference = _small_grey(original_jpg, displayed=True)
    candidate = _small_grey(new_jpg, displayed=True)
    ref_aspect = _aspect_class(reference.size)
    scores = {}
    for angle in (0, 90, 180, 270):
        turned = candidate.rotate(angle, expand=True) if angle else candidate
        aspect = _aspect_class(turned.size)
        if ref_aspect is not None and aspect is not None and aspect != ref_aspect:
            continue  # a portrait photo never matches a landscape one
        scores[angle] = _correlation(reference, turned)
    log.debug("RAW white balance rotation scores: %s", scores)
    if scores:
        best = max(scores, key=scores.get)
        if scores[best] >= _MATCH_MIN_CORRELATION:
            return best
    # Unsure: keep RawTherapee's turn if its shape fits, else give up.
    if 0 in scores:
        return 0
    raise RuntimeError("could not match the photo's orientation.")


def rotate_jpeg(path: Path, angle: int, *, quality: int) -> None:
    """Turn a JPEG counter-clockwise by ``angle`` in place, keeping metadata."""
    with Image.open(path) as img:
        exif = img.getexif()
        icc = img.info.get("icc_profile")
        turned = ImageOps.exif_transpose(img).transpose(_ROTATE_CCW[angle])
    exif[0x0112] = 1  # the pixels are upright now
    save_kwargs: dict[str, Any] = {
        "quality": quality,
        "subsampling": 0,
        "exif": exif.tobytes(),
    }
    if icc:
        save_kwargs["icc_profile"] = icc
    tmp = path.with_name(path.stem + ".rot" + path.suffix)
    turned.save(tmp, "JPEG", **save_kwargs)
    os.replace(tmp, path)


class RawWhiteBalanceMixin:
    def _init_raw_white_balance(self) -> None:
        self._raw_awb_lock = threading.Lock()
        self._raw_awb_operations: dict[str, RawWhiteBalanceOperation] = {}
        self._rawWhiteBalanceFinished.connect(
            self._on_raw_white_balance_finished,
            Qt.ConnectionType.QueuedConnection,
        )

    # -- Entry point ---------------------------------------------------------

    @Slot()
    def raw_white_balance_to_jpg(self) -> bool:
        """Replace the current JPG with RawTherapee's auto-WB render of its RAW."""
        if getattr(self, "_shutting_down", False) or not self.image_files:
            return False
        if not (0 <= self.current_index < len(self.image_files)):
            return False
        if self._block_auto_adjust_during_crop():
            return False
        if getattr(self, "_is_grid_view_active", False):
            # Never overwrite a photo the user is not looking at.
            self.update_status_message(
                "Open the photo first; W works in the image view."
            )
            return False
        if getattr(self, "view_override_path", None):
            self.update_status_message(
                "W replaces the main JPG; go back to it from the backup/developed view first."
            )
            return False

        image_file = self.image_files[self.current_index]
        jpg_path = Path(image_file.path)
        raw_path = image_file.raw_path
        if (
            raw_path is None
            or jpg_path.suffix.lower() not in JPG_EXTENSIONS
            or self._key(raw_path) == self._key(jpg_path)
        ):
            self.update_status_message(
                "RAW white balance needs a JPG with a matching RAW file."
            )
            return False
        if not Path(raw_path).is_file():
            self.update_status_message(f"RAW file is missing: {Path(raw_path).name}")
            return False
        if not jpg_path.is_file():
            self.update_status_message(f"JPG is missing: {jpg_path.name}")
            return False

        jpg_key = self._key(jpg_path) or str(jpg_path)
        with self._raw_awb_lock:
            if jpg_key in self._raw_awb_operations:
                self.update_status_message(
                    "RAW white balance is already running for this image."
                )
                return False

        if self._raw_awb_target_busy(jpg_path, jpg_key):
            self.update_status_message(
                "This image is still saving. Please wait a moment.", timeout=3000
            )
            return False
        if self._raw_awb_current_session_dirty(jpg_path):
            # The new JPG replaces the pixels those edits were made on, so
            # they would be lost. Leave the choice to the user. Ctrl+S only
            # saves with the editor open; in the loupe it toggles Stacked,
            # and moving to another photo is what saves live edits.
            if self.ui_state.isEditorOpen:
                hint = "Save (Ctrl+S) or undo (Ctrl+Z) the unsaved edits first"
            else:
                hint = (
                    "Save the unsaved edits (move to another photo and back) "
                    "or undo them (Ctrl+Z) first"
                )
            self.update_status_message(f"{hint}, then press W.", timeout=6000)
            return False

        rt_exe = self._resolve_raw_awb_executable()
        if rt_exe is None:
            return False

        operation = RawWhiteBalanceOperation(
            operation_id=uuid.uuid4().hex,
            jpg_key=jpg_key,
            jpg_path=jpg_path,
            raw_path=Path(raw_path),
            jpg_fingerprint=self._file_state_fingerprint(jpg_path),
            directory_key=self._key(self.image_dir),
            sidecar=self.sidecar,
        )
        with self._raw_awb_lock:
            self._raw_awb_operations[jpg_key] = operation

        method = _raw_awb_method()
        quality = _raw_awb_jpeg_quality()
        log.info(
            "Starting RAW auto white balance (%s): %s -> %s",
            method,
            operation.raw_path,
            jpg_path,
        )
        self.update_status_message(
            f"Developing {operation.raw_path.name} with auto white balance...",
            timeout=_RAW_AWB_TIMEOUT_SECONDS * 1000,
        )
        thread = threading.Thread(
            target=self._raw_awb_worker,
            args=(operation, rt_exe, method, quality),
            daemon=True,
            name="RawWhiteBalance",
        )
        operation.worker = thread
        thread.start()
        return True

    # -- Guards ----------------------------------------------------------------

    def _raw_awb_target_busy(self, jpg_path: Path, jpg_key: str) -> bool:
        """True while a FastStack save of this JPG is running or queued."""
        if self._is_image_saving(str(jpg_path)):
            return True
        pending = getattr(self, "_pending_edit_save_requests", {}) or {}
        if jpg_key in pending:
            return True
        for request in pending.values():
            context = request.get("context", {}) if isinstance(request, dict) else {}
            target = context.get("target")
            try:
                if target and self._key(Path(target)) == jpg_key:
                    return True
            except (OSError, TypeError, ValueError):
                continue
        return False

    def _raw_awb_current_session_dirty(self, jpg_path: Path) -> bool:
        """True when the open editor session for this JPG has unsaved edits."""
        if getattr(self, "_auto_adjust_save_pending_action", None) is not None:
            return True
        if not self._is_current_live_edit_session_dirty():
            return False
        editor_path = getattr(self.image_editor, "current_filepath", None)
        if editor_path is None:
            return True
        try:
            return self._key(Path(editor_path)) == self._key(jpg_path)
        except (OSError, TypeError, ValueError):
            return True

    def _resolve_raw_awb_executable(self) -> Optional[str]:
        rt_exe = self._configured_rawtherapee_executable()
        if not rt_exe or not os.path.isfile(rt_exe):
            found = shutil.which("rawtherapee-cli") or shutil.which(
                "rawtherapee-cli.exe"
            )
            if not found:
                self.update_status_message(
                    "rawtherapee-cli not found. Install RawTherapee or set its "
                    "path in Settings."
                )
                log.error("RawTherapee CLI not configured or missing: %r", rt_exe)
                return None
            rt_exe = found
        if Path(rt_exe).stem.lower() == "rawtherapee":
            # The GUI binary is configured; the batch converter sits next to it.
            cli = Path(rt_exe).with_name(f"rawtherapee-cli{Path(rt_exe).suffix}")
            if cli.is_file():
                rt_exe = str(cli)
        valid, error = validate_executable_path(
            rt_exe, app_type="rawtherapee", allow_custom_paths=True
        )
        if not valid:
            self.update_status_message(f"RawTherapee validation failed: {error}")
            log.error("RawTherapee executable rejected: %s", error)
            return None
        return rt_exe

    # -- Worker ----------------------------------------------------------------

    def _raw_awb_worker(
        self,
        operation: RawWhiteBalanceOperation,
        rt_exe: str,
        method: str,
        quality: int,
    ) -> None:
        result = RawWhiteBalanceResult(
            operation.operation_id,
            operation.jpg_key,
            False,
            "RAW white balance did not complete.",
        )
        tmp_dir: Optional[Path] = None
        try:
            tmp_dir = Path(tempfile.mkdtemp(prefix="faststack-raw-awb-"))
            profile_path = tmp_dir / "auto-white-balance.pp3"
            profile_path.write_text(build_raw_awb_profile(method), encoding="utf-8")
            output_path = tmp_dir / "developed.jpg"
            cmd = build_raw_awb_command(
                rt_exe, operation.raw_path, output_path, profile_path, quality=quality
            )
            popen_kwargs: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "env": child_environment(),
            }
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            process = subprocess.Popen(cmd, shell=False, **popen_kwargs)
            with self._raw_awb_lock:
                operation.process = process
                cancelled = operation.cancel_requested
            if cancelled:
                self._stop_raw_process(process)
                raise _Cancelled()
            try:
                stdout, stderr = process.communicate(timeout=_RAW_AWB_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self._stop_raw_process(process)
                raise RuntimeError(
                    f"RawTherapee timed out after {_RAW_AWB_TIMEOUT_SECONDS} seconds."
                ) from None
            tool_name = Path(rt_exe).stem or "rawtherapee-cli"
            log_tool_output(tool_name, stdout)
            log_tool_output(tool_name, stderr)
            if operation.cancel_requested:
                raise _Cancelled()
            if process.returncode != 0:
                detail = (stderr or stdout or "").strip().splitlines()
                raise RuntimeError(
                    f"RawTherapee failed (exit code {process.returncode})"
                    + (f": {detail[-1]}" if detail else ".")
                )
            if not output_path.is_file() or output_path.stat().st_size == 0:
                raise RuntimeError("RawTherapee did not write a JPG.")
            try:
                with Image.open(output_path) as img:
                    img.verify()
            except Exception as exc:
                raise RuntimeError(f"RawTherapee wrote an unreadable JPG: {exc}")
            angle = match_output_rotation(operation.jpg_path, output_path)
            if angle:
                log.info(
                    "Turning RawTherapee's output %d degrees to match %s",
                    angle,
                    operation.jpg_path.name,
                )
                rotate_jpeg(output_path, angle, quality=quality)

            # Stage next to the JPG so the swap is one atomic rename. A hidden
            # *.tmp name is ignored by the folder watcher and the indexer.
            staged = operation.jpg_path.with_name(
                f".{operation.jpg_path.name}.{uuid.uuid4().hex[:8]}.raw-awb.tmp"
            )
            shutil.copyfile(output_path, staged)
            with self._raw_awb_lock:
                operation.staged_path = staged
                cancelled = operation.cancel_requested
            if cancelled:
                staged.unlink(missing_ok=True)
                raise _Cancelled()
            result = RawWhiteBalanceResult(
                operation.operation_id, operation.jpg_key, True, None, staged
            )
        except _Cancelled:
            result.error = "RAW white balance was cancelled."
        except Exception as exc:
            log.exception("RAW auto white balance failed for %s", operation.raw_path)
            result.error = str(exc) or exc.__class__.__name__
        finally:
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            self._rawWhiteBalanceFinished.emit(result)

    # -- Completion (Qt thread) ------------------------------------------------

    def _on_raw_white_balance_finished(self, result: RawWhiteBalanceResult) -> None:
        with self._raw_awb_lock:
            operation = self._raw_awb_operations.get(result.jpg_key)
            if operation is None or operation.operation_id != result.operation_id:
                operation = None
            else:
                self._raw_awb_operations.pop(result.jpg_key, None)
        staged = result.staged_path
        try:
            if operation is None or getattr(self, "_shutting_down", False):
                return
            if not result.success or staged is None:
                self.update_status_message(
                    f"RAW white balance failed: {result.error}", timeout=8000
                )
                return
            error = self._apply_raw_awb_result(operation, staged)
            if error:
                self.update_status_message(
                    f"RAW white balance not applied: {error}", timeout=8000
                )
        finally:
            if staged is not None:
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    log.warning("Could not remove %s", staged, exc_info=True)

    def _apply_raw_awb_result(
        self, operation: RawWhiteBalanceOperation, staged: Path
    ) -> Optional[str]:
        """Swap the developed JPG in. Returns an error message, or None."""
        jpg_path = operation.jpg_path
        if self._file_state_fingerprint(jpg_path) != operation.jpg_fingerprint:
            return f"{jpg_path.name} changed while RawTherapee was running."
        if self._raw_awb_target_busy(jpg_path, operation.jpg_key):
            return f"{jpg_path.name} is being saved."
        on_current = self._current_image_path_key() == operation.jpg_key
        if on_current and self._raw_awb_current_session_dirty(jpg_path):
            return f"{jpg_path.name} was edited while RawTherapee was running."

        try:
            original_stat = jpg_path.stat()
        except OSError as exc:
            return str(exc)
        backup_path = create_backup_file(jpg_path)
        if backup_path is None:
            return f"could not back up {jpg_path.name}."
        self._suppress_watcher_paths(jpg_path, backup_path)
        try:
            _safe_replace(staged, jpg_path)
        except OSError as exc:
            log.exception("Could not replace %s", jpg_path)
            try:
                backup_path.unlink(missing_ok=True)
            except OSError:
                pass
            return f"could not replace {jpg_path.name}: {exc}"
        # Keep the capture-time file date, as FastStack's own saves do, so a
        # folder sorted by date does not move the photo.
        try:
            os.utime(jpg_path, (original_stat.st_atime, original_stat.st_mtime))
        except OSError:
            log.warning("Could not restore timestamps on %s", jpg_path)
        log.info(
            "RAW auto white balance replaced %s (backup %s)", jpg_path, backup_path
        )

        # Deferred saves or a crash-recovery edit state for the old pixels
        # must never land on the new JPG.
        try:
            self._retire_pending_saves_for_photoshop(jpg_path, jpg_path)
        except Exception:
            log.exception("Could not retire pending saves for %s", jpg_path)

        sidecar = operation.sidecar or self.sidecar
        metadata_before = self._mark_image_edited_in_sidecar(
            sidecar,
            jpg_path,
            clear_saved_edit_state=True,
            background=True,
        )
        if operation.directory_key == self._key(self.image_dir):
            self.undo_history.append(
                (
                    RAW_WHITE_BALANCE_ACTION,
                    self._build_edit_undo_data(
                        jpg_path,
                        backup_path,
                        metadata_path=jpg_path,
                        metadata_before=metadata_before,
                        sidecar=sidecar,
                    ),
                    time.time(),
                )
            )

        self._refresh_after_raw_awb(jpg_path, on_current=on_current)
        self._auto_add_edited_to_batch_if_enabled(jpg_path)
        self.update_status_message(
            f"Auto white balance from {operation.raw_path.name} applied "
            "(Ctrl+Z to undo)."
        )
        return None

    def _current_image_path_key(self) -> Optional[str]:
        if not (0 <= self.current_index < len(self.image_files)):
            return None
        return self._key(self.image_files[self.current_index].path)

    def _refresh_after_raw_awb(self, jpg_path: Path, *, on_current: bool) -> None:
        """Show the new pixels; mirrors the background save completion."""
        # Cleared even when the user has moved on: the swap keeps the file's
        # mtime, so a session still holding the old pixels would otherwise
        # be reused as if it were current.
        if self.image_editor is not None:
            editor_path = getattr(self.image_editor, "current_filepath", None)
            try:
                editor_on_jpg = editor_path is not None and self._key(
                    Path(editor_path)
                ) == self._key(jpg_path)
            except (OSError, TypeError, ValueError):
                editor_on_jpg = False
            if editor_on_jpg:
                # The editor holds the old pixels; the next edit must start
                # from the new file.
                if self.ui_state.isEditorOpen:
                    self.ui_state.isEditorOpen = False
                self._clear_active_auto_adjust_state(
                    "RAW white balance replaced the image", clear_editor=False
                )
                self.image_editor.clear()
                self._clear_live_edit_session_state()
                with self._preview_lock:
                    self._clear_last_rendered_preview_locked()

        preserved_path = self._current_image_path()
        self._begin_direct_image_transition("RAW white balance replaced a JPG")
        self.refresh_image_list()
        if preserved_path is not None:
            self._reindex_after_save(preserved_path)
        self._invalidate_decoded_path(jpg_path, force=True)
        if on_current:
            self.prefetcher.update_prefetch(self.current_index)
            self.sync_ui_state()
            if self.ui_state.isHistogramVisible:
                self.update_histogram()
        else:
            self._do_prefetch(self.current_index)
        self._restart_quality_decode_timer()
        if self.ui_state:
            self.ui_state.variantBadgesChanged.emit()

    # -- Shutdown --------------------------------------------------------------

    def _cancel_raw_white_balance_operations(self) -> None:
        with self._raw_awb_lock:
            operations = list(self._raw_awb_operations.values())
            self._raw_awb_operations.clear()
            for operation in operations:
                operation.cancel_requested = True
        for operation in operations:
            if operation.process is not None:
                self._stop_raw_process(operation.process)
        current = threading.current_thread()
        for operation in operations:
            worker = operation.worker
            if worker is not None and worker is not current:
                worker.join(timeout=0.5)
            if operation.staged_path is not None:
                try:
                    operation.staged_path.unlink(missing_ok=True)
                except OSError:
                    pass


class _Cancelled(Exception):
    pass
