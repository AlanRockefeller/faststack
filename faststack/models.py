"""Core data types and enumerations for FastStack."""

import dataclasses
import hashlib
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


def filesystem_source_identity(stat_result: os.stat_result) -> str:
    """Return a cheap stable token for the filesystem object behind an image.

    This deliberately avoids content hashing. A same-size in-place rewrite
    that preserves every cheap timestamp/file-id field (possible on Windows)
    cannot be detected by this scheme and still requires manual refresh.
    """
    fields = (
        getattr(stat_result, "st_mtime_ns", 0),
        getattr(stat_result, "st_size", 0),
        getattr(stat_result, "st_ino", 0),
        getattr(stat_result, "st_ctime_ns", 0),
    )
    payload = ":".join(str(value or 0) for value in fields).encode("ascii")
    return hashlib.blake2s(payload, digest_size=8).hexdigest()


@dataclasses.dataclass
class ImageFile:
    """Represents a single image file on disk."""

    path: Path
    raw_pair: Optional[Path] = None
    timestamp: float = 0.0
    # Casefolded name used as the primary sort key.  For developed images this
    # is the *base* image's name (so the pair sorts adjacently); for everything
    # else it defaults to None which means "use path.name.casefold()".
    sort_name_cf: Optional[str] = None
    has_backups: bool = False
    has_developed: bool = False
    # Stat details captured by the folder scan so the thumbnail grid does not
    # stat every image again on the UI thread. None means "unknown, stat it".
    # The controller clears both when it rewrites the file itself.
    mtime_ns: Optional[int] = None
    source_identity: Optional[str] = None

    @property
    def raw_path(self) -> Optional[Path]:
        """Returns the path to the RAW file if it exists, otherwise None."""
        if self.raw_pair:
            return self.raw_pair
        # If the main path itself is a RAW file (orphaned RAW case)
        # We need a way to check if 'path' is a raw extension.
        # Ideally we check against known extensions, but for now let's assume
        # if raw_pair is None but we are treating it as RAW, we might need logic here.
        # However, the indexer will set raw_pair = path for orphaned RAWs likely.
        return None

    @property
    def has_raw(self) -> bool:
        """Returns True if a RAW file is associated with this image."""
        return self.raw_pair is not None

    @property
    def working_tif_path(self) -> Path:
        """Canonical path for the working 16-bit TIFF: stem + -working.tif"""
        return self.path.parent / f"{self.path.stem}-working.tif"

    @property
    def has_working_tif(self) -> bool:
        """Returns True if a valid working TIFF file exists on disk."""
        try:
            return (
                self.working_tif_path.exists()
                and self.working_tif_path.stat().st_size > 0
            )
        except OSError:
            return False

    @property
    def developed_jpg_path(self) -> Path:
        """Canonical path for the developed JPG: stem + -developed.jpg"""
        # If the original path is 'photo.jpg', we want 'photo-developed.jpg'.
        # If 'photo.CR2', we want 'photo-developed.jpg'.
        return self.path.with_name(f"{self.path.stem}-developed.jpg")


# pylint: disable=too-many-instance-attributes
@dataclasses.dataclass
class EntryMetadata:
    """Flat sidecar metadata for a single image entry (mirrors JSON schema)."""

    stack_id: Optional[int] = None
    stacked: bool = False
    stacked_date: Optional[str] = None
    uploaded: bool = False
    uploaded_date: Optional[str] = None
    edited: bool = False
    edited_date: Optional[str] = None
    edit_state: Optional[Dict[str, Any]] = None
    restacked: bool = False
    restacked_date: Optional[str] = None
    favorite: bool = False
    todo: bool = False
    todo_date: Optional[str] = None
    batch: bool = False


@dataclasses.dataclass
class Sidecar:
    """Represents the entire sidecar JSON file."""

    version: int = 2
    last_index: int = 0
    last_path: Optional[str] = None
    sort_mode: Optional[str] = None
    entries: Dict[str, EntryMetadata] = dataclasses.field(default_factory=dict)
    stacks: List[List[int]] = dataclasses.field(default_factory=list)
    # Stable identities backing ``stacks`` for cross-process merge/remapping.
    stack_paths: List[List[str]] = dataclasses.field(default_factory=list)
    stack_order: List[str] = dataclasses.field(default_factory=list)
    # Extensionless metadata from old releases is normally migrated to the
    # concrete JPEG that owns the stem.  If multiple JPEG extensions owned the
    # stem when migration was first attempted, remember that ambiguity so the
    # metadata cannot silently attach to whichever file happens to survive.
    ambiguous_legacy_keys: List[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class DecodedImage:
    """A decoded image buffer ready for display."""

    buffer: memoryview
    width: int
    height: int
    bytes_per_line: int
    format: Any  # QImage.Format
    quality: str = "cover"  # "fast" navigation decode or settled "cover" decode
    native_width: int = 0
    native_height: int = 0
    # Provenance is populated by the display decoder. It lets the provider
    # verify that the buffer it is about to expose belongs to the requested
    # file instead of reconstructing that answer from the current index.
    source_path: Optional[str] = None
    cache_key: Optional[str] = None
    # Present only for debug-cache runs. The same dictionary is completed by
    # the worker after cache insertion, so a later cache hit retains the exact
    # task/queue/decode history of the pixels being served.
    decode_trace: Optional[Dict[str, Any]] = None
    is_placeholder: bool = False
    placeholder_reason: Optional[str] = None

    def __sizeof__(self) -> int:
        """Returns the retained size of this frame in bytes.

        Production cache budgeting goes through
        ``faststack.imaging.cache.get_decoded_image_size`` rather than this
        method; the two are kept in agreement so neither can under-report.
        """
        return self.buffer.nbytes
