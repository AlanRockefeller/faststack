"""Runtime resource lookup helpers."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import PySide6

log = logging.getLogger(__name__)


def faststack_package_dir() -> Path:
    """Return the package directory in source, installed, or frozen layouts."""
    if getattr(sys, "frozen", False):
        bundle_root = Path(getattr(sys, "_MEIPASS", ""))
        bundled_package = bundle_root / "faststack"
        if bundled_package.is_dir():
            return bundled_package

    return Path(__file__).resolve().parent


def faststack_qml_dir() -> Path:
    """Return the directory containing FastStack QML files."""
    qml_dir = faststack_package_dir() / "qml"
    if not qml_dir.is_dir():
        raise FileNotFoundError(f"FastStack QML directory not found: {qml_dir}")
    return qml_dir


def pyside_qml_dir() -> Optional[Path]:
    """Return the PySide6 Qt QML import directory when it is available."""
    pyside_dir = Path(PySide6.__file__).resolve().parent
    for candidate in (pyside_dir / "Qt" / "qml", pyside_dir / "qml"):
        if candidate.is_dir():
            return candidate

    log.warning("PySide6 QML import directory was not found under %s", pyside_dir)
    return None
