"""TurboJPEG discovery helpers with Windows DLL fallbacks."""

from __future__ import annotations

import logging
import os
import sys
import threading
import warnings
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger(__name__)
_fallback_warnings_emitted: set[str] = set()

try:
    from turbojpeg import TJPF_RGB, TurboJPEG
except ImportError:  # pragma: no cover - exercised via create_turbojpeg
    TurboJPEG = None
    TJPF_RGB = None

# --- Per-thread decoder warning capture -------------------------------------
#
# PyTurboJPEG reports recoverable libjpeg problems (notably "Premature end of
# JPEG file" for a truncated snapshot) through ``warnings.warn``.
# ``warnings.catch_warnings`` swaps process-global state and is not safe while
# several decode threads run at once: a thread can lose its recorder mid-decode
# or receive another thread's warning. Instead, one permanent showwarning
# router delivers each warning to the sink of the thread that emitted it.

_capture_state = threading.local()
_router_lock = threading.Lock()
_ROUTER_MARK = "_faststack_thread_warning_router"
# Without an "always" filter the default action shows a given message once per
# call site, so a second truncated file would go unreported. Scoped to the one
# message FastStack acts on so other warning policies are left untouched.
TRUNCATION_WARNING_TEXT = "Premature end of JPEG file"


def _has_truncation_filter() -> bool:
    for action, message, _category, _module, _lineno in warnings.filters:
        if (
            action == "always"
            and message is not None
            and message.pattern == TRUNCATION_WARNING_TEXT
        ):
            return True
    return False


def _install_warning_router() -> None:
    """Install (or re-install) the per-thread router and its filter.

    Cheap when already installed. Re-checked on every capture because
    ``catch_warnings`` blocks elsewhere (including test harnesses) restore the
    previous global state on exit and drop the router with it.
    """
    if getattr(warnings.showwarning, _ROUTER_MARK, False) and _has_truncation_filter():
        return
    with _router_lock:
        previous = warnings.showwarning
        if not getattr(previous, _ROUTER_MARK, False):

            def _router(message, category, filename, lineno, file=None, line=None):
                sink = getattr(_capture_state, "sink", None)
                if sink is not None:
                    sink.append(str(message))
                    return
                previous(message, category, filename, lineno, file, line)

            setattr(_router, _ROUTER_MARK, True)
            warnings.showwarning = _router
        if not _has_truncation_filter():
            warnings.filterwarnings("always", message=TRUNCATION_WARNING_TEXT)


@contextmanager
def capture_thread_warnings():
    """Collect the messages of warnings emitted on the current thread only."""
    _install_warning_router()
    previous_sink = getattr(_capture_state, "sink", None)
    sink: list[str] = []
    _capture_state.sink = sink
    try:
        yield sink
    finally:
        _capture_state.sink = previous_sink
        if previous_sink is not None:
            previous_sink.extend(sink)


def forward_thread_warnings(messages: list[str]) -> None:
    """Hand warnings captured on a helper thread to the current thread's sink.

    Used by split decodes, whose chunk workers run on pool threads while the
    caller waits inside its own :func:`capture_thread_warnings` block.
    """
    if not messages:
        return
    sink = getattr(_capture_state, "sink", None)
    if sink is not None:
        sink.extend(messages)
        return
    for message in messages:
        warnings.warn(message, RuntimeWarning, stacklevel=2)


