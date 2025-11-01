"""Manages reading and writing the faststack.json sidecar file."""

import json
import logging
from pathlib import Path
from typing import Optional

from faststack.models import Sidecar, EntryMetadata

log = logging.getLogger(__name__)

class SidecarManager:
    def __init__(self, directory: Path):
        self.path = directory / "faststack.json"
        self.data = self.load()

    def load(self) -> Sidecar:
        """Loads sidecar data from disk if it exists, otherwise returns a new object."""
        if not self.path.exists():
            log.info(f"No sidecar file found at {self.path}. Creating new one.")
            return Sidecar()
        try:
            with self.path.open("r") as f:
                data = json.load(f)
                # Reconstruct nested objects
                entries = { 
                    stem: EntryMetadata(**meta) 
                    for stem, meta in data.get("entries", {}).items()
                }
                return Sidecar(
                    version=data.get("version", 1),
                    last_index=data.get("last_index", 0),
                    entries=entries,
                )
        except (json.JSONDecodeError, TypeError) as e:
            log.error(f"Failed to load or parse sidecar file {self.path}: {e}")
            # Consider backing up the corrupted file here
            return Sidecar()

    def save(self):
        """Saves the sidecar data to disk atomically."""
        temp_path = self.path.with_suffix(".tmp")
        try:
            with temp_path.open("w") as f:
                # Convert to a dict that json.dump can handle
                serializable_data = {
                    "version": self.data.version,
                    "last_index": self.data.last_index,
                    "entries": {
                        stem: meta.__dict__
                        for stem, meta in self.data.entries.items()
                    }
                }
                json.dump(serializable_data, f, indent=2)
            
            # Atomic rename
            temp_path.rename(self.path)
            log.debug(f"Saved sidecar file to {self.path}")

        except (IOError, TypeError) as e:
            log.error(f"Failed to save sidecar file {self.path}: {e}")

    def get_metadata(self, image_stem: str) -> EntryMetadata:
        """Gets metadata for an image, creating it if it doesn't exist."""
        return self.data.entries.setdefault(image_stem, EntryMetadata())

    def set_last_index(self, index: int):
        self.data.last_index = index
