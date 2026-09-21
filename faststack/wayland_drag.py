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
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def begin(self) -> None:
        with self._lock:
            self._source_id = None
            self._finished = False
            self._cancelled = False
            self._outcome_ready.clear()
            self._active = True

    def end(self) -> bool:
        # The protocol event can be queued in the pipe when QDrag.exec returns.
        self._outcome_ready.wait(0.1)
        with self._lock:
            self._active = False
            finished = self._finished and not self._cancelled
        return finished

    def _record(self, line: bytes) -> None:
        with self._lock:
            if not self._active:
                return
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
        pending = b""
        while chunk := os.read(self._read_fd, 65536):
            pending += chunk
            *lines, pending = pending.split(b"\n")
            for line in lines:
                complete_line = line + b"\n"
                if _WAYLAND_LINE.match(line):
                    self._record(line)
                    if self._forward_protocol:
                        self._forward(complete_line)
                else:
                    self._forward(complete_line)
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

    forward_protocol = bool(os.environ.get("WAYLAND_DEBUG"))
    os.environ["WAYLAND_DEBUG"] = "1"
    original_stderr = None
    read_fd = None
    write_fd = None
    try:
        original_stderr = os.dup(2)
        read_fd, write_fd = os.pipe()
        os.dup2(write_fd, 2)
        os.close(write_fd)
        write_fd = None
        return WaylandDragOutcomeMonitor(original_stderr, read_fd, forward_protocol)
    except (OSError, RuntimeError):
        if original_stderr is not None:
            os.dup2(original_stderr, 2)
            os.close(original_stderr)
        if read_fd is not None:
            os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
        log.warning("Wayland drag outcome monitor is unavailable", exc_info=True)
        return None
