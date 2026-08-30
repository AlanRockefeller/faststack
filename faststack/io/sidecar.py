"""Manages reading and writing the faststack.json sidecar file."""

import copy
import dataclasses
import json
import logging
import os
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator, Literal, Optional, Union, overload

from faststack.io.indexer import JPG_EXTENSIONS, RAW_EXTENSIONS
from faststack.io.utils import atomic_write_json
from faststack.models import EntryMetadata, Sidecar

log = logging.getLogger(__name__)
KNOWN_IMAGE_EXTENSIONS = frozenset(
    ext.lower() for ext in JPG_EXTENSIONS | RAW_EXTENSIONS
)
_MISSING = object()


def _entrymetadata_from_json(meta: dict) -> EntryMetadata:
    """
    Helper to create EntryMetadata from JSON dict, handling legacy fields
    and filtering unknown keys.
    """
    try:
        # Handle legacy keys
        # Legacy 'flag' and 'reject' do not map to current EntryMetadata fields,
        # so they will be filtered out by valid_keys check below.

        # stack_id IS in the current model, so we keep it (don't delete it).

        # Filter out unknown keys
        valid_keys = {f.name for f in dataclasses.fields(EntryMetadata)}
        filtered_meta = {k: v for k, v in meta.items() if k in valid_keys}

        return EntryMetadata(**filtered_meta)
    except Exception as e:
        log.warning(f"Error parsing metadata entry: {e}")
        return EntryMetadata()


def _entrymetadata_to_json(meta: EntryMetadata) -> dict:
    """Convert EntryMetadata to a detached JSON-ready dict.

    ``edit_state`` is the only nested mutable field, so copying it explicitly
    detaches the entry without a full deepcopy — this runs once per entry on
    every save, which is a UI-thread cost on large folders.
    """
    data = meta.__dict__.copy()
    edit_state = data.get("edit_state")
    if edit_state is None:
        data.pop("edit_state", None)
    else:
        data["edit_state"] = copy.deepcopy(edit_state)
    return data


def _sidecar_to_json(sidecar: Sidecar) -> dict:
    """Return a detached JSON payload for a sidecar."""
    return {
        "version": sidecar.version,
        "last_index": sidecar.last_index,
        "last_path": sidecar.last_path,
        "sort_mode": sidecar.sort_mode,
        "entries": {
            key: _entrymetadata_to_json(meta) for key, meta in sidecar.entries.items()
        },
        "stacks": copy.deepcopy(sidecar.stacks),
        "stack_paths": copy.deepcopy(sidecar.stack_paths),
        "stack_order": copy.deepcopy(sidecar.stack_order),
    }


def _sidecar_from_json(data: dict) -> Sidecar:
    """Reconstruct a sidecar from a validated version-2 JSON payload."""
    entries_data = data.get("entries", {})
    if not isinstance(entries_data, dict):
        raise TypeError("sidecar entries must be an object")
    return Sidecar(
        version=data.get("version", 2),
        last_index=data.get("last_index", 0),
        last_path=(
            data.get("last_path") if isinstance(data.get("last_path"), str) else None
        ),
        sort_mode=(
            data.get("sort_mode") if isinstance(data.get("sort_mode"), str) else None
        ),
        entries={
            key: _entrymetadata_from_json(meta)
            for key, meta in entries_data.items()
            if isinstance(meta, dict)
        },
        stacks=copy.deepcopy(data.get("stacks", [])),
        stack_paths=copy.deepcopy(data.get("stack_paths", [])),
        stack_order=copy.deepcopy(data.get("stack_order", [])),
    )


def _select_three_way(base, ours, theirs):
    """Choose a value without letting an unchanged stale value win."""
    if ours == base:
        return _MISSING if theirs is _MISSING else copy.deepcopy(theirs)
    if theirs == base or ours == theirs:
        return _MISSING if ours is _MISSING else copy.deepcopy(ours)
    # Both writers changed the same value. The process holding the write lock
    # is the most recent writer, so its explicit change wins.
    return _MISSING if ours is _MISSING else copy.deepcopy(ours)


def _copy_merge_value(value):
    """Copy a merge value without cloning the identity-based missing sentinel."""
    return _MISSING if value is _MISSING else copy.deepcopy(value)


