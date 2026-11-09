"""Helpers for external tools FastStack launches.

Two ways to run a tool:

* ``launch_with_relayed_output()`` for programs FastStack starts and does not
  parse (focus stackers, image editors, the RawTherapee GUI). Their stdout and
  stderr land in FastStack's own log, one line each, prefixed with the
  program's name, so the console shows which tool a message came from.
* ``subprocess.Popen(..., stdout=PIPE, stderr=PIPE, env=child_environment())``
  plus ``communicate()`` for programs whose output FastStack reads
  (rawtherapee-cli, ExifTool); ``log_tool_output()`` then logs the captured
  text with the same prefix.

``child_popen_kwargs()`` (console inheritance) remains for the one child that
is not a tool: a FastStack window respawned for crash recovery.
"""

import logging
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time

from faststack import wayland_drag

log = logging.getLogger(__name__)

# Lines relayed from external tools. setup_logging() pins this logger at INFO
# so they show by default: they stand in for what the tool used to write
# straight to the console.
tool_log = logging.getLogger("faststack.external")

_RELAY_POLL_ACTIVE = 0.05  # seconds between reads while output is flowing
_RELAY_POLL_IDLE = 0.5  # back-off ceiling while the tool is quiet
_RELAY_LINGER = 2.0  # keep reading this long after the child has exited
_RELAY_MAX_LINE = 64 * 1024  # flush a line that never ends past this size
_LINE_BREAK = re.compile(rb"\r\n|\r|\n")


def _stream_fd(stream) -> int | None:
    """Return the descriptor behind a standard stream, or None when unusable."""
    if stream is None or getattr(stream, "closed", False):
        return None
    try:
        return stream.fileno()
    except (AttributeError, OSError, ValueError):
        return None


def _console_stderr_fd() -> int | None:
    """Return the descriptor FastStack's own stderr output really reaches.

    On Wayland the drag monitor replaces fd 2 with a pipe it drains itself and
    keeps the real stderr on a saved descriptor; children must write there,
    not into the monitor's pipe.
    """
    saved = wayland_drag.original_stderr_fd()
    if saved is not None:
        return saved
    return _stream_fd(sys.stderr)


def _is_console_like(fd: int | None) -> bool:
    """True for a terminal or regular file a child may keep open indefinitely.

    Pipes and sockets are excluded: a long-lived editor inheriting a launcher
    wrapper's capture pipe would keep that wrapper waiting for EOF after
    FastStack itself has exited.
    """
    if fd is None:
        return False
    try:
        mode = os.fstat(fd).st_mode
    except OSError:
        return False
    return not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode))


def inherited_output_kwargs() -> dict:
    """Popen kwargs that send a child's stdout/stderr to FastStack's console.

    A console or regular-file redirection captures tool output alongside
    FastStack's own log. Each stream is checked on its own: pipe-backed or
    unavailable output is discarded (windowed builds have no usable console).
    When the Wayland drag monitor has taken over fd 2, the child gets the
    saved real stderr instead of the monitor's pipe.
    """
    stdout = None if _is_console_like(_stream_fd(sys.stdout)) else subprocess.DEVNULL

    stderr_fd = _console_stderr_fd()
    if not _is_console_like(stderr_fd):
        stderr = subprocess.DEVNULL
    elif wayland_drag.original_stderr_fd() is not None:
        stderr = stderr_fd
    else:
        stderr = None
    return {"stdout": stdout, "stderr": stderr}


def child_environment() -> dict[str, str]:
    """Environment for child processes, without FastStack-internal debug flags."""
    return wayland_drag.child_environment()


def child_popen_kwargs() -> dict:
    """Popen kwargs for a child that shares FastStack's console: a respawned window.

    Tools go through ``launch_with_relayed_output()`` instead, so their lines
    carry the program's name.
    """
    kwargs = inherited_output_kwargs()
    kwargs["env"] = child_environment()
    return kwargs


