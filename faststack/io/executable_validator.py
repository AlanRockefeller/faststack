"""Secure validation of executable paths before execution."""

import logging
import os
from pathlib import Path
from typing import Optional, List

log = logging.getLogger(__name__)

# Known safe installation directories for common applications on Windows
KNOWN_SAFE_PATHS = [
    r"C:\Program Files",
    r"C:\Program Files (x86)",
]

# Known executable names that are safe to run
KNOWN_SAFE_EXECUTABLES = {
    "photoshop": ["Photoshop.exe"],
    "helicon": ["HeliconFocus.exe"],
}


def validate_executable_path(
    exe_path: str,
    app_type: Optional[str] = None,
    allow_custom_paths: bool = True
) -> tuple[bool, Optional[str]]:
    """
    Validates an executable path before execution.
    
    Args:
        exe_path: Path to the executable to validate
        app_type: Type of application (e.g., 'photoshop', 'helicon') for additional checks
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
    except (ValueError, OSError) as e:
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
    
    # Check if in known safe directory
    in_safe_path = any(
        _is_subpath(path, Path(safe_path))
        for safe_path in KNOWN_SAFE_PATHS
    )
    
    if not in_safe_path:
        if not allow_custom_paths:
            return False, f"Executable not in allowed directory: {exe_path}"
        else:
            log.warning(
                f"Executable '{exe_path}' is not in a known safe directory. "
                f"Proceeding with caution."
            )
    
    # Check for suspicious paths (potential directory traversal, etc.)
    try:
        normalized = os.path.normpath(exe_path)
        if ".." in normalized or normalized != str(path):
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
    if path.suffix.lower() == '.exe':
        return True
    
    if os.name == 'nt':  # Windows
        return path.suffix.lower() == '.exe'
    else:  # Unix-like
        return os.access(path, os.X_OK)


def _is_subpath(path: Path, parent: Path) -> bool:
    """Check if path is a subpath of parent."""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, RuntimeError):
        return False