def _merge_entry(base, ours, theirs):
    """Three-way merge one metadata entry at field granularity."""
    if ours == base:
        return _copy_merge_value(theirs)
    if theirs == base or ours == theirs:
        return _copy_merge_value(ours)
    if not isinstance(ours, dict) or not isinstance(theirs, dict):
        return _copy_merge_value(ours)

    default_entry = _entrymetadata_to_json(EntryMetadata())
    base_fields = base if isinstance(base, dict) else default_entry
    merged = {}
    for field_name in base_fields.keys() | ours.keys() | theirs.keys():
        base_value = base_fields.get(field_name, _MISSING)
        our_value = ours.get(field_name, _MISSING)
        their_value = theirs.get(field_name, _MISSING)
        value = _select_three_way(base_value, our_value, their_value)
        if value is not _MISSING:
            merged[field_name] = value
    return merged


def _merge_entries(base, ours, theirs) -> dict:
    """Three-way merge the sidecar entry mapping."""
    base = base if isinstance(base, dict) else {}
    ours = ours if isinstance(ours, dict) else {}
    theirs = theirs if isinstance(theirs, dict) else {}
    merged = {}
    for key in base.keys() | ours.keys() | theirs.keys():
        value = _merge_entry(
            base.get(key, _MISSING),
            ours.get(key, _MISSING),
            theirs.get(key, _MISSING),
        )
        if value is not _MISSING:
            merged[key] = value
    return merged


def _valid_stack_identity(payload: dict) -> Optional[tuple[list[list[str]], list[str]]]:
    """Return validated semantic stack groups and their image order."""
    groups = payload.get("stack_paths")
    order = payload.get("stack_order")
    if not isinstance(groups, list) or not isinstance(order, list):
        return None
    # A serializer round-trip of a legacy, index-only sidecar adds empty
    # identity fields.  Those fields are not semantic evidence that its
    # still-present numeric stacks were intentionally cleared.
    if payload.get("stacks") and not groups:
        return None
    if any(not isinstance(key, str) or not key for key in order):
        return None
    if len(order) != len(set(order)):
        return None
    known = set(order)
    normalized = []
    for group in groups:
        if not isinstance(group, list) or not group:
            return None
        if any(not isinstance(key, str) or key not in known for key in group):
            return None
        normalized.append(list(dict.fromkeys(group)))
    return normalized, list(order)


def _project_stack_groups(
    groups: list[list[str]], order: list[str]
) -> Optional[tuple[list[list[int]], list[list[str]]]]:
    """Project distinct groups to ranges, or reject an unsafe representation."""
    order_index = {key: index for index, key in enumerate(order)}
    projected = []
    seen_members = set()
    for group in groups:
        group_members = set(group)
        if seen_members & group_members:
            return None
        seen_members.update(group_members)
        indices = sorted(order_index[key] for key in group_members)
        if indices != list(range(indices[0], indices[-1] + 1)):
            return None
        projected.append((indices[0], indices[-1], group_members))

    projected.sort(key=lambda item: item[0])
    for previous, current in zip(projected, projected[1:]):
        if current[0] <= previous[1] + 1:
            # Runtime ranges normalize adjacency, which would erase the
            # persisted boundary between these two logical groups.
            return None
    ranges = [[start, end] for start, end, _members in projected]
    ordered_groups = [
        [key for key in order[start : end + 1] if key in group_members]
        for start, end, group_members in projected
    ]
    return ranges, ordered_groups


def _stack_merge_fail_safe(
    identity: tuple[list[list[str]], list[str]], reason: str
) -> tuple[list[list[int]], list[list[str]], list[str]]:
    """Preserve lock-holder identities without inventing runtime ranges."""
    groups, order = identity
    projected = _project_stack_groups(groups, order)
    log.warning(
        "Concurrent stack merge could not preserve group boundaries (%s); "
        "preserving the lock holder's stack identities",
        reason,
    )
    ranges = projected[0] if projected is not None else []
    return ranges, copy.deepcopy(groups), copy.deepcopy(order)


