"""Core data types and enumerations for FastStack."""

import dataclasses
from pathlib import Path
from typing import Any, Dict, List, Optional


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
class ResolvedDarkenMask:
    """The background-darkening mask one render actually applied.

    Published by ``ImageEditor._apply_edits`` at pipeline step 19.5 and carried
    on the ``DecodedImage`` produced by that same pass, so the overlay and the
    pixels underneath it are one atomic result: a frame can never be shown with
    a mask resolved by a different render.

    **Ownership.** ``mask`` is the array ``resolve_mask`` allocated (a fresh
    ``np.clip`` output) and handed to ``MaskRasterCache``. The cache only ever
    rebinds that slot, never writes through it, and ``apply_masked_darken``
    reads it without mutating. It is published read-only
    (``flags.writeable = False``) so the invariant fails loudly instead of
    silently corrupting a displayed overlay, which lets the worker share it
    with the GUI thread with no copy.

    ``width``/``height`` are the mask's own dimensions — the render tier that
    produced it, which is the drag tier during a slider drag and the display
    tier after refinement. They need not match a ``DecodedImage`` that was
    later resized for display.
    """

    mask: Any  # np.ndarray float32 (H, W) in [0, 1]; typed loosely to keep
    # numpy off models.py's import path.
    width: int
    height: int
    mask_id: str
    mask_revision: int


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
    # The darkening mask resolved by the render that produced `buffer`, when
    # background darkening was active. Lets the overlay be built without a
    # second pre-darken render. See ResolvedDarkenMask.
    darken_mask: Optional[ResolvedDarkenMask] = None

    def __sizeof__(self) -> int:
        """Returns the retained size of this frame in bytes.

        Includes the published darkening mask, which is a float32 plane 4/3 the
        size of the RGB888 buffer. Production cache budgeting goes through
        ``faststack.imaging.cache.get_decoded_image_size`` rather than this
        method; the two are kept in agreement so neither can under-report.
        """
        mask = getattr(self.darken_mask, "mask", None)
        nbytes = getattr(mask, "nbytes", None)
        mask_bytes = nbytes if isinstance(nbytes, int) and nbytes > 0 else 0
        return self.buffer.nbytes + mask_bytes
