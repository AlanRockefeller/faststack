"""Tests for the SidecarManager."""

import json
import os
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
            "IMG_0001": {"flag": True, "reject": False, "stack_id": 1},
            "IMG_0002": {"flag": False, "reject": True, "stack_id": None},
        },
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
    meta = sm.get_metadata(Path("IMG_TEST.jpg"))
    # Modify a valid field
    meta.stack_id = 99

    # Save
    sm.save()

    # Verify file content
    saved_data = json.loads((d / "faststack.json").read_text())
    expected_key = sm.metadata_key_for_path(Path("IMG_TEST.jpg"))
    assert saved_data["last_index"] == 10
    assert saved_data["entries"][expected_key]["stack_id"] == 99


def test_sidecar_get_metadata_creates_new(mock_sidecar_dir):
    """Tests that get_metadata creates a new entry if one doesn't exist."""
    d = mock_sidecar_dir()
    sm = SidecarManager(d, None)
    expected_key = sm.metadata_key_for_path(Path("NEW_IMG.jpg"))
    assert expected_key not in sm.data.entries
    meta = sm.get_metadata(Path("NEW_IMG.jpg"))

    # EntryMetadata may be a runtime class OR a typing alias, depending on refactors.
    if isinstance(EntryMetadata, type):
        assert isinstance(meta, EntryMetadata)
    else:
        # Fallback: validate by name + expected attributes.
        assert meta.__class__.__name__ == "EntryMetadata"
        assert hasattr(meta, "stack_id")

    assert expected_key in sm.data.entries


def test_favorite_toggle_sets_json(mock_sidecar_dir):
    """Tests that toggling favorite writes true/false to JSON."""
    d = mock_sidecar_dir()
    sm = SidecarManager(d, None)
    meta = sm.get_metadata(Path("IMG_FAV.jpg"))

    # Initially false
    assert meta.favorite is False

    # Toggle on
    meta.favorite = True
    sm.save()
    saved = json.loads((d / "faststack.json").read_text())
    expected_key = sm.metadata_key_for_path(Path("IMG_FAV.jpg"))
    assert saved["entries"][expected_key]["favorite"] is True

    # Toggle off
    meta.favorite = False
    sm.save()
    saved = json.loads((d / "faststack.json").read_text())
    assert saved["entries"][expected_key]["favorite"] is False


def test_favorite_loads_from_sidecar(mock_sidecar_dir):
    """Tests that favorite loads correctly when reopening sidecar."""
    content = {
        "version": 2,
        "last_index": 0,
        "entries": {
            "IMG_FAV.jpg": {"favorite": True},
        },
    }
    d = mock_sidecar_dir(content)
    sm = SidecarManager(d, None)
    meta = sm.get_metadata(Path("IMG_FAV.jpg"))
    assert meta.favorite is True


def test_favorite_toggle_roundtrip(mock_sidecar_dir):
    """Tests that toggling twice restores original JSON (round-trip)."""
    d = mock_sidecar_dir()
    sm = SidecarManager(d, None)
    meta = sm.get_metadata(Path("IMG_FAV.jpg"))

    # Capture original state
    assert meta.favorite is False

    # Toggle on then off
    meta.favorite = True
    meta.favorite = False
    sm.save()

    # Reload and verify
    sm2 = SidecarManager(d, None)
    meta2 = sm2.get_metadata(Path("IMG_FAV.jpg"))
    assert meta2.favorite is False


def test_legacy_stem_entry_migrates_to_path_key(mock_sidecar_dir):
    """Legacy stem-keyed entries should migrate on first concrete path lookup."""
    content = {
        "version": 2,
        "entries": {
            "IMG_0001": {"uploaded": True},
        },
    }
    d = mock_sidecar_dir(content)
    sm = SidecarManager(d, None)

    meta = sm.get_metadata(Path("IMG_0001.jpg"), create=False)
    expected_key = sm.metadata_key_for_path(Path("IMG_0001.jpg"))

    assert meta is not None
    assert meta.uploaded is True
    assert expected_key in sm.data.entries
    assert "IMG_0001.jpg" not in sm.data.entries
    if os.name == "nt":
        assert expected_key == "img_0001"
    else:
        assert expected_key == "IMG_0001"


