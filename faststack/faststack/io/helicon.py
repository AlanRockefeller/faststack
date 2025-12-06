"""Handles launching Helicon Focus with a list of RAW files."""

import logging
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from faststack.config import config
from faststack.io.executable_validator import validate_executable_path

log = logging.getLogger(__name__)

def launch_helicon_focus(raw_files: List[Path]) -> Tuple[bool, Optional[Path]]:
    """Launches Helicon Focus with the provided list of RAW files.

    Args:
        raw_files: A list of absolute paths to RAW files.

    Returns:
        Tuple of (success: bool, tmp_path: Optional[Path]).
        Returns (True, tmp_path) if launched successfully, (False, None) otherwise.
    """
    helicon_exe = config.get("helicon", "exe")
    if not helicon_exe or not isinstance(helicon_exe, str):
        log.error("Helicon Focus executable path not configured or invalid.")
        return False, None

    # Validate executable path securely
    is_valid, error_msg = validate_executable_path(
        helicon_exe,
        app_type="helicon",
        allow_custom_paths=True
    )
    
    if not is_valid:
        log.error(f"Helicon Focus executable validation failed: {error_msg}")
        return False, None

    if not raw_files:
        log.warning("No RAW files selected to open in Helicon Focus.")
        return False, None

    try:
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", encoding='utf-8') as tmp:
            for f in raw_files:
                # Ensure file path is resolved and exists
                if not f.exists():
                    log.warning(f"RAW file does not exist, skipping: {f}")
                    continue
                tmp.write(f"{f.resolve()}\n")
            tmp_path = Path(tmp.name)

        log.info(f"Temporary file for Helicon Focus: {tmp_path}")
        log.info(f"Input files: {[str(f) for f in raw_files]}")

        # Build command list safely
        args = [helicon_exe, "-i", str(tmp_path.resolve())]
        
        # Parse additional args safely using shlex (handles quotes and escapes properly)
        extra_args = config.get("helicon", "args")
        if extra_args:
            try:
                # Use shlex to properly parse arguments with quotes/escapes
                # On Windows, use posix=False to handle Windows-style paths
                parsed_args = shlex.split(extra_args, posix=(os.name != 'nt'))
                args.extend(parsed_args)
            except ValueError as e:
                log.exception(f"Invalid helicon args format: {e}")
                return False, None

        log.info(f"Launching Helicon Focus with {len(raw_files)} files")
        log.info(f"Command: {' '.join(args)}")
        
        # SECURITY: Explicitly disable shell execution
        subprocess.Popen(
            args,
            shell=False,  # CRITICAL: Never use shell=True with user input
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True  # Close unused file descriptors
        )
        return True, tmp_path
    except (OSError, subprocess.SubprocessError) as e:
        log.exception(f"Failed to launch Helicon Focus: {e}")
        return False, None
    except (IOError, PermissionError) as e:
        log.exception(f"Failed to create temporary file for Helicon Focus: {e}")
        return False, None
