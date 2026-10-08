"""Secure validation of executable paths before execution."""

import logging
import os
import stat
from pathlib import Path, PureWindowsPath
from typing import Optional

log = logging.getLogger(__name__)

# Installation locations are trust hints, not guarantees about a program.
# Resolve symlinks and check Unix ownership/permissions before trusting them.
if os.name == "nt":
    KNOWN_SAFE_PATHS = [
        r"C:\Program Files",
        r"C:\Program Files (x86)",
    ]
else:
    KNOWN_SAFE_PATHS = [
        "/bin",
        "/sbin",
        "/usr/bin",
        "/usr/sbin",
        "/usr/local/bin",
        "/usr/local/sbin",
        "/opt",
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / "bin"),
    ]

# Known executable names that are safe to run
KNOWN_SAFE_EXECUTABLES = {
    "helicon": ["HeliconFocus.exe"],
}


def validate_executable_path(
    exe_path: str, app_type: Optional[str] = None, allow_custom_paths: bool = True
) -> tuple[bool, Optional[str]]:
    """
    Validates an executable path before execution.

    Args:
        exe_path: Path to the executable to validate
        app_type: Type of application (e.g., 'image_editor', 'helicon') for additional checks
        allow_custom_paths: Whether to allow executables outside known safe paths

    Returns:
        Tuple of (is_valid, error_message)
        If valid, error_message is None
        If invalid, error_message contains reason
    """
    if not exe_path:
        return False, "Executable path is empty"

    try:
        path = Path(exe_path).resolve()
    except (ValueError, OSError, RuntimeError) as e:
        log.exception(f"Invalid path format: {exe_path}")
        return False, f"Invalid path format: {e}"

    # Check if file exists
    if not path.exists():
        return False, f"Executable not found: {exe_path}"

    if not path.is_file():
        return False, f"Path is not a file: {exe_path}"

    # Check if it's actually an executable
    if not _is_executable(path):
        return False, f"File is not executable: {exe_path}"

    # Check if the executable name matches expected names for the app type
    if app_type and app_type in KNOWN_SAFE_EXECUTABLES:
        expected_names = KNOWN_SAFE_EXECUTABLES[app_type]
        if path.name not in expected_names:
            log.warning(
                f"Executable name '{path.name}' does not match expected names "
                f"for {app_type}: {expected_names}"
            )
            if not allow_custom_paths:
                return False, f"Executable name mismatch: {path.name}"

    # Configured custom builds are valid. Warn about a concrete permission or
    # ownership risk, rather than treating every nonstandard location as unsafe.
    if os.name != "nt":
        risk = _unix_path_risk(Path(os.path.abspath(exe_path)), path)
        if risk:
            if not allow_custom_paths:
                return False, f"Unsafe executable path: {risk}"
            log.warning(
                "Executable '%s' has a potentially unsafe path: %s. "
                "Allowing configured custom path.",
                path,
                risk,
            )

    # Check if in a recognized installation directory.
    in_safe_path = any(
        _is_subpath(path, Path(safe_path)) for safe_path in KNOWN_SAFE_PATHS
    )

    if not in_safe_path:
        if not allow_custom_paths:
            return False, f"Executable not in allowed directory: {exe_path}"
        else:
            log.debug("Allowing executable in custom location: %s", path)

    # Check for suspicious paths (explicit parent traversal segments).
    # Do not reject valid segment names that merely contain ".." (e.g. "v1..2").
    try:
        # Parse Windows-style paths with PureWindowsPath so ".." detection
        # remains correct even when running on non-Windows hosts.
        if "\\" in exe_path or ":" in exe_path:
            parts = PureWindowsPath(exe_path).parts
        else:
            parts = Path(exe_path).parts
        has_parent_traversal = ".." in parts
        if has_parent_traversal:
            log.warning(f"Suspicious path detected: {exe_path}")
            if not allow_custom_paths:
                return False, f"Suspicious path detected: {exe_path}"
    except (ValueError, OSError) as e:
        log.exception("Error normalizing path")
        return False, f"Path validation error: {e}"

    return True, None


def _is_executable(path: Path) -> bool:
    """Check if a file is executable (has .exe extension on Windows)."""
    # Always accept .exe extension (mocked tests might run on Linux)
    if path.suffix.lower() == ".exe":
        return True

    if os.name == "nt":  # Windows
        return path.suffix.lower() == ".exe"
    else:  # Unix-like
        return os.access(path, os.X_OK)


def _unix_path_risk(original_path: Path, resolved_path: Path) -> Optional[str]:
    """Identify replaceable executables, including their symlink locations.

    The current user's private builds are intentional custom installations.
    Root and the current user are trusted owners; group/other write access is
    reported even inside a standard installation directory. These mode checks
    are advisory and do not inspect ACLs or authenticate executable contents.
    """
    trusted_owners = {0, os.getuid()}
    checked: set[Path] = set()
    for path in (original_path, resolved_path):
        for component in (path, *path.parents):
            if component in checked:
                continue
            checked.add(component)
            try:
                info = component.stat()
            except OSError as exc:
                return f"cannot inspect {component}: {exc}"
            if info.st_mode & stat.S_IWOTH:
                return f"{component} is writable by other users"
            if info.st_mode & stat.S_IWGRP:
                return f"{component} is writable by its group"
            if info.st_uid not in trusted_owners:
                return f"{component} is owned by another user (uid {info.st_uid})"
    return None


def _is_subpath(path: Path, parent: Path) -> bool:
    """Check if path is a subpath of parent."""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, RuntimeError):
        return False
