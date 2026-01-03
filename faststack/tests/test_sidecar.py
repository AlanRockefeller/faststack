"""Tests for the SidecarManager."""

import json
from pathlib import Path

import pytest

from faststack.io.sidecar import SidecarManager
from faststack.models import EntryMetadata

@pytest.fixture
def mock_sidecar_dir(tmp_path: Path):
    """Creates a temp dir and can pre-populate a sidecar file."""
    def _create(content: dict = None):
        if content:
            (tmp_path / "faststack.json").write_text(json.dumps(content))
        return tmp_path
    return _create

def test_sidecar_load_non_existent(mock_sidecar_dir):
    """Tests loading when no sidecar file exists."""
    d = mock_sidecar_dir()
    sm = SidecarManager(d, None)
    assert sm.data.version == 2
    assert sm.data.last_index == 0
    assert not sm.data.entries

def test_sidecar_load_existing(mock_sidecar_dir):
    """Tests loading a valid, existing sidecar file."""
    content = {
        "version": 2,
        "last_index": 42,
        "entries": {
            "IMG_0001": { "flag": True, "reject": False, "stack_id": 1 },
            "IMG_0002": { "flag": False, "reject": True, "stack_id": None },
        }
    }
    d = mock_sidecar_dir(content)
    sm = SidecarManager(d, None)

    assert sm.data.last_index == 42
    assert len(sm.data.entries) == 2
    
    # flag and reject are legacy and not in current model, so they are dropped.
    # stack_id IS in the current model, so it should be preserved.
    assert sm.data.entries["IMG_0001"].stack_id == 1
    
    # IMG_0002 has stack_id=None
    assert sm.data.entries["IMG_0002"].stack_id is None

def test_sidecar_save(mock_sidecar_dir):
    """Tests saving data back to the JSON file."""
    d = mock_sidecar_dir()
    sm = SidecarManager(d, None)

    # Modify data
    sm.set_last_index(10)
    meta = sm.get_metadata("IMG_TEST")
    # Modify a valid field
    meta.stack_id = 99
    
    # Save
    sm.save()

    # Verify file content
    saved_data = json.loads((d / "faststack.json").read_text())
    assert saved_data["last_index"] == 10
    assert saved_data["entries"]["IMG_TEST"]["stack_id"] == 99

def test_sidecar_get_metadata_creates_new(mock_sidecar_dir):
    """Tests that get_metadata creates a new entry if one doesn't exist."""
    d = mock_sidecar_dir()
    sm = SidecarManager(d, None)
    assert "NEW_IMG" not in sm.data.entries
    meta = sm.get_metadata("NEW_IMG")
    assert isinstance(meta, EntryMetadata)
    assert "NEW_IMG" in sm.data.entries