def test_raw_only_entry_survives_transition_to_visible_jpg(mock_sidecar_dir):
    """RAW-only metadata must remain visible after a matching JPG appears."""
    d = mock_sidecar_dir()
    sm = SidecarManager(d, None)

    raw_meta = sm.get_metadata(Path("photo.CR2"))
    raw_meta.favorite = True
    raw_meta.uploaded = True

    jpg_meta = sm.get_metadata(Path("photo.jpg"), create=False)

    assert jpg_meta is raw_meta
    assert jpg_meta.favorite is True
    assert jpg_meta.uploaded is True
    assert list(sm.data.entries) == ["photo"]


def test_regressed_filename_key_migrates_to_stable_stem_key(mock_sidecar_dir):
    """Entries created by the filename-key regression should migrate back."""
    content = {
        "version": 2,
        "entries": {
            "photo.CR2": {"favorite": True},
        },
    }
    d = mock_sidecar_dir(content)
    sm = SidecarManager(d, None)

    meta = sm.get_metadata(Path("photo.jpg"), create=False)

    assert meta is not None
    assert meta.favorite is True
    assert "photo" in sm.data.entries
    assert "photo.CR2" not in sm.data.entries
    assert "photo.jpg" not in sm.data.entries


def test_same_stem_in_different_subfolders_do_not_collide(mock_sidecar_dir):
    """Relative parent path must namespace same-stem files in different folders."""
    d = mock_sidecar_dir()
    sm = SidecarManager(d, None)

    first = sm.get_metadata(Path("a") / "photo.CR2")
    first.favorite = True

    second = sm.get_metadata(Path("b") / "photo.jpg")
    second.uploaded = True

    assert first is not second
    assert sm.metadata_key_for_path(Path("a") / "photo.CR2") == "a/photo"
    assert sm.metadata_key_for_path(Path("b") / "photo.jpg") == "b/photo"
    assert sm.data.entries["a/photo"].favorite is True
    assert sm.data.entries["a/photo"].uploaded is False
    assert sm.data.entries["b/photo"].favorite is False
    assert sm.data.entries["b/photo"].uploaded is True


def test_semantic_dot_key_is_not_treated_as_path(mock_sidecar_dir):
    """Semantic keys like sample.v1 must not be truncated as filenames."""
    d = mock_sidecar_dir()
    sm = SidecarManager(d, None)
    legacy = EntryMetadata(todo=True)
    sm.data.entries["sample.v1"] = legacy

    meta = sm.get_metadata("sample.v1", create=False)

    assert meta is legacy
    assert "sample.v1" in sm.data.entries
    assert "sample" not in sm.data.entries


def test_empty_string_create_false_returns_none(mock_sidecar_dir):
    """Empty string refs must not create a sidecar entry."""
    d = mock_sidecar_dir()
    sm = SidecarManager(d, None)

    assert sm.get_metadata("", create=False) is None
    assert "" not in sm.data.entries


def test_empty_string_create_true_raises(mock_sidecar_dir):
    """Empty string refs should fail fast when asked to create metadata."""
    d = mock_sidecar_dir()
    sm = SidecarManager(d, None)

    with pytest.raises(ValueError, match="image_ref must not be empty"):
        sm.get_metadata("", create=True)

    assert "" not in sm.data.entries


def test_empty_path_create_false_returns_none(mock_sidecar_dir):
    """Path('') should be treated the same as an empty string."""
    d = mock_sidecar_dir()
    sm = SidecarManager(d, None)

    assert sm.get_metadata(Path(""), create=False) is None
    assert "" not in sm.data.entries


def test_empty_path_create_true_raises(mock_sidecar_dir):
    """Path('') should fail fast when asked to create metadata."""
    d = mock_sidecar_dir()
    sm = SidecarManager(d, None)

    with pytest.raises(ValueError, match="image_ref must not be empty"):
        sm.get_metadata(Path(""), create=True)

    assert "" not in sm.data.entries


def test_metadata_key_for_path_normalizes_case_on_windows(mock_sidecar_dir):
    """Windows path casing should not create duplicate stable keys."""
    d = mock_sidecar_dir()
    sm = SidecarManager(d, None)

    key_upper = sm.metadata_key_for_path(Path("Mixed/Case/IMG_0001.JPG"))
    key_lower = sm.metadata_key_for_path(Path("mixed/case/img_0001.jpg"))

    if os.name == "nt":
        assert key_upper == key_lower
    else:
        assert key_upper != ""
        assert key_lower != ""