def _candidate_library_paths() -> list[Optional[str]]:
    """Return candidate libjpeg-turbo library paths to try in priority order."""
    candidates: list[Optional[str]] = []

    explicit = os.getenv("FASTSTACK_TURBOJPEG_LIB") or os.getenv("TURBOJPEG_LIB")
    if explicit:
        candidates.append(explicit)
    candidates.extend(_bundled_library_paths())
    candidates.append(None)

    if os.name == "nt":
        common_roots = [
            os.getenv("FASTSTACK_TURBOJPEG_ROOT"),
            os.getenv("SystemDrive", "C:") + os.sep,
            os.getenv("ProgramFiles"),
            os.getenv("ProgramFiles(x86)"),
        ]
        suffixes = [
            ("libjpeg-turbo", "bin", "turbojpeg.dll"),
            ("libjpeg-turbo64", "bin", "turbojpeg.dll"),
            ("libjpeg-turbo-gcc64", "bin", "turbojpeg.dll"),
            ("TurboJPEG", "bin", "turbojpeg.dll"),
            ("bin", "turbojpeg.dll"),
        ]
        for root in common_roots:
            if not root:
                continue
            for suffix in suffixes:
                candidates.append(str(Path(root).joinpath(*suffix)))

        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            candidates.append(
                str(
                    Path(local_app_data)
                    / "Programs"
                    / "libjpeg-turbo"
                    / "bin"
                    / "turbojpeg.dll"
                )
            )

        for path_dir in os.getenv("PATH", "").split(os.pathsep):
            if path_dir:
                candidates.append(str(Path(path_dir) / "turbojpeg.dll"))

    unique: list[Optional[str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = "__default__" if candidate is None else os.path.normcase(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _bundled_library_paths() -> list[str]:
    """Return libjpeg-turbo candidates inside a frozen app bundle."""
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))

    if getattr(sys, "frozen", False) and sys.executable:
        executable_dir = Path(sys.executable).resolve().parent
        roots.extend([executable_dir, executable_dir / "_internal"])

    if os.name == "nt":
        library_names = ("turbojpeg.dll",)
    elif sys.platform == "darwin":
        library_names = ("libturbojpeg.dylib",)
    else:
        library_names = ("libturbojpeg.so", "libturbojpeg.so.0")

    candidates: list[str] = []
    seen: set[str] = set()
    for root in roots:
        for directory in (root, root / "lib", root / "bin"):
            for library_name in library_names:
                candidate = directory / library_name
                key = os.path.normcase(str(candidate))
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(str(candidate))
    return candidates


def _install_hint() -> str:
    """Return a concise, platform-specific libjpeg-turbo install hint."""
    if os.name == "nt":
        return (
            "Windows: install the x64 libjpeg-turbo package so "
            r"C:\libjpeg-turbo64\bin\turbojpeg.dll exists, or set "
            "FASTSTACK_TURBOJPEG_LIB to the full turbojpeg.dll path."
        )
    if sys.platform == "darwin":
        return (
            "macOS: install libjpeg-turbo with `brew install jpeg-turbo`, "
            "or set FASTSTACK_TURBOJPEG_LIB to the full libturbojpeg.dylib path."
        )
    return (
        "Linux: install the TurboJPEG shared library, for example "
        "`sudo apt install libturbojpeg` on Debian/Ubuntu, "
        "`sudo dnf install turbojpeg` on Fedora, or "
        "`sudo pacman -S libjpeg-turbo` on Arch. You can also set "
        "FASTSTACK_TURBOJPEG_LIB to the full libturbojpeg.so path."
    )


def _warn_fallback_once(message: str, *args: object) -> None:
    """Emit only one user-facing TurboJPEG fallback warning per process."""
    if message in _fallback_warnings_emitted:
        return
    _fallback_warnings_emitted.add(message)
    log.warning(message, *args)


@lru_cache(maxsize=8)
def _create_turbojpeg_cached(
    _decoder_identity: int,
    candidates: tuple[Optional[str], ...],
) -> Tuple[Optional["TurboJPEG"], bool]:
    """Probe TurboJPEG once per candidate set and cache the result."""
    if TurboJPEG is None:
        _warn_fallback_once(
            "PyTurboJPEG is not installed. Falling back to Pillow for JPEG "
            "decoding, which is slower for large folders. Install PyTurboJPEG "
            "and libjpeg-turbo to enable faster image navigation. %s",
            _install_hint(),
        )
        return None, False

    failures: list[str] = []
    for candidate in candidates:
        try:
            decoder = TurboJPEG() if candidate is None else TurboJPEG(candidate)
        except Exception as exc:
            source = "default loader" if candidate is None else candidate
            failures.append(f"{source}: {exc}")
            continue

        if candidate is None:
            log.info("PyTurboJPEG is available. Using it for JPEG decoding.")
        else:
            log.info("Loaded TurboJPEG library from %s", candidate)
        return decoder, True

    for failure in failures:
        log.debug("TurboJPEG load attempt failed: %s", failure)
    _warn_fallback_once(
        "TurboJPEG initialization failed (%d location(s) tried). "
        "PyTurboJPEG is installed, but the native libjpeg-turbo shared "
        "library was not found or could not be loaded. Falling back to "
        "Pillow for JPEG decoding, which is slower for large folders. %s",
        len(failures),
        _install_hint(),
    )
    return None, False


def create_turbojpeg() -> Tuple[Optional["TurboJPEG"], bool]:
    """Create a TurboJPEG decoder if possible."""
    candidates = tuple(_candidate_library_paths())
    return _create_turbojpeg_cached(id(TurboJPEG), candidates)
