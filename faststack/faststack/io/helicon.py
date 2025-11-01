"""Handles launching Helicon Focus with a list of RAW files."""

import logging
import subprocess
import tempfile
from pathlib import Path
from typing import List

from faststack.config import config

log = logging.getLogger(__name__)

def launch_helicon_focus(raw_files: List[Path]) -> bool:
    """Launches Helicon Focus with the provided list of RAW files.

    Args:
        raw_files: A list of absolute paths to RAW files.

    Returns:
        True if the process was launched successfully, False otherwise.
    """
    helicon_exe = config.get("helicon", "exe")
    if not Path(helicon_exe).is_file():
        log.error(f"Helicon Focus executable not found at: {helicon_exe}")
        # In a real app, this would trigger a dialog to find the exe.
        return False

    if not raw_files:
        log.warning("No RAW files selected to open in Helicon Focus.")
        return False

    try:
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", encoding='utf-8') as tmp:
            for f in raw_files:
                tmp.write(f"{f}\n")
            tmp_path = tmp.name

        args = [helicon_exe, f"-openlist:{tmp_path}"]
        extra_args = config.get("helicon", "args")
        if extra_args:
            args.extend(extra_args.split())

        log.info(f"Launching Helicon Focus with {len(raw_files)} files.")
        log.debug(f"Command: {args}")
        subprocess.Popen(args)
        return True

    except Exception as e:
        log.error(f"Failed to launch Helicon Focus: {e}")
        return False
