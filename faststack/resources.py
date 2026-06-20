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


def faststack_readme_path() -> Optional[Path]:
    """Return the bundled README.md path across source and frozen layouts.

    In a source/installed checkout the README lives at the repo root (the
    parent of the package directory). In a PyInstaller build it is bundled
    next to the package. Return the first candidate that exists, else None.
    """
    package_dir = faststack_package_dir()
    for candidate in (
        package_dir / "README.md",
        package_dir.parent / "README.md",
    ):
        if candidate.is_file():
            return candidate

    log.warning("FastStack README.md was not found near %s", package_dir)
    return None


def readme_from_metadata() -> Optional[str]:
    """Return the README text embedded in the installed package metadata.

    Non-editable wheel installs do not ship README.md as a file, but setuptools
    embeds the project ``readme`` into the distribution metadata, so this lets
    Help > View Readme still work there. Returns None if unavailable.
    """
    try:
        from importlib.metadata import PackageNotFoundError, metadata
    except ImportError:
        return None

    try:
        meta = metadata("faststack")
    except PackageNotFoundError:
        return None

    payload = meta.get_payload()
    if isinstance(payload, str) and payload.strip():
        return payload

    description = meta.get("Description")
    if description and description.strip():
        return description

    return None


def pyside_qml_dir() -> Optional[Path]:
    """Return the PySide6 Qt QML import directory when it is available."""
    pyside_dir = Path(PySide6.__file__).resolve().parent
    for candidate in (pyside_dir / "Qt" / "qml", pyside_dir / "qml"):
        if candidate.is_dir():
            return candidate

    log.warning("PySide6 QML import directory was not found under %s", pyside_dir)
    return None
