"""Handles launching Helicon Focus with a list of RAW files."""

import logging
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from faststack.config import config
from faststack.io.executable_validator import validate_executable_path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HeliconLaunch:
    """A Helicon child and the manifest owned by that exact launch."""

    process: subprocess.Popen
    manifest_path: Path


def launch_helicon_focus(raw_files: List[Path]) -> Tuple[bool, Optional[HeliconLaunch]]:
    """Launches Helicon Focus with the provided list of RAW files.

    Args:
        raw_files: A list of absolute paths to RAW files.

    Returns:
        Tuple of (success, launch). On success the caller owns the launch record
        and must retain its manifest until that child exits.
    """
    helicon_exe = config.get("helicon", "exe")
    if not helicon_exe or not isinstance(helicon_exe, str):
        log.error("Helicon Focus executable path not configured or invalid.")
        return False, None

    # Validate executable path securely
    is_valid, error_msg = validate_executable_path(
        helicon_exe, app_type="helicon", allow_custom_paths=True
    )

    if not is_valid:
        log.error(f"Helicon Focus executable validation failed: {error_msg}")
        return False, None

    if not raw_files:
        log.warning("No RAW files selected to open in Helicon Focus.")
        return False, None

    validated_files = []
    for input_path in raw_files:
        try:
            resolved = input_path.resolve(strict=True)
            if not resolved.is_file() or not os.access(resolved, os.R_OK):
                raise OSError("not a readable regular file")
            with resolved.open("rb") as input_file:
                input_file.read(1)
            validated_files.append(resolved)
        except OSError as exc:
            log.warning("Helicon input is unavailable (%s): %s", input_path, exc)
            return False, None

    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", delete=False, suffix=".txt", encoding="utf-8"
        ) as tmp:
            for input_path in validated_files:
                tmp.write(f"{input_path}\n")
            tmp_path = Path(tmp.name)

        log.info(f"Temporary file for Helicon Focus: {tmp_path}")
        log.info(f"Input files: {[str(f) for f in validated_files]}")

        # Build command list safely
        args = [helicon_exe, "-i", str(tmp_path.resolve())]

        # Parse additional args safely using shlex (handles quotes and escapes properly)
        extra_args = config.get("helicon", "args")
        if extra_args:
            try:
                # Use shlex to properly parse arguments with quotes/escapes
                # On Windows, use posix=False to handle Windows-style paths
                parsed_args = shlex.split(extra_args, posix=(os.name != "nt"))
                args.extend(parsed_args)
            except ValueError as e:
                log.exception(f"Invalid helicon args format: {e}")
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)
                return False, None

        log.info(f"Launching Helicon Focus with {len(validated_files)} files")
        log.info(f"Command: {' '.join(args)}")

        # SECURITY: Explicitly disable shell execution
        process = subprocess.Popen(
            args,
            shell=False,  # CRITICAL: Never use shell=True with user input
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,  # Close unused file descriptors
        )
        return True, HeliconLaunch(process=process, manifest_path=tmp_path)
    except (OSError, subprocess.SubprocessError) as e:
        log.exception(f"Failed to launch Helicon Focus: {e}")
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        return False, None