def _merge_stack_payloads(
    base: dict, ours: dict, theirs: dict
) -> Optional[tuple[list[list[int]], list[list[str]], list[str]]]:
    """Three-way merge distinct stack groups by stable image identity."""
    base_identity = _valid_stack_identity(base)
    our_identity = _valid_stack_identity(ours)
    their_identity = _valid_stack_identity(theirs)
    if base_identity is None or our_identity is None or their_identity is None:
        return None

    base_groups, base_order = base_identity
    our_groups, our_order = our_identity
    their_groups, their_order = their_identity
    if our_order == base_order:
        merged_order = their_order
    elif their_order == base_order or our_order == their_order:
        merged_order = our_order
    elif set(our_order) == set(their_order):
        # Both processes sorted the same images differently. Identity still
        # makes membership safe; the lock holder's order is deterministic.
        merged_order = our_order
    else:
        return _stack_merge_fail_safe(
            our_identity,
            "image orders contain incompatible identities",
        )

    base_group_set = {frozenset(group) for group in base_groups}
    our_group_set = {frozenset(group) for group in our_groups}
    their_group_set = {frozenset(group) for group in their_groups}
    merged_group_set = set()
    for group in base_group_set | our_group_set | their_group_set:
        base_value = group in base_group_set
        our_value = group in our_group_set
        their_value = group in their_group_set
        if our_value == base_value:
            keep = their_value
        elif their_value == base_value or our_value == their_value:
            keep = our_value
        else:  # Boolean group presence cannot reach a genuine conflict.
            keep = our_value
        if keep:
            merged_group_set.add(group)

    merged_groups = [list(group) for group in merged_group_set]
    projected = _project_stack_groups(merged_groups, merged_order)
    if projected is None:
        # Overlapping concurrent edits to the same base group, a reordered
        # non-contiguous group, or newly adjacent distinct groups cannot be
        # expressed by FastStack's normalized runtime ranges. Lock-holder wins.
        return _stack_merge_fail_safe(
            our_identity,
            "merged groups overlap, are non-contiguous, or became adjacent",
        )
    ranges, ordered_groups = projected
    return ranges, ordered_groups, merged_order


def _merge_sidecar_payloads(base: dict, ours: dict, theirs: dict) -> dict:
    """Merge changes made since ``base`` into the latest disk payload."""
    merged = {}
    stack_merge = _merge_stack_payloads(base, ours, theirs)
    for key in base.keys() | ours.keys() | theirs.keys():
        if key == "entries":
            value = _merge_entries(
                base.get(key, {}),
                ours.get(key, {}),
                theirs.get(key, {}),
            )
        elif key in {"stacks", "stack_paths", "stack_order"} and stack_merge:
            value = {
                "stacks": stack_merge[0],
                "stack_paths": stack_merge[1],
                "stack_order": stack_merge[2],
            }[key]
        else:
            if key == "stacks" and stack_merge is None:
                base_stacks = base.get(key, _MISSING)
                our_stacks = ours.get(key, _MISSING)
                their_stacks = theirs.get(key, _MISSING)
                if (
                    our_stacks != base_stacks
                    and their_stacks != base_stacks
                    and our_stacks != their_stacks
                ):
                    log.warning(
                        "Concurrent legacy index-only stack edits cannot be merged "
                        "safely; preserving the lock holder's ranges"
                    )
            value = _select_three_way(
                base.get(key, _MISSING),
                ours.get(key, _MISSING),
                theirs.get(key, _MISSING),
            )
        if value is not _MISSING:
            merged[key] = value
    return merged


def _lock_sidecar_file(lock_file: BinaryIO) -> None:
    """Acquire an exclusive process lock on the first byte of ``lock_file``."""
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"\0")
        lock_file.flush()
    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)


def _unlock_sidecar_file(lock_file: BinaryIO) -> None:
    """Release the process lock acquired by ``_lock_sidecar_file``."""
    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _sidecar_write_lock(lock_path: Path) -> Iterator[None]:
    """Serialize read/merge/write transactions across FastStack processes."""
    with lock_path.open("a+b") as lock_file:
        _lock_sidecar_file(lock_file)
        try:
            yield
        finally:
            _unlock_sidecar_file(lock_file)


