"""Recovery for Qt's Windows screen enumeration falling back to 96 DPI.

Qt's Windows platform plugin reads each monitor's DPI through a device
context created with ``CreateDC()`` while enumerating screens. When that
call fails -- observed after resuming a laptop from suspend, while the
display is still coming back -- the plugin logs

    monitorData: Unable to obtain handle for monitor '\\\\.\\DISPLAY1',
    defaulting to 96 DPI.

and keeps that fallback for the rest of the process' life. Every screen
then reports a device pixel ratio of 1.0, so the whole UI is drawn 1:1 on a
scaled display: the window keeps its size but text and images render tiny
(2.5x too small on a 240 DPI screen).

Qt only re-reads monitors when it sees ``WM_DISPLAYCHANGE``, and the message
that arrived on resume is the one it already mishandled, so nothing corrects
it short of a restart. This watchdog compares Qt's device pixel ratio
against the DPI Windows reports for the same monitor and, when Qt is stuck
at 1.0, posts a synthetic ``WM_DISPLAYCHANGE`` to Qt's own hidden
``ScreenChangeObserverWindow``. That re-runs the plugin's enumeration
exactly as changing the display scale by hand would.

No-ops on every platform except Windows.
"""

import ctypes
import functools
import logging
import os
import sys
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QScreen

log = logging.getLogger(__name__)

_WM_DISPLAYCHANGE = 0x007E
_MDT_EFFECTIVE_DPI = 0
_OBSERVER_CLASS_SUFFIX = "ScreenChangeObserverWindow"

# Below 125% scaling a monitor legitimately yields a device pixel ratio of
# 1.0, so a mismatch there is not evidence of the failure.
_MIN_SCALED_DPI = 120

# A healthy scaled screen reports well above 1.0; compare with slack rather
# than against the float literal.
_STUCK_RATIO_MAX = 1.05

_CHECK_INTERVAL_MS = 30_000

# Consecutive failed repairs before telling the user a restart is needed.
_MAX_QUIET_ATTEMPTS = 3

# Any of these means the user chose the scaling themselves; leave it alone.
_SCALING_OVERRIDE_VARS = (
    "QT_SCALE_FACTOR",
    "QT_SCREEN_SCALE_FACTORS",
    "QT_FONT_DPI",
    "QT_ENABLE_HIGHDPI_SCALING",
)


class DpiWatchdog(QObject):
    """Detects and repairs Qt's cached 96 DPI fallback on Windows."""

    repairFailed = Signal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._failed_attempts = 0
        self._reported = False
        self._timer = QTimer(self)
        self._timer.setInterval(_CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self._check)

    def start(self) -> None:
        self._timer.start()
        # Screen changes are the moment Qt installs the bad value, so react
        # to them directly instead of waiting out the polling interval.
        app = _gui_app()
        if app is not None:
            app.screenAdded.connect(self._on_screens_changed)
            app.screenRemoved.connect(self._on_screens_changed)
            app.primaryScreenChanged.connect(self._on_screens_changed)

    def _on_screens_changed(self, _screen: object = None) -> None:
        # Windows hands out monitor handles a moment after announcing the
        # change; a short delay avoids reading it mid-transition.
        QTimer.singleShot(1000, self._check)

    def _check(self) -> None:
        app = _gui_app()
        if app is None:
            return
        try:
            dpi_by_device = _monitor_dpi_by_device()
        except OSError:
            log.debug("DPI watchdog: could not query monitors", exc_info=True)
            return
        if not dpi_by_device:
            return

        stuck = _stuck_screens(app.screens(), dpi_by_device)
        if not stuck:
            self._failed_attempts = 0
            return

        for screen, dpi in stuck:
            log.warning(
                "Qt reports devicePixelRatio %.2f for monitor %s but Windows "
                "reports %d DPI: Qt's screen enumeration fell back to 96 DPI, "
                "so the interface is drawn too small. Forcing re-enumeration.",
                screen.devicePixelRatio(),
                screen.name(),
                dpi,
            )

        try:
            posted = _post_display_change()
        except OSError:
            log.debug("DPI watchdog: repair failed", exc_info=True)
            posted = 0
        if not posted:
            log.warning("DPI watchdog: no screen-change observer window found")

        self._failed_attempts += 1
        if self._failed_attempts >= _MAX_QUIET_ATTEMPTS and not self._reported:
            self._reported = True
            self.repairFailed.emit()


def _gui_app() -> Optional[QGuiApplication]:
    app = QGuiApplication.instance()
    return app if isinstance(app, QGuiApplication) else None


