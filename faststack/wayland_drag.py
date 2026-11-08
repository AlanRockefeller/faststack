"""Observe source-side Wayland drag outcomes hidden by QDrag.exec()."""

import logging
import os
import re
import sys
import threading

log = logging.getLogger(__name__)

_WAYLAND_LINE = re.compile(rb"^\[\d\d:\d\d:")
_START_DRAG = re.compile(rb"start_drag\(wl_data_source#(\d+),")
_FINISHED = re.compile(rb"wl_data_source#(\d+)\.dnd_finished\(\)")
_CANCELLED = re.compile(rb"wl_data_source#(\d+)\.cancelled\(\)")
# Data-transfer traffic worth keeping for --debug drag diagnostics.
_DND_TRACE = re.compile(rb"wl_data_(source|device|offer)|zwp_primary_selection")
_DND_TRACE_LIMIT = 400
_MAX_PENDING_LINE = 1 << 20

# Process-wide record of what install_wayland_drag_monitor() changed, so the
# child-process helpers can undo it for every program FastStack launches: a
# child must neither inherit WAYLAND_DEBUG (a Wayland client would then log its
# whole protocol into our pipe) nor write to fd 2 (the pipe), which would make
# inherited_output_kwargs() treat the console as a pipe and discard its output.
_monitor_installed = False
_previous_wayland_debug: str | None = None
_original_stderr_fd: int | None = None


def monitor_installed() -> bool:
    """Whether the drag monitor has taken over this process's stderr."""
    return _monitor_installed


def original_stderr_fd() -> int | None:
    """The real stderr descriptor saved by the monitor, or None when unredirected."""
    return _original_stderr_fd if _monitor_installed else None


def child_environment() -> dict[str, str]:
    """Return an environment for child processes without the monitor's debug flag.

    ``WAYLAND_DEBUG`` is restored to whatever the user had set before FastStack
    started (usually nothing), so launched editors, stackers and respawned
    FastStack instances run at normal speed and keep their own stderr.
    """
    env = dict(os.environ)
    if _monitor_installed:
        if _previous_wayland_debug is None:
            env.pop("WAYLAND_DEBUG", None)
        else:
            env["WAYLAND_DEBUG"] = _previous_wayland_debug
    return env


class WaylandDragOutcomeMonitor:
    """Track protocol completion for the active drag while forwarding stderr."""

    def __init__(self, original_stderr: int, read_fd: int, forward_protocol: bool):
        self._original_stderr = original_stderr
        self._read_fd = read_fd
        self._forward_protocol = forward_protocol
        self._lock = threading.Lock()
        self._outcome_ready = threading.Event()
        self._source_id: bytes | None = None
        self._active = False
        self._finished = False
        self._cancelled = False
        self._trace: list[str] = []
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def begin(self) -> None:
        with self._lock:
            self._source_id = None
            self._finished = False
            self._cancelled = False
            self._trace = []
            self._outcome_ready.clear()
            self._active = True

    def end(self) -> bool:
        # The protocol event can be queued in the pipe when QDrag.exec returns.
        self._outcome_ready.wait(0.1)
        with self._lock:
            self._active = False
            finished = self._finished and not self._cancelled
        return finished

    def outcome(self) -> str:
        """Describe the protocol outcome of the last drag, for diagnostics."""
        with self._lock:
            if self._source_id is None:
                return "no start_drag request seen"
            if self._cancelled:
                return "wl_data_source cancelled"
            if self._finished:
                return "wl_data_source dnd_finished"
            return "no finish or cancel event"

    def take_trace(self) -> list[str]:
        """Return and clear the data-transfer protocol lines of the last drag.

        Lines are buffered rather than logged as they arrive: logging from the
        reader thread would write back into the pipe that thread drains.
        """
        with self._lock:
            trace, self._trace = self._trace, []
        return trace

    def _record(self, line: bytes) -> None:
        with self._lock:
            if not self._active:
                return
            if _DND_TRACE.search(line) and len(self._trace) < _DND_TRACE_LIMIT:
                self._trace.append(line.decode("utf-8", "replace"))
            if self._source_id is None:
                match = _START_DRAG.search(line)
                if match:
                    self._source_id = match.group(1)
                return
            match = _FINISHED.search(line)
            if match and match.group(1) == self._source_id:
                self._finished = True
                self._outcome_ready.set()
                return
            match = _CANCELLED.search(line)
            if match and match.group(1) == self._source_id:
                self._cancelled = True
                self._outcome_ready.set()

    def _read_stderr(self) -> None:
        # This thread is the only reader of the process's stderr pipe: if it
        # ever died, the pipe would fill and every later stderr write in the
        # process would block. So nothing in the loop may escape it.
        pending = b""
        while True:
            try:
                chunk = os.read(self._read_fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            pending += chunk
            *lines, pending = pending.split(b"\n")
            for line in lines:
                complete_line = line + b"\n"
                try:
                    if _WAYLAND_LINE.match(line):
                        self._record(line)
                        if self._forward_protocol:
                            self._forward(complete_line)
                    else:
                        self._forward(complete_line)
                except Exception:  # noqa: BLE001 - keep draining the pipe
                    self._forward(complete_line)
            if len(pending) > _MAX_PENDING_LINE:
                # Pathological newline-free output: pass it through rather
                # than buffering it without bound.
                self._forward(pending)
                pending = b""
        if pending:
            self._forward(pending)

    def _forward(self, data: bytes) -> None:
        remaining = memoryview(data)
        while remaining:
            try:
                remaining = remaining[os.write(self._original_stderr, remaining) :]
            except OSError:
                return


def install_wayland_drag_monitor() -> WaylandDragOutcomeMonitor | None:
    """Start protocol capture before QApplication opens the Wayland display."""
    if sys.platform != "linux" or not os.environ.get("WAYLAND_DISPLAY"):
        return None
    platform = os.environ.get("QT_QPA_PLATFORM", "wayland").split(";", 1)[0]
    if platform != "wayland":
        return None

    global _monitor_installed, _previous_wayland_debug, _original_stderr_fd
    previous_wayland_debug = os.environ.get("WAYLAND_DEBUG")
    forward_protocol = bool(previous_wayland_debug)
    original_stderr = None
    read_fd = None
    write_fd = None
    installed = False
    try:
        os.environ["WAYLAND_DEBUG"] = "1"
        original_stderr = os.dup(2)
        read_fd, write_fd = os.pipe()
        os.dup2(write_fd, 2)
        os.close(write_fd)
        write_fd = None
        monitor = WaylandDragOutcomeMonitor(original_stderr, read_fd, forward_protocol)
        installed = True
        _monitor_installed = True
        _previous_wayland_debug = previous_wayland_debug
        _original_stderr_fd = original_stderr
        return monitor
    except (OSError, RuntimeError):
        if original_stderr is not None:
            try:
                os.dup2(original_stderr, 2)
            finally:
                os.close(original_stderr)
        if read_fd is not None:
            os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
        log.warning("Wayland drag outcome monitor is unavailable", exc_info=True)
        return None
    finally:
        if not installed:
            if previous_wayland_debug is None:
                os.environ.pop("WAYLAND_DEBUG", None)
            else:
                os.environ["WAYLAND_DEBUG"] = previous_wayland_debug