class SidecarManager:
    def __init__(self, directory: Path, watcher, debug: bool = False):
        self.directory = directory
        self.path = directory / "faststack.json"
        self.watcher = watcher
        self.debug = debug
        # Precomputed once: the case-normalized absolute base dir used by
        # metadata_key_for_path / _metadata_filename_key on every call.
        self._base_dir_normcased = Path(
            os.path.normcase(os.path.abspath(str(directory)))
        )
        # Bounded per-instance caches: input str → resolved key. Folder
        # refresh resolves the same paths repeatedly across bulk-map build,
        # flag filter, and grid entry construction.
        self._stable_key_cache: dict[str, str] = {}
        self._filename_key_cache: dict[str, str] = {}
        self._key_cache_max = 8192
        self._save_lock = threading.RLock()
        # Guards the structure of ``self.data`` (entry dict, stacks list, the
        # scalar position fields) so a background save cannot serialize or
        # merge-back a half-updated state while another thread is adding or
        # migrating entries. Never held across file I/O, and never held while
        # calling save() — save() takes _save_lock first, so acquiring the
        # locks in the other order would risk a deadlock.
        #
        # Thread contract: all mutation of ``self.data`` (and of EntryMetadata
        # objects returned by get_metadata()) happens on the main/Qt thread —
        # background executors (save, delete) do file I/O only and hand results
        # back via queued Qt signals, which AppController's slots then apply to
        # the sidecar on the main thread. This lock therefore only needs to
        # protect against save()'s own background merge-back, not against
        # concurrent callers mutating entries; it is not a substitute for
        # copy-on-read if that contract ever changes.
        self._state_lock = threading.RLock()
        self._write_lock_path = directory / "faststack.json.lock"
        # Identity of the disk file the baseline payload was taken from. Set by
        # load() and by every successful save; see _disk_matches_baseline.
        self._baseline_stamp: Optional[tuple[int, int, int]] = None
        self._load_failed = False
        self._dirty = False
        self._last_save_error: Optional[str] = None
        self._recovery_backup_notice: Optional[Path] = None
        self.data = self.load()
        # Three-way merge base: the disk state this process last incorporated.
        self._baseline_payload = _sidecar_to_json(self.data)

    def stop_watcher(self):
        if self.watcher:
            self.watcher.stop()

    def start_watcher(self):
        if self.watcher:
            self.watcher.start()

    def _disk_stamp(self) -> Optional[tuple[int, int, int]]:
        """Return a cheap identity for the on-disk sidecar, or None if absent.

        ``atomic_write_json`` replaces the file, so a write by any process
        changes mtime/size and, where the platform supports it, the inode.

        ``st_ino`` is only a bonus signal here: Windows network shares and some
        FAT/exFAT volumes report 0 or an unstable value. Both failure modes are
        safe — an inode that changes spuriously makes the stamp mismatch, which
        only costs us a full three-way merge, and an inode that is always 0
        simply leaves mtime+size doing the work. The stamp is never used to
        conclude that a foreign write *did* happen, only that none did.
        """
        try:
            st = self.path.stat()
        except OSError:
            return None
        ino = getattr(st, "st_ino", 0) or 0
        return (st.st_mtime_ns, st.st_size, ino)

    def _disk_matches_baseline(self) -> bool:
        """True when no other process has written the file since our baseline."""
        if self._baseline_stamp is None:
            return False
        stamp = self._disk_stamp()
        return stamp is not None and stamp == self._baseline_stamp

    def _read_disk_payload(self) -> Optional[dict]:
        """Return the current version-2 disk payload, or None if unusable.

        Mirrors ``load()``'s tolerance: a missing, malformed or pre-v2 file is
        reported as "nothing to merge against" rather than raising. Raising here
        would abort the save and, because the caller swallows the error, leave
        the bad file in place to break every subsequent save too.
        """
        try:
            with self.path.open("r") as f:
                payload = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as e:
            log.warning(f"Ignoring unreadable sidecar file {self.path}: {e}")
            return None
        if not isinstance(payload, dict):
            log.warning(f"Ignoring sidecar file {self.path}: root is not an object")
            return None
        if payload.get("version") != 2:
            log.warning(
                f"Ignoring sidecar file {self.path}: "
                f"unsupported version {payload.get('version')!r}"
            )
            return None
        return payload

    def load(self) -> Sidecar:
        """Loads sidecar data from disk if it exists, otherwise returns a new object."""
        try:
            t_start = time.perf_counter()
            # Stamp before reading: if a foreign write lands in between we keep
            # the older stamp and the next save falls back to a full merge,
            # which is the safe direction to be wrong in.
            self._baseline_stamp = self._disk_stamp()
            with self.path.open("r") as f:
                data = json.load(f)
            json_load_time = time.perf_counter() - t_start
            if not isinstance(data, dict):
                raise TypeError("sidecar root must be an object")

            if self.debug:
                log.info(
                    f"SidecarManager.load: loading sidecar took {json_load_time:.3f}s"
                )
            if data.get("version") != 2:
                log.warning("Old sidecar format detected. Starting fresh.")
                self._load_failed = True
                return Sidecar()

            return _sidecar_from_json(data)
        except FileNotFoundError:
            log.info(f"No sidecar file found at {self.path}. Creating new one.")
            self._baseline_stamp = None
            return Sidecar()
        except (OSError, json.JSONDecodeError, TypeError) as e:
            self._load_failed = True
            log.warning(
                "Could not read sidecar %s; using safe in-memory state and "
                "preserving the unreadable file: %s",
                self.path,
                e,
            )
            return Sidecar()

    @property
    def dirty(self) -> bool:
        """Whether state still needs a successful durable write."""
        return self._dirty

    @property
    def last_save_error(self) -> Optional[str]:
        return self._last_save_error

    def take_recovery_backup_notice(self) -> Optional[Path]:
        """Return and clear the unreadable-sidecar backup notification."""
        backup = self._recovery_backup_notice
        self._recovery_backup_notice = None
        return backup

    def save(self, *, replace_unreadable: bool = False) -> bool:
        """Merge and atomically persist state, returning durable success.

        A sidecar that could not be read is never overwritten without recovery.
        If a later read succeeds, normal merge/save resumes. Otherwise the
        first requested write preserves the unknown original under a unique
        backup name before atomically creating the new sidecar. The keyword is
        retained for API compatibility; recovery is automatic either way.
        """
        with self._save_lock:
            self._dirty = True
            try:
                with self._state_lock:
                    ours = _sidecar_to_json(self.data)
                recovered_payload = None
                with _sidecar_write_lock(self._write_lock_path):
                    if self._load_failed:
                        recovered_payload = self._read_disk_payload()
                        if recovered_payload is not None:
                            self._load_failed = False
                        elif self.path.exists():
                            recovery_backup = self.path.with_name(
                                f"{self.path.name}.unreadable-{time.time_ns()}.bak"
                            )
                            shutil.copy2(self.path, recovery_backup)
                            log.warning(
                                "Preserved unreadable sidecar as %s before recovery",
                                recovery_backup,
                            )
                            self._recovery_backup_notice = recovery_backup
                            self._load_failed = False
                        else:
                            self._load_failed = False

                    theirs = (
                        recovered_payload
                        if recovered_payload is not None
                        else (
                            None
                            if self._disk_matches_baseline()
                            else self._read_disk_payload()
                        )
                    )
                    if theirs is None:
                        # Either nobody has touched the file since our baseline,
                        # or what is there is unreadable/pre-v2 and load() has
                        # already discarded it. Both mean there is nothing to
                        # merge, so write our state straight out. This keeps a
                        # legacy or corrupt file from making every save a no-op.
                        atomic_write_json(self.path, ours)
                        self._baseline_payload = ours
                        self._baseline_stamp = self._disk_stamp()
                        self._dirty = False
                        self._last_save_error = None
                        log.debug(f"Saved sidecar file to {self.path}")
                        return True

                    merged = _merge_sidecar_payloads(
                        self._baseline_payload,
                        ours,
                        theirs,
                    )
                    atomic_write_json(self.path, merged)
                    self._baseline_stamp = self._disk_stamp()

                # Incorporate changes from other processes into this manager so
                # a later save cannot mistake them for local deletions.
                merged_data = _sidecar_from_json(merged)
                with self._state_lock:
                    self.data.version = merged_data.version
                    self.data.last_index = merged_data.last_index
                    self.data.last_path = merged_data.last_path
                    self.data.sort_mode = merged_data.sort_mode
                    # Mutate in place: AppController aliases this list as
                    # ``self.stacks``, so rebinding it would hide merged-in
                    # stacks from the UI and let the next save overwrite them.
                    self.data.stacks[:] = merged_data.stacks
                    self.data.stack_paths[:] = merged_data.stack_paths
                    self.data.stack_order[:] = merged_data.stack_order
                    for key in list(self.data.entries):
                        if key not in merged_data.entries:
                            del self.data.entries[key]
                    for key, merged_meta in merged_data.entries.items():
                        current_meta = self.data.entries.get(key)
                        if current_meta is None:
                            self.data.entries[key] = merged_meta
                            continue
                        for field in dataclasses.fields(EntryMetadata):
                            setattr(
                                current_meta,
                                field.name,
                                copy.deepcopy(getattr(merged_meta, field.name)),
                            )

                    self._baseline_payload = _sidecar_to_json(self.data)
                log.debug(f"Merged and saved sidecar file to {self.path}")
                self._dirty = False
                self._last_save_error = None
                return True

            except (OSError, TypeError, ValueError, json.JSONDecodeError) as e:
                self._last_save_error = str(e)
                log.error(f"Failed to merge or save sidecar file {self.path}: {e}")
                return False

    @overload
    def get_metadata(
        self,
        image_ref: Union[str, Path],
        *,
        create: Literal[True] = True,
        migrate: bool = True,
    ) -> EntryMetadata: ...

    @overload
    def get_metadata(
        self,
        image_ref: Union[str, Path],
        *,
        create: Literal[False],
        migrate: bool = True,
    ) -> Optional[EntryMetadata]: ...

    @overload
    def get_metadata(
        self, image_ref: Union[str, Path], *, create: bool, migrate: bool = True
    ) -> Optional[EntryMetadata]: ...

    def get_metadata(
        self,
        image_ref: Union[str, Path],
        *,
        create: bool = True,
        migrate: bool = True,
    ) -> Optional[EntryMetadata]:
        """Get metadata for an image, optionally creating a persistent entry.

        When create=True (default), always returns an EntryMetadata (creating
        and storing one if it doesn't exist).  When create=False, returns None
        if no entry exists — callers must handle the None case explicitly.

        ``migrate=False`` skips the legacy-key migration scan, which walks
        EVERY sidecar entry with per-entry filesystem checks. That scan runs
        on every lookup miss, so bulk read-only callers (grid refresh,
        bulk metadata maps) — which miss for most images in a folder — must
        disable it or pay O(images x entries) filesystem stats per refresh.
        User-action paths keep migrate=True so ancient entries still get
        migrated when an image is actually viewed or modified.
        """
        stable_key, candidate_keys = self._lookup_keys(image_ref)
        if not stable_key:
            if create:
                raise ValueError(f"image_ref must not be empty: {image_ref!r}")
            return None

        with self._state_lock:
            meta = self.data.entries.get(stable_key)
            if meta is None:
                for candidate_key in candidate_keys:
                    if candidate_key == stable_key:
                        continue
                    candidate_meta = self.data.entries.get(candidate_key)
                    if candidate_meta is None:
                        continue
                    meta = candidate_meta
                    if stable_key not in self.data.entries:
                        self.data.entries[stable_key] = candidate_meta
                    if (
                        candidate_key in self.data.entries
                        and candidate_key != stable_key
                    ):
                        del self.data.entries[candidate_key]
                    break
            if meta is None and migrate:
                for existing_key, existing_meta in list(self.data.entries.items()):
                    if existing_key == stable_key:
                        continue
                    if (
                        self._stable_key_from_key(existing_key, check_fs=True)
                        != stable_key
                    ):
                        continue
                    meta = existing_meta
                    self.data.entries[stable_key] = existing_meta
                    del self.data.entries[existing_key]
                    break

            if meta is None and create:
                meta = EntryMetadata()
                self.data.entries[stable_key] = meta
        return meta

    def metadata_key_for_path(self, image_path: Union[str, Path]) -> str:
        """Return the stable sidecar key for a concrete image path."""
        cache_key = str(image_path)
        cached = self._stable_key_cache.get(cache_key)
        if cached is not None:
            return cached

        path = Path(image_path)
        if not path.name:
            return ""
        if not path.is_absolute():
            path = self.directory / path
        abs_path = Path(os.path.normcase(os.path.abspath(str(path))))

        try:
            relative = abs_path.relative_to(self._base_dir_normcased)
            stable_path = relative.parent / relative.stem
            result = str(stable_path).replace("\\", "/")
        except ValueError:
            stable_path = abs_path.parent / abs_path.stem
            result = str(stable_path).replace("\\", "/")

        if len(self._stable_key_cache) >= self._key_cache_max:
            del self._stable_key_cache[next(iter(self._stable_key_cache))]
        self._stable_key_cache[cache_key] = result
        return result

    def _lookup_keys(self, image_ref: Union[str, Path]) -> tuple[str, list[str]]:
        """Return (stable_key, migration_candidate_keys) for a metadata lookup."""
        if isinstance(image_ref, Path):
            if not image_ref.name:
                return "", []
            stable_key = self.metadata_key_for_path(image_ref)
            full_name_key = self._metadata_filename_key(image_ref)
            return stable_key, [full_name_key, image_ref.stem]

        value = str(image_ref)
        if not value:
            return "", []

        # Only treat string as a path if it contains explicit path separators.
        # Dotted strings (even with image extensions like "photo.CR2") are
        # treated as exact keys — migration of legacy filename keys is handled
        # by the _stable_key_from_key scan in get_metadata.
        if os.path.sep in value or "/" in value or "\\" in value:
            path = Path(value)
            stable_key = self.metadata_key_for_path(path)
            full_name_key = self._metadata_filename_key(path)
            return stable_key, [full_name_key, path.stem]

        return value, [value]

    def _metadata_filename_key(self, image_path: Union[str, Path]) -> str:
        """Return the extension-preserving key used by the regressed patch."""
        cache_key = str(image_path)
        cached = self._filename_key_cache.get(cache_key)
        if cached is not None:
            return cached

        path = Path(image_path)
        if not path.name:
            return ""
        if not path.is_absolute():
            path = self.directory / path
        abs_path = Path(os.path.normcase(os.path.abspath(str(path))))

        try:
            relative = abs_path.relative_to(self._base_dir_normcased)
            result = str(relative).replace("\\", "/")
        except ValueError:
            result = str(abs_path).replace("\\", "/")

        if len(self._filename_key_cache) >= self._key_cache_max:
            del self._filename_key_cache[next(iter(self._filename_key_cache))]
        self._filename_key_cache[cache_key] = result
        return result

    def _stable_key_from_key(self, key: str, check_fs: bool = False) -> str:
        """Convert any historical sidecar key form into today's stable key.

        Args:
            key: The sidecar key to normalize.
            check_fs: If True, check the filesystem for bare-stem keys that
                match an existing file. Set to True during one-time migration
                scans; leave False on hot paths to avoid filesystem I/O.
        """
        if not key:
            return ""
        if (
            os.path.sep in key
            or "/" in key
            or "\\" in key
            or Path(key).suffix.lower() in KNOWN_IMAGE_EXTENSIONS
        ):
            return self.metadata_key_for_path(Path(key))
        if check_fs:
            candidate_path = self.directory / key
            if candidate_path.exists():
                return self.metadata_key_for_path(candidate_path)
        return key

    def set_last_index(self, index: int):
        with self._state_lock:
            self.data.last_index = index

    def set_last_position(self, index: int, image_path: Optional[Path]) -> None:
        """Store an index fallback plus a relocatable path within this folder."""
        if image_path is None:
            with self._state_lock:
                self.data.last_index = index
                self.data.last_path = None
            return
        path = Path(image_path)
        try:
            last_path = path.relative_to(self.directory).as_posix()
        except ValueError:
            last_path = str(path)
        with self._state_lock:
            self.data.last_index = index
            self.data.last_path = last_path

    def update_metadata(self, image_ref: Union[str, Path], updates: dict) -> bool:
        """Update multiple metadata fields for an image and save if changed."""
        meta = self.get_metadata(image_ref, create=True)
        changed = False
        for key, value in updates.items():
            if hasattr(meta, key):
                if getattr(meta, key) != value:
                    setattr(meta, key, value)
                    changed = True
            else:
                log.warning(f"Unknown metadata key: {key}")

        if changed:
            return self.save()
        return True
