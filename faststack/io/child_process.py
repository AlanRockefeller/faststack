"""Helpers for external tools FastStack launches and does not wait on."""

import logging
import os
import stat
import subprocess
import sys
import threading

from faststack import wayland_drag

log = logging.getLogger(__name__)

_DISCARD_OUTPUT = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}


def _console_stderr_fd() -> int | None:
    """Return the descriptor FastStack's own stderr output really reaches.

    On Wayland the drag monitor replaces fd 2 with a pipe it drains itself and
    keeps the real stderr on a saved descriptor; children must write there,
    not into the monitor's pipe.
    """
    saved = wayland_drag.original_stderr_fd()
    if saved is not None:
        return saved
    stream = sys.stderr
    if stream is None or getattr(stream, "closed", False):
        return None
    try:
        return stream.fileno()
    except (AttributeError, OSError, ValueError):
        return None


def inherited_output_kwargs() -> dict:
    """Popen kwargs that send a child's stdout/stderr to FastStack's console.

    A console or regular-file redirection captures tool output alongside
    FastStack's own log. Pipe-backed output is discarded so a long-lived
    editor cannot keep a launcher wrapper's capture pipe open after FastStack
    exits. Windowed builds have no usable stderr and are discarded too.
    """
    fd = _console_stderr_fd()
    if fd is None:
        return dict(_DISCARD_OUTPUT)
    try:
        mode = os.fstat(fd).st_mode
    except OSError:
        return dict(_DISCARD_OUTPUT)
    if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
        return dict(_DISCARD_OUTPUT)
    if wayland_drag.original_stderr_fd() is not None:
        # fd 2 is the drag monitor's pipe; hand the child the real stderr.
        return {"stdout": None, "stderr": fd}
    return {"stdout": None, "stderr": None}


def child_environment() -> dict[str, str]:
    """Environment for child processes, without FastStack-internal debug flags."""
    return wayland_drag.child_environment()


def child_popen_kwargs() -> dict:
    """Popen kwargs every launched tool should use: console output plus a clean env."""
    kwargs = inherited_output_kwargs()
    kwargs["env"] = child_environment()
    return kwargs


def reap_in_background(process: subprocess.Popen, name: str) -> None:
    """Wait for ``process`` on a daemon thread so it never lingers as a zombie.

    Single-instance editors (PhotoCraft, GIMP) often hand the file to an
    already-running window and exit at once; without a wait the exited child
    stays <defunct> until FastStack quits. A non-zero exit is logged so
    launch failures show up in the console log.
    """

    def _wait() -> None:
        returncode = process.wait()
        if returncode != 0:
            log.warning("%s exited with status %s", name, returncode)

    threading.Thread(target=_wait, name=f"reap-{name}", daemon=True).start()
