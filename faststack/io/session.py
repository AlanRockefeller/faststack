"""Per-instance session registry for crash/reboot recovery.

Each running FastStack process owns exactly one JSON file under
``get_app_data_dir() / "sessions"``, named by a per-launch UUID so that
concurrent instances never clobber each other. The file records the open
directory, current image path, index fallback, and view mode. A clean shutdown
deletes the file; a file that survives is therefore evidence that the process
was killed (crash or reboot) while a folder was open, and is offered for
reopening on the next launch.
"""

import ctypes
import json
import logging
import os
import sys
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import List, Optional, Tuple

from faststack.io.utils import atomic_write_json
from faststack.logging_setup import get_app_data_dir

log = logging.getLogger(__name__)


def _sessions_dir() -> Path:
    """Return (and create) the directory holding per-instance session files."""
    return get_app_data_dir() / "sessions"


def _current_boot_id() -> str:
    """Return an identifier that is stable within a boot and changes on reboot.

    Linux/WSL exposes a per-boot UUID, which is exactly this. Windows has no
    equivalent that is cheap to read, and the old derivation
    ``int(time.time() - GetTickCount64()/1000)`` mixed the adjustable wall
    clock with monotonic uptime: any clock correction changed the value
    without a reboot, so a live sibling's stored id stopped matching and its
    active session was offered as a crash survivor and then deleted
    (FS-P1-004). Windows liveness is now decided by ``_process_identity()``
    instead, and this function deliberately returns "" there.
    """
    # Linux / WSL
    boot_id_path = Path("/proc/sys/kernel/random/boot_id")
    try:
        if boot_id_path.exists():
            return boot_id_path.read_text().strip()
    except OSError:
        pass

    return ""


def _windows_kernel32():
    """Return kernel32 with the process APIs declared, or None off Windows."""
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        return None
    try:
        kernel32 = win_dll("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return None

    filetime_pointer = ctypes.POINTER(wintypes.FILETIME)
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        filetime_pointer,
        filetime_pointer,
        filetime_pointer,
        filetime_pointer,
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    return kernel32


def _windows_process_creation_time(pid: int) -> Optional[int]:
    """Return a Windows process's creation time, or None if unavailable.

    The (PID, creation time) pair is unique for the lifetime of a boot, so it
    identifies the exact process even after PID reuse.
    """
    kernel32 = _windows_kernel32()
    if kernel32 is None or pid <= 0:
        return None
    try:
        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            ok = kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            )
            if not ok:
                return None
            return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        return None


def _process_identity(pid: Optional[int] = None) -> str:
    """Return a string identifying the exact process, not merely its PID.

    Empty string means "this platform has no extra identity to record", in
    which case callers fall back to the boot id + PID check.
    """
    target_pid = os.getpid() if pid is None else pid
    creation = _windows_process_creation_time(target_pid)
    if creation is None:
        return ""
    return f"win:{creation}"


def _process_identity_matches(pid: int, stored_identity: str) -> Optional[bool]:
    """Compare a stored process identity against the live process.

    Returns True/False when a comparison was possible, and None when it was
    not (non-Windows, a legacy record without the field, or an unreadable
    process) so the caller can fall back to the previous heuristics.
    """
    if not stored_identity:
        return None
    current = _process_identity(pid)
    if not current:
        # PID is gone, or we cannot read it. _pid_alive() decides.
        return None
    return current == stored_identity


