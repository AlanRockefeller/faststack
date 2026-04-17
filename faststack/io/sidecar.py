"""Manages reading and writing the faststack.json sidecar file."""

import json
import logging
import os
import time
from pathlib import Path
from typing import Literal, Optional, Union, overload

from faststack.io.indexer import JPG_EXTENSIONS, RAW_EXTENSIONS
from faststack.models import EntryMetadata, Sidecar

log = logging.getLogger(__name__)
KNOWN_IMAGE_EXTENSIONS = frozenset(
    ext.lower() for ext in JPG_EXTENSIONS | RAW_EXTENSIONS
)


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
        import dataclasses

        valid_keys = {f.name for f in dataclasses.fields(EntryMetadata)}
        filtered_meta = {k: v for k, v in meta.items() if k in valid_keys}

        return EntryMetadata(**filtered_meta)
    except Exception as e:
        log.warning(f"Error parsing metadata entry: {e}")
        return EntryMetadata()


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
        self.data = self.load()

    def stop_watcher(self):
        if self.watcher:
            self.watcher.stop()

    def start_watcher(self):
        if self.watcher:
            self.watcher.start()

    def load(self) -> Sidecar:
        """Loads sidecar data from disk if it exists, otherwise returns a new object."""
        if not self.path.exists():
            log.info(f"No sidecar file found at {self.path}. Creating new one.")
            return Sidecar()
        try:
            t_start = time.perf_counter()
            with self.path.open("r") as f:
                data = json.load(f)
            json_load_time = time.perf_counter() - t_start

            if self.debug:
                log.info(
                    f"SidecarManager.load: loading sidecar took {json_load_time:.3f}s"
                )
            if data.get("version") != 2:
                log.warning("Old sidecar format detected. Starting fresh.")
                return Sidecar()

            # Reconstruct nested objects
            entries = {
                key: _entrymetadata_from_json(meta)
                for key, meta in data.get("entries", {}).items()
            }
            return Sidecar(
                version=data.get("version", 2),
                last_index=data.get("last_index", 0),
                entries=entries,
                stacks=data.get("stacks", []),
            )
        except (json.JSONDecodeError, TypeError) as e:
            log.error(f"Failed to load or parse sidecar file {self.path}: {e}")
            # Consider backing up the corrupted file here
            return Sidecar()

    def save(self):
        """Saves the sidecar data to disk atomically."""
        temp_path = self.path.with_suffix(".tmp")
        was_watcher_running = False
        try:
            if (
                self.watcher
                and hasattr(self.watcher, "is_alive")
                and self.watcher.is_alive()
            ):
                self.stop_watcher()
                was_watcher_running = True
            with temp_path.open("w") as f:
                # Convert to a dict that json.dump can handle
                serializable_data = {
                    "version": self.data.version,
                    "last_index": self.data.last_index,
                    "entries": {
                        key: meta.__dict__ for key, meta in self.data.entries.items()
                    },
                    "stacks": self.data.stacks,
                }
                json.dump(serializable_data, f, indent=2)

            # Atomic rename
            temp_path.replace(self.path)
            log.debug(f"Saved sidecar file to {self.path}")

        except (IOError, TypeError) as e:
            log.error(f"Failed to save sidecar file {self.path}: {e}")
        finally:
            if was_watcher_running:
                self.start_watcher()

    @overload
    def get_metadata(
        self, image_ref: Union[str, Path], *, create: Literal[True] = True
    ) -> EntryMetadata: ...

    @overload
    def get_metadata(
        self, image_ref: Union[str, Path], *, create: Literal[False]
    ) -> Optional[EntryMetadata]: ...

    @overload
    def get_metadata(
        self, image_ref: Union[str, Path], *, create: bool
    ) -> Optional[EntryMetadata]: ...

    def get_metadata(
        self, image_ref: Union[str, Path], *, create: bool = True
    ) -> Optional[EntryMetadata]:
        """Get metadata for an image, optionally creating a persistent entry.

        When create=True (default), always returns an EntryMetadata (creating
        and storing one if it doesn't exist).  When create=False, returns None
        if no entry exists — callers must handle the None case explicitly.
        """
        stable_key, candidate_keys = self._lookup_keys(image_ref)
        if not stable_key:
            if create:
                raise ValueError(f"image_ref must not be empty: {image_ref!r}")
            return None

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
                if candidate_key in self.data.entries and candidate_key != stable_key:
                    del self.data.entries[candidate_key]
                break
        if meta is None:
            for existing_key, existing_meta in list(self.data.entries.items()):
                if existing_key == stable_key:
                    continue
                if self._stable_key_from_key(existing_key, check_fs=True) != stable_key:
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
        self.data.last_index = index

    def update_metadata(self, image_ref: Union[str, Path], updates: dict):
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
            self.save()
