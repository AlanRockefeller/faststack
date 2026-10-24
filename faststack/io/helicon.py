"""Launch the configured focus stacker with a list of source images."""

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from faststack.config import config
from faststack.io.arguments import parse_external_arguments
from faststack.io.executable_validator import validate_executable_path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FocusStackerLaunch:
    """A focus-stacker child and an optional manifest owned by that launch."""

    process: subprocess.Popen
    manifest_path: Optional[Path] = None


# Compatibility for callers/tests using the original Helicon-specific name.
HeliconLaunch = FocusStackerLaunch


def _openfocus_source_command(target: Path, input_files: list[Path]) -> list[str]:
    """Build a command for an OpenFocus source checkout.

    Calling the interpreter directly is equivalent to activating its virtual
    environment first, and works when FastStack was launched outside a shell.
    """
    project_dir = target if target.is_dir() else target.parent
    script = project_dir / "main.py" if target.is_dir() else target
    if script.name.casefold() != "main.py" or not script.is_file():
        return []

    interpreter_candidates = [
        project_dir / ".venv" / "bin" / "python",
        project_dir / ".venv" / "Scripts" / "python.exe",
        project_dir / "venv" / "bin" / "python",
        project_dir / "venv" / "Scripts" / "python.exe",
    ]
    active_venv = os.environ.get("VIRTUAL_ENV", "").strip()
    if active_venv:
        interpreter_candidates.extend(
            [
                Path(active_venv) / "bin" / "python",
                Path(active_venv) / "Scripts" / "python.exe",
            ]
        )
    if not getattr(sys, "frozen", False):
        interpreter_candidates.append(Path(sys.executable))
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            interpreter_candidates.append(Path(found))

    for interpreter in interpreter_candidates:
        if interpreter.is_file() and os.access(interpreter, os.X_OK):
            return [
                str(interpreter),
                str(script.resolve()),
                *(str(path) for path in input_files),
            ]
    return []


def _is_openfocus_target(target: Path) -> bool:
    if target.is_dir():
        return (target / "main.py").is_file()
    return target.name.casefold() == "main.py" or "openfocus" in target.stem.casefold()


def validate_focus_stacker_path(path: str) -> Tuple[bool, str]:
    """Validate an OpenFocus source checkout/executable or Helicon executable."""
    if not path or not isinstance(path, str):
        return False, "Focus stacker path is not configured."

    configured = os.path.expanduser(os.path.expandvars(path.strip().strip('"')))
    target = Path(configured).absolute()
    if _is_openfocus_target(target):
        if target.is_dir() or target.name.casefold() == "main.py":
            if _openfocus_source_command(target, []):
                return True, ""
            return (
                False,
                "OpenFocus source needs main.py and a usable Python interpreter.",
            )
        return validate_executable_path(configured, allow_custom_paths=True)

    return validate_executable_path(
        configured, app_type="helicon", allow_custom_paths=True
    )


def launch_focus_stacker(
    input_files: List[Path],
) -> Tuple[bool, Optional[FocusStackerLaunch]]:
    """Launch OpenFocus or Helicon Focus with the provided source images."""
    configured = config.get("helicon", "exe")
    if not configured or not isinstance(configured, str):
        log.error("Focus stacker path is not configured or invalid.")
        return False, None

    configured = os.path.expanduser(os.path.expandvars(configured.strip().strip('"')))
    # Popen resolves argv[0] relative to ``cwd``. Keep this absolute before
    # changing the child directory, while leaving interpreter symlinks intact.
    target = Path(configured).absolute()

    if not input_files:
        log.warning("No files selected to open in the focus stacker.")
        return False, None

    validated_files = []
    for input_path in input_files:
        try:
            resolved = input_path.resolve(strict=True)
            if not resolved.is_file() or not os.access(resolved, os.R_OK):
                raise OSError("not a readable regular file")
            with resolved.open("rb") as input_file:
                input_file.read(1)
            validated_files.append(resolved)
        except OSError as exc:
            log.warning("Focus stacker input is unavailable (%s): %s", input_path, exc)
            return False, None

    if _is_openfocus_target(target):
        if target.is_dir() or target.name.casefold() == "main.py":
            args = _openfocus_source_command(target, validated_files)
            working_directory = target if target.is_dir() else target.parent
            if not args:
                log.error(
                    "No Python interpreter was found for OpenFocus source at %s. "
                    "Create .venv in that folder or launch FastStack from the "
                    "environment used by OpenFocus.",
                    target,
                )
                return False, None
        else:
            is_valid, error_msg = validate_executable_path(
                configured, allow_custom_paths=True
            )
            if not is_valid:
                log.error("OpenFocus executable validation failed: %s", error_msg)
                return False, None
            args = [str(target), *(str(path) for path in validated_files)]
            working_directory = target.parent

        log.info("Launching OpenFocus with %d files: %s", len(validated_files), args)
        try:
            process = subprocess.Popen(
                args,
                cwd=str(working_directory),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.exception("Failed to launch OpenFocus: %s", exc)
            return False, None
        return True, FocusStackerLaunch(process=process)

    # Anything other than OpenFocus retains Helicon's manifest protocol.
    is_valid, error_msg = validate_focus_stacker_path(configured)
    if not is_valid:
        log.error("Helicon Focus executable validation failed: %s", error_msg)
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
        args = [configured, "-i", str(tmp_path.resolve())]

        # Parse additional args safely using shlex (handles quotes and escapes properly)
        extra_args = config.get("helicon", "args")
        if extra_args:
            try:
                parsed_args = parse_external_arguments(extra_args)
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
        return True, FocusStackerLaunch(process=process, manifest_path=tmp_path)
    except (OSError, subprocess.SubprocessError) as e:
        log.exception(f"Failed to launch Helicon Focus: {e}")
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        return False, None


def launch_helicon_focus(
    raw_files: List[Path],
) -> Tuple[bool, Optional[FocusStackerLaunch]]:
    """Compatibility wrapper for the former Helicon-specific launcher."""
    return launch_focus_stacker(raw_files)