def _pid_alive(pid: int) -> bool:
    """Best-effort check whether a process with ``pid`` is currently running."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        kernel32 = _windows_kernel32()
        if kernel32 is None:
            return True
        try:
            # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            ctypes.set_last_error(0)
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                # ERROR_INVALID_PARAMETER means that the PID does not exist.
                # Other failures may be access restrictions, so stay
                # conservative and treat the process as potentially alive.
                return ctypes.get_last_error() != 87
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                # STILL_ACTIVE = 259
                return exit_code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, ValueError):
            # If we can't tell, assume alive so we don't offer a live folder.
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — treat as alive.
        return True
    except OSError:
        return True
    return True


def _validated_session_payload(data: object) -> Optional[dict]:
    """Return a normalized session dict, or None for malformed payloads."""
    if not isinstance(data, dict):
        return None

    directory = data.get("dir")
    if not isinstance(directory, str):
        return None

    index = data.get("index", 0)
    if isinstance(index, bool) or not isinstance(index, int):
        return None

    image_path = data.get("path")
    if image_path is not None and not isinstance(image_path, str):
        return None

    grid = data.get("grid")
    if grid is not None and not isinstance(grid, bool):
        return None

    pid = data.get("pid", -1)
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None

    boot_id = data.get("boot_id", "")
    if not isinstance(boot_id, str):
        return None

    # Legacy records predate proc_id. Missing or malformed simply means "no
    # extra identity available"; it must never invalidate the whole record.
    proc_id = data.get("proc_id", "")
    if not isinstance(proc_id, str):
        proc_id = ""

    updated = data.get("updated", 0.0)
    if isinstance(updated, bool) or not isinstance(updated, (int, float)):
        return None

    normalized = dict(data)
    normalized["dir"] = directory
    normalized["index"] = index
    normalized["path"] = image_path
    normalized["grid"] = grid
    normalized["pid"] = pid
    normalized["boot_id"] = boot_id
    normalized["proc_id"] = proc_id
    normalized["updated"] = float(updated)
    return normalized


def _is_live_sibling(
    *, pid: int, boot_id: str, proc_id: str, current_boot: str
) -> bool:
    """Is this session record owned by a FastStack process that is still running?

    Decided in order of how much the evidence actually proves:

    1. A recorded ``proc_id`` (Windows PID + process creation time) is the
       strongest signal. It survives wall-clock changes and cannot be spoofed
       by PID reuse, so when it can be compared at all it is authoritative --
       a mismatch means the PID belongs to some *other* process and the record
       really is stale.
    2. Otherwise a stable boot id (Linux/WSL) plus a live PID.
    3. Otherwise (legacy Windows record with no proc_id, or no boot id at all)
       fall back to the bare PID check, which is what shipped before. It can
       be fooled by PID reuse across a reboot, but erring toward "live" only
       costs a missed recovery offer, whereas erring toward "stale" deletes a
       running instance's session file.
    """
    if not _pid_alive(pid):
        return False

    identity_match = _process_identity_matches(pid, proc_id)
    if identity_match is not None:
        return identity_match

    if current_boot:
        # Unchanged Linux/WSL behaviour: a real per-boot UUID must match
        # exactly, so a reboot survivor whose PID got reused is still stale.
        return boot_id == current_boot

    return True


class SessionRegistry:
    """Tracks this process's open session for crash recovery."""

    def __init__(self):
        self.session_id = uuid.uuid4().hex
        self.path = _sessions_dir() / f"{self.session_id}.json"

    def update(
        self,
        image_dir,
        index: int,
        grid: bool,
        image_path: Optional[Path] = None,
    ) -> None:
        """Record/refresh this instance's session file. Best-effort."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            directory = Path(image_dir).expanduser().resolve()
            payload = {
                "dir": str(directory),
                "index": int(index),
                "path": str(image_path) if image_path is not None else None,
                "grid": bool(grid),
                "pid": os.getpid(),
                "boot_id": _current_boot_id(),
                # Distinguishes this exact process from a future PID reuse.
                # Empty on platforms with a real boot id (Linux/WSL).
                "proc_id": _process_identity(),
                "updated": time.time(),
            }
            atomic_write_json(self.path, payload)
        except (OSError, RuntimeError) as e:
            log.warning("Failed to update session file %s: %s", self.path, e)

    def close(self) -> None:
        """Delete this instance's session file on clean shutdown. Best-effort."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError as e:
            log.warning("Failed to remove session file %s: %s", self.path, e)

    @classmethod
    def scan_stale(cls) -> Tuple[List[dict], List[Path]]:
        """Find sessions orphaned by a crash/reboot.

        Returns ``(records, paths)`` where ``records`` are the stale session
        dicts (existing directory, de-duplicated by directory, most-recent
        first) and ``paths`` are every stale file on disk (including duplicates
        and dead-directory orphans) so the caller can prune them.
        """
        sessions_dir = _sessions_dir()
        records: List[dict] = []
        stale_paths: List[Path] = []
        current_boot = _current_boot_id()

        try:
            files = sorted(sessions_dir.glob("*.json"))
        except OSError:
            return [], []

        for file in files:
            try:
                raw_data = json.loads(file.read_text())
            except (OSError, ValueError):
                # Unreadable/corrupt — treat as a prunable orphan.
                stale_paths.append(file)
                continue

            data = _validated_session_payload(raw_data)
            if data is None:
                stale_paths.append(file)
                continue

            boot_id = data["boot_id"]
            pid = data["pid"]
            if _is_live_sibling(
                pid=pid,
                boot_id=boot_id,
                proc_id=data["proc_id"],
                current_boot=current_boot,
            ):
                continue

            # Stale: reboot survivor or same-boot crash survivor.
            stale_paths.append(file)
            data["_path"] = str(file)
            records.append(data)

        # Keep only sessions whose directory still exists, de-duplicated by
        # directory (newest wins), most-recent first.
        records.sort(key=lambda d: d.get("updated", 0), reverse=True)
        seen: set = set()
        unique: List[dict] = []
        for rec in records:
            directory = rec.get("dir", "")
            if not directory:
                continue
            try:
                directory_path = Path(directory).expanduser()
                if not directory_path.is_absolute():
                    continue
                directory_path = directory_path.resolve()
                resolved_directory = str(directory_path)
                if resolved_directory in seen:
                    continue
                seen.add(resolved_directory)
                if directory_path.is_dir():
                    rec["dir"] = resolved_directory
                    unique.append(rec)
            except (OSError, RuntimeError):
                continue

        return unique, stale_paths

    @classmethod
    def prune(cls, paths: List[Path]) -> None:
        """Delete consumed/orphaned session files. Best-effort."""
        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError as e:
                log.warning("Failed to prune session file %s: %s", path, e)


def respawn_for_directory(
    directory: str,
    grid: Optional[bool],
    image_path: Optional[str] = None,
    index: Optional[int] = None,
) -> bool:
    """Launch a new FastStack process for ``directory``. Returns success.

    Used to reopen additional folders (one window each) selected at the
    crash-recovery prompt. The internal CLI options restore the exact image
    path, with the index retained as a fallback; ``--loupe`` is passed when
    the folder was last in loupe view so the view mode is preserved too.

    The boolean matters: the caller must not prune a stale session record
    whose respawn failed, or the user's selection is neither reopened nor
    kept for another attempt (FS-P1-006).
    """
    import subprocess

    if not directory:
        return False

    args = [sys.executable, "-m", "faststack.app"]
    if grid is False:
        args.append("--loupe")
    if image_path:
        args.extend(["--restore-path", image_path])
    if index is not None:
        args.extend(["--restore-index", str(index)])
    args.append(directory)
    try:
        subprocess.Popen(args, shell=False)
    except OSError as e:
        log.warning("Failed to respawn FastStack for %s: %s", directory, e)
        return False
    return True
