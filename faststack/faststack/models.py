"""Core data types and enumerations for FastStack."""

import dataclasses
from pathlib import Path
from typing import Optional, Dict, List

@dataclasses.dataclass
class ImageFile:
    """Represents a single image file on disk."""
    path: Path
    raw_pair: Optional[Path] = None
    timestamp: float = 0.0

@dataclasses.dataclass
class EntryMetadata:
    """Sidecar metadata for a single image entry."""
    flag: bool = False
    reject: bool = False


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
    format: object # QImage.Format

    def __sizeof__(self) -> int:
        return self.buffer.nbytes