def log_tool_output(name: str, text) -> None:
    """Log already-captured tool output line by line as ``[name] line``.

    For tools run with ``PIPE`` + ``communicate()``; ``text`` may be str,
    bytes or None (nothing captured).
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if not isinstance(text, str):
        return
    for line in text.splitlines():
        line = line.rstrip()
        if line:
            tool_log.info("[%s] %s", name, line)


class ToolOutputRelay:
    """Capture a launched tool's stdout/stderr in a file and log each line.

    The child writes to an ordinary temporary file rather than a pipe, so it
    never depends on FastStack staying alive: a pipe whose reader has gone
    kills the writer with SIGPIPE (or raises BrokenPipeError inside a Python
    tool such as OpenFocus) the next time it prints, which would abort a
    focus stack or an editor session when FastStack is closed first. A daemon
    thread tails the file while the child runs and logs every line as
    ``[name] line`` through the ``faststack.external`` logger, which reaches
    the console or the log file like FastStack's own output, then logs the
    exit status, so no separate reaper is needed.

    On POSIX the file is unlinked as soon as both ends are open, so nothing is
    left behind even if FastStack dies. Windows cannot unlink an open file;
    there the file is removed when the tool exits and is left in the temp
    folder only if FastStack exits first while the tool keeps running.
    Output from a helper the tool spawns and leaves behind after exiting is
    read for a short grace period only.
    """

    def __init__(self, name: str):
        self.name = name
        fd, path = tempfile.mkstemp(prefix="faststack-tool-", suffix=".log")
        self._write_fd: int | None = fd
        self._path: str | None = path
        self._reader = None
        try:
            self._reader = open(path, "rb", buffering=0)
            if os.name != "nt":
                os.unlink(path)
                self._path = None
        except OSError:
            self.abandon()
            raise

    def popen_kwargs(self) -> dict:
        """Popen keyword arguments that send both streams into the relay."""
        return {
            "stdout": self._write_fd,
            "stderr": subprocess.STDOUT,
            "env": child_environment(),
        }

    def start(self, process: subprocess.Popen) -> None:
        """Begin tailing once ``process`` owns its own copy of the write handle."""
        self._close_write_end()
        threading.Thread(
            target=self._tail,
            args=(process,),
            name=f"relay-{self.name}",
            daemon=True,
        ).start()

    def abandon(self) -> None:
        """Release everything when the launch itself failed."""
        self._close_write_end()
        self._close_reader()
        self._unlink()

    # -- internals ---------------------------------------------------------

    def _tail(self, process: subprocess.Popen) -> None:
        pending = b""
        delay = _RELAY_POLL_ACTIVE
        returncode = None
        linger_until: float | None = None
        try:
            while True:
                chunk = self._read_available()
                if chunk:
                    pending = self._emit_lines(pending + chunk)
                    delay = _RELAY_POLL_ACTIVE
                    if linger_until is not None:
                        linger_until = time.monotonic() + _RELAY_LINGER
                else:
                    delay = min(delay * 2, _RELAY_POLL_IDLE)
                if linger_until is None:
                    returncode = process.poll()
                    if returncode is not None:
                        linger_until = time.monotonic() + _RELAY_LINGER
                elif time.monotonic() >= linger_until:
                    break
                time.sleep(delay)
            chunk = self._read_available()
            if chunk:
                pending = self._emit_lines(pending + chunk)
            if pending.strip():
                self._emit(pending)
            if isinstance(returncode, int):
                level = logging.WARNING if returncode != 0 else logging.INFO
                tool_log.log(level, "[%s] exited with status %s", self.name, returncode)
        except Exception:
            log.debug("Output relay for %s stopped", self.name, exc_info=True)
        finally:
            self._close_reader()
            self._unlink()

    def _read_available(self) -> bytes:
        reader = self._reader
        if reader is None:
            return b""
        try:
            return reader.read() or b""
        except OSError:
            return b""

    def _emit_lines(self, data: bytes) -> bytes:
        """Log every complete line in ``data`` and return the unfinished tail.

        A bare carriage return counts as a line break so progress output that
        rewrites one line still appears as it happens.
        """
        parts = _LINE_BREAK.split(data)
        for part in parts[:-1]:
            self._emit(part)
        remainder = parts[-1]
        if len(remainder) > _RELAY_MAX_LINE:
            self._emit(remainder)
            remainder = b""
        return remainder

    def _emit(self, raw: bytes) -> None:
        text = raw.decode("utf-8", "replace").rstrip()
        if text:
            tool_log.info("[%s] %s", self.name, text)

    def _close_write_end(self) -> None:
        fd, self._write_fd = self._write_fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _close_reader(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None:
            try:
                reader.close()
            except OSError:
                pass

    def _unlink(self) -> None:
        path, self._path = self._path, None
        if path is not None:
            try:
                os.unlink(path)
            except OSError:
                pass


def launch_with_relayed_output(args, *, name: str, **popen_kwargs) -> subprocess.Popen:
    """Start ``args`` with its stdout/stderr relayed to the log as ``[name]`` lines.

    Replaces ``subprocess.Popen(args, shell=False, **child_popen_kwargs())``
    followed by ``reap_in_background()``: the relay thread also waits for the
    child and logs its exit status. ``popen_kwargs`` may carry ``cwd``,
    ``stdin``, ``close_fds``, ``creationflags`` and the like, but not
    ``stdout``, ``stderr``, ``env`` or ``shell``. The shell is never used.
    """
    try:
        relay = ToolOutputRelay(name)
    except OSError as exc:
        # No usable temp file: the tool matters more than the prefix.
        log.warning(
            "Output relay unavailable for %s (%s); its output goes to the console",
            name,
            exc,
        )
        process = subprocess.Popen(
            args, shell=False, **child_popen_kwargs(), **popen_kwargs
        )
        reap_in_background(process, name)
        return process
    try:
        process = subprocess.Popen(
            args, shell=False, **relay.popen_kwargs(), **popen_kwargs
        )
    except BaseException:
        relay.abandon()
        raise
    relay.start(process)
    return process


def reap_in_background(process: subprocess.Popen, name: str) -> None:
    """Wait for ``process`` on a daemon thread so it never lingers as a zombie.

    For children started without an output relay (the relay reaps its own
    child). Single-instance editors often hand the file to an already-running
    window and exit at once; without a wait the exited child stays <defunct>
    until FastStack quits. A non-zero exit is logged so launch failures show
    up in the console log.
    """

    def _wait() -> None:
        returncode = process.wait()
        if returncode != 0:
            log.warning("%s exited with status %s", name, returncode)

    threading.Thread(target=_wait, name=f"reap-{name}", daemon=True).start()
