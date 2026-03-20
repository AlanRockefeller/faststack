"""TurboJPEG discovery helpers with Windows DLL fallbacks."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger(__name__)

try:
    from turbojpeg import TurboJPEG, TJPF_RGB
except ImportError:  # pragma: no cover - exercised via create_turbojpeg
    TurboJPEG = None
    TJPF_RGB = None


def _candidate_library_paths() -> list[Optional[str]]:
    """Return candidate libjpeg-turbo library paths to try in priority order."""
    candidates: list[Optional[str]] = []

    explicit = os.getenv("FASTSTACK_TURBOJPEG_LIB") or os.getenv("TURBOJPEG_LIB")
    if explicit:
        candidates.append(explicit)
    candidates.append(None)

    if os.name == "nt":
        common_roots = [
            os.getenv("FASTSTACK_TURBOJPEG_ROOT"),
            os.getenv("ProgramFiles"),
            os.getenv("ProgramFiles(x86)"),
        ]
        suffixes = [
            ("libjpeg-turbo", "bin", "turbojpeg.dll"),
            ("libjpeg-turbo64", "bin", "turbojpeg.dll"),
            ("libjpeg-turbo-gcc64", "bin", "turbojpeg.dll"),
            ("TurboJPEG", "bin", "turbojpeg.dll"),
            ("bin", "turbojpeg.dll"),
        ]
        for root in common_roots:
            if not root:
                continue
            for suffix in suffixes:
                candidates.append(str(Path(root).joinpath(*suffix)))

        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            candidates.append(
                str(
                    Path(local_app_data)
                    / "Programs"
                    / "libjpeg-turbo"
                    / "bin"
                    / "turbojpeg.dll"
                )
            )

        for path_dir in os.getenv("PATH", "").split(os.pathsep):
            if path_dir:
                candidates.append(str(Path(path_dir) / "turbojpeg.dll"))

    unique: list[Optional[str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = "__default__" if candidate is None else os.path.normcase(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def create_turbojpeg() -> Tuple[Optional["TurboJPEG"], bool]:
    """Create a TurboJPEG decoder if possible."""
    if TurboJPEG is None:
        log.warning("PyTurboJPEG not found. Falling back to Pillow for JPEG decoding.")
        return None, False

    failures: list[str] = []
    for candidate in _candidate_library_paths():
        try:
            decoder = TurboJPEG() if candidate is None else TurboJPEG(candidate)
        except Exception as exc:
            source = "default loader" if candidate is None else candidate
            failures.append(f"{source}: {exc}")
            continue

        if candidate is None:
            log.info("PyTurboJPEG is available. Using it for JPEG decoding.")
        else:
            log.info("Loaded TurboJPEG library from %s", candidate)
        return decoder, True

    for failure in failures:
        log.warning("TurboJPEG load attempt failed: %s", failure)
    log.warning(
        "TurboJPEG initialization failed for all attempted locations. "
        "Falling back to Pillow."
    )
    return None, False
