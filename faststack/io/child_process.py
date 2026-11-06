"""Helpers for external tools FastStack launches and does not wait on."""

import logging
import os
import stat
import subprocess
import sys
import threading

log = logging.getLogger(__name__)


def inherited_output_kwargs() -> dict:
    """Popen kwargs that send a child's stdout/stderr to FastStack's console.

    A console or regular-file redirection captures tool output alongside
    FastStack's own log. Pipe-backed output is discarded so a long-lived
    editor cannot keep a launcher wrapper's capture pipe open after FastStack
    exits. Windowed builds have no usable stderr and are discarded too.
    """
    stream = sys.stderr
    if stream is None or getattr(stream, "closed", False):
        return {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        fd = stream.fileno()
        mode = os.fstat(fd).st_mode
    except (AttributeError, OSError, ValueError):
        return {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
        return {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    return {"stdout": None, "stderr": None}


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
