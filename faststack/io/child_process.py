"""Helpers for external tools FastStack launches and does not wait on."""

import logging
import subprocess
import sys
import threading

log = logging.getLogger(__name__)


def inherited_output_kwargs() -> dict:
    """Popen kwargs that send a child's stdout/stderr to FastStack's console.

    A console or launcher-wrapper run then captures tool output alongside
    FastStack's own log. Windowed builds have no usable stderr, so the
    child's output is discarded there as before.
    """
    stream = sys.stderr
    if stream is None or getattr(stream, "closed", False):
        return {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        stream.fileno()
    except (AttributeError, OSError, ValueError):
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