def _stuck_screens(
    screens: List[QScreen], dpi_by_device: Dict[str, int]
) -> List[Tuple[QScreen, int]]:
    """Screens Qt scales 1:1 while Windows reports a scaled monitor."""
    stuck: List[Tuple[QScreen, int]] = []
    for screen in screens:
        dpi = dpi_by_device.get(screen.name())
        if dpi is None or dpi < _MIN_SCALED_DPI:
            continue
        if screen.devicePixelRatio() > _STUCK_RATIO_MAX:
            continue
        stuck.append((screen, dpi))
    return stuck


def start_dpi_watchdog(parent: Optional[QObject] = None) -> Optional["DpiWatchdog"]:
    """Start the watchdog, or return None when it does not apply."""
    if sys.platform != "win32":
        return None
    app = _gui_app()
    # Headless runs (offscreen/minimal) have neither the observer window nor
    # real monitors, so there is nothing to watch.
    if app is None or app.platformName() != "windows":
        log.debug("DPI watchdog disabled: platform is not the Windows plugin")
        return None
    for var in _SCALING_OVERRIDE_VARS:
        if os.environ.get(var):
            log.debug("DPI watchdog disabled: %s is set", var)
            return None
    try:
        _monitor_dpi_by_device()
    except OSError:
        log.debug("DPI watchdog unavailable", exc_info=True)
        return None

    watchdog = DpiWatchdog(parent)
    watchdog.start()
    return watchdog


# --- Win32 plumbing -------------------------------------------------------
#
# ctypes defaults every return value to a 32-bit int, which truncates the
# 64-bit handles these functions return, so each one is declared explicitly.

if sys.platform == "win32":
    from ctypes import wintypes

    class _MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    _MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HANDLE,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )
    _ENUMWINDOWSPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @functools.lru_cache(maxsize=1)
    def _user32() -> ctypes.WinDLL:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.EnumDisplayMonitors.argtypes = [
            wintypes.HDC,
            ctypes.POINTER(wintypes.RECT),
            _MONITORENUMPROC,
            wintypes.LPARAM,
        ]
        user32.EnumDisplayMonitors.restype = wintypes.BOOL
        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.EnumWindows.argtypes = [_ENUMWINDOWSPROC, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.GetClassNameW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.PostMessageW.restype = wintypes.BOOL
        return user32

    @functools.lru_cache(maxsize=1)
    def _shcore() -> ctypes.WinDLL:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.GetDpiForMonitor.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        shcore.GetDpiForMonitor.restype = ctypes.c_long
        return shcore

    def _monitor_dpi_by_device() -> Dict[str, int]:
        """Map each monitor's device name to its effective DPI.

        This is the same query Qt's own ``monitorDPI()`` makes, but it does
        not depend on ``CreateDC()``, which is the call that fails.
        """
        user32 = _user32()
        shcore = _shcore()
        result: Dict[str, int] = {}

        def collect(hmonitor, _hdc, _rect, _lparam) -> bool:
            info = _MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
            if not user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
                return True
            dpi_x = ctypes.c_uint()
            dpi_y = ctypes.c_uint()
            if (
                shcore.GetDpiForMonitor(
                    hmonitor,
                    _MDT_EFFECTIVE_DPI,
                    ctypes.byref(dpi_x),
                    ctypes.byref(dpi_y),
                )
                == 0
            ):
                result[info.szDevice] = dpi_x.value
            return True

        user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(collect), 0)
        return result

    def _post_display_change() -> int:
        """Ask Qt to re-enumerate screens; returns how many windows we hit.

        Qt's platform plugin listens for ``WM_DISPLAYCHANGE`` on a hidden
        top-level window of its own (``Qt<version>ScreenChangeObserverWindow``),
        not on the application window, so the message goes there. Only this
        process' windows are touched -- never a broadcast.
        """
        user32 = _user32()
        pid = os.getpid()
        observers: List[int] = []
        name = ctypes.create_unicode_buffer(256)

        def collect(hwnd, _lparam) -> bool:
            window_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            if window_pid.value != pid:
                return True
            user32.GetClassNameW(hwnd, name, len(name))
            if name.value.endswith(_OBSERVER_CLASS_SUFFIX):
                observers.append(hwnd)
            return True

        user32.EnumWindows(_ENUMWINDOWSPROC(collect), 0)
        posted = 0
        for hwnd in observers:
            if user32.PostMessageW(hwnd, _WM_DISPLAYCHANGE, 0, 0):
                posted += 1
        return posted

else:  # pragma: no cover - the watchdog never starts off Windows

    def _monitor_dpi_by_device() -> Dict[str, int]:
        return {}

    def _post_display_change() -> int:
        return 0
