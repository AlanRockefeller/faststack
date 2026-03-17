"""Helpers for optional libjpeg-turbo discovery and initialization."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger(__name__)

try:
    from turbojpeg import TurboJPEG, TJPF_RGB
except ImportError:
    TurboJPEG = None
    TJPF_RGB = None


def _candidate_library_paths() -> list[Path]:
    """Return likely native library locations for libjpeg-turbo on Windows."""
    candidates: list[Path] = []

    for env_name in ("FASTSTACK_TURBOJPEG_LIB", "TURBOJPEG_LIB"):
        value = os.getenv(env_name)
        if value:
            candidates.append(Path(value))

    if os.name == "nt":
        candidates.extend(
            [
                Path(r"C:\libjpeg-turbo64\bin\turbojpeg.dll"),
                Path(r"C:\libjpeg-turbo\bin\turbojpeg.dll"),
                Path(r"C:\Program Files\libjpeg-turbo\bin\turbojpeg.dll"),
                Path(r"C:\Program Files\libjpeg-turbo64\bin\turbojpeg.dll"),
                Path(r"C:\Program Files (x86)\libjpeg-turbo\bin\turbojpeg.dll"),
                Path(r"C:\Program Files (x86)\libjpeg-turbo64\bin\turbojpeg.dll"),
            ]
        )

        for path_dir in os.getenv("PATH", "").split(os.pathsep):
            if path_dir:
                candidates.append(Path(path_dir) / "turbojpeg.dll")

    # Preserve order while removing duplicates.
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate))
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def create_turbojpeg() -> Tuple[Optional["TurboJPEG"], bool]:
    """Create a TurboJPEG decoder if both wrapper and native library exist."""
    if TurboJPEG is None:
        log.debug("PyTurboJPEG Python package not available")
        return None, False

    try:
        return TurboJPEG(), True
    except Exception:
        log.debug("TurboJPEG auto-discovery failed; trying explicit library paths")

    for candidate in _candidate_library_paths():
        if not candidate.exists():
            continue
        try:
            decoder = TurboJPEG(lib_path=str(candidate))
            log.info("Using libjpeg-turbo at %s", candidate)
            return decoder, True
        except Exception:
            log.debug("Failed to initialize turbojpeg from %s", candidate)

    return None, False
