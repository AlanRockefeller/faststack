"""Core data types and enumerations for FastStack."""

import dataclasses
from pathlib import Path
from typing import Any, Optional, Dict, List


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
    restacked: bool = False
    restacked_date: Optional[str] = None
    favorite: bool = False
    todo: bool = False
    todo_date: Optional[str] = None


@dataclasses.dataclass
class Sidecar:
    """Represents the entire sidecar JSON file."""

    version: int = 2
    last_index: int = 0
    entries: Dict[str, EntryMetadata] = dataclasses.field(default_factory=dict)
    stacks: List[List[int]] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class DecodedImage:
    """A decoded image buffer ready for display."""

    buffer: memoryview
    width: int
    height: int
    bytes_per_line: int
    format: Any  # QImage.Format

    def __sizeof__(self) -> int:
        """Returns the size of the image buffer in bytes."""
        return self.buffer.nbytes
