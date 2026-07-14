"""Per-instance session registry for crash/reboot recovery.

Each running FastStack process owns exactly one JSON file under
``get_app_data_dir() / "sessions"``, named by a per-launch UUID so that
concurrent instances never clobber each other. The file records the open
directory, current image index, and view mode. A clean shutdown deletes the
file; a file that survives is therefore evidence that the process was killed
(crash or reboot) while a folder was open, and is offered for reopening on the
next launch.
"""

import ctypes
import json
import logging
import os
import sys
import time
import uuid
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

    Linux/WSL exposes a per-boot UUID. On Windows we derive a boot timestamp
    from the system uptime. The exact value is irrelevant — only equality
    against a stored value matters, so a mismatch reliably means "rebooted".
    """
    # Linux / WSL
    boot_id_path = Path("/proc/sys/kernel/random/boot_id")
    try:
        if boot_id_path.exists():
            return boot_id_path.read_text().strip()
    except OSError:
        pass

    # Windows: GetTickCount64() is milliseconds since boot; subtract from the
    # wall clock to get an (approximate, second-rounded) boot timestamp.
    windll = getattr(ctypes, "windll", None)
    if windll is not None:
        try:
            get_tick_count64 = windll.kernel32.GetTickCount64
            get_tick_count64.restype = ctypes.c_ulonglong
            ticks_ms = get_tick_count64()
            return f"boot:{int(time.time() - ticks_ms / 1000.0)}"
        except (AttributeError, OSError):
            pass

    return ""


def _pid_alive(pid: int) -> bool:
    """Best-effort check whether a process with ``pid`` is currently running."""
    if pid <= 0:
        return False
    windll = getattr(ctypes, "windll", None)
    if windll is not None:
        try:
            # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            windll.kernel32.CloseHandle(handle)
            return True
        except (AttributeError, OSError):
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

    grid = data.get("grid")
    if grid is not None and not isinstance(grid, bool):
        return None

    pid = data.get("pid", -1)
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None

    boot_id = data.get("boot_id", "")
    if not isinstance(boot_id, str):
        return None

    updated = data.get("updated", 0.0)
    if isinstance(updated, bool) or not isinstance(updated, (int, float)):
        return None

    normalized = dict(data)
    normalized["dir"] = directory
    normalized["index"] = index
    normalized["grid"] = grid
    normalized["pid"] = pid
    normalized["boot_id"] = boot_id
    normalized["updated"] = float(updated)
    return normalized


class SessionRegistry:
    """Tracks this process's open session for crash recovery."""

    def __init__(self):
        self.session_id = uuid.uuid4().hex
        self.path = _sessions_dir() / f"{self.session_id}.json"

    def update(self, image_dir, index: int, grid: bool) -> None:
        """Record/refresh this instance's session file. Best-effort."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            directory = Path(image_dir).expanduser().resolve()
            payload = {
                "dir": str(directory),
                "index": int(index),
                "grid": bool(grid),
                "pid": os.getpid(),
                "boot_id": _current_boot_id(),
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
            # Live sibling: same boot and process still running. If this
            # platform has no stable boot id, fall back to the PID check so we
            # do not offer or prune another active FastStack window.
            if (current_boot and boot_id == current_boot and _pid_alive(pid)) or (
                not current_boot and _pid_alive(pid)
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


def respawn_for_directory(directory: str, grid: Optional[bool]) -> None:
    """Launch a new FastStack process for ``directory``.

    Used to reopen additional folders (one window each) selected at the
    crash-recovery prompt. The CLI path restores the folder's last index from
    its own sidecar; ``--loupe`` is passed when the folder was last in loupe
    view so the view mode is preserved too.
    """
    import subprocess

    args = [sys.executable, "-m", "faststack.app"]
    if grid is False:
        args.append("--loupe")
    args.append(directory)
    try:
        subprocess.Popen(args, shell=False)
    except OSError as e:
        log.warning("Failed to respawn FastStack for %s: %s", directory, e)
