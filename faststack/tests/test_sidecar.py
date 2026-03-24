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
    """Legacy filename-keyed entries should migrate on first concrete path lookup."""
    # Use a legacy key ("IMG_0001.jpg") that differs from the stable key
    # returned by metadata_key_for_path (which strips the extension), so that
    # get_metadata must actually perform migration.
    legacy_key = "IMG_0001.jpg"
    content = {
        "version": 2,
        "entries": {
            legacy_key: {"uploaded": True},
        },
    }
    d = mock_sidecar_dir(content)
    sm = SidecarManager(d, None)

    expected_key = sm.metadata_key_for_path(Path("IMG_0001.jpg"))
    assert expected_key != legacy_key, "legacy key must differ from stable key"

    meta = sm.get_metadata(Path("IMG_0001.jpg"), create=False)

    assert meta is not None
    assert meta.uploaded is True
    # Entry now lives under the stable key
    assert expected_key in sm.data.entries
    # Legacy key was removed during migration
    assert legacy_key not in sm.data.entries


def test_bulk_iteration_finds_legacy_keyed_entries(mock_sidecar_dir):
    """Bulk get_metadata(path, create=False) must find and migrate legacy keys.

    Regression test: hot paths like flag filtering and batch selection iterate
    over all images calling get_metadata(path, create=False).  If they bypassed
    migration (e.g. via direct entries dict access), legacy-keyed entries would
    be silently missed.
    """
    content = {
        "version": 2,
        "entries": {
            # Legacy filename-keyed entry (has extension — differs from stable key)
            "IMG_0001.jpg": {"favorite": True},
            # Already-stable entry (bare stem, matches metadata_key_for_path)
            "IMG_0002": {"uploaded": True},
            # Another legacy filename-keyed entry
            "IMG_0003.jpg": {"favorite": True, "uploaded": True},
        },
    }
    d = mock_sidecar_dir(content)
    sm = SidecarManager(d, None)

    # Simulate the bulk iteration pattern used by _apply_filter_to_cached_list,
    # _get_bulk_metadata_map, _batch_add_favorites, _batch_add_uploaded
    image_paths = [Path("IMG_0001.jpg"), Path("IMG_0002.jpg"), Path("IMG_0003.jpg")]
    found = {}
    for p in image_paths:
        meta = sm.get_metadata(p, create=False)
        if meta is not None:
            found[p.stem] = meta

    # All three entries must be found — none silently dropped
    assert "IMG_0001" in found, "legacy filename-keyed entry must be found"
    assert "IMG_0002" in found, "already-stable entry must be found"
    assert "IMG_0003" in found, "legacy filename-keyed entry must be found"

    assert found["IMG_0001"].favorite is True
    assert found["IMG_0002"].uploaded is True
    assert found["IMG_0003"].favorite is True
    assert found["IMG_0003"].uploaded is True

    # Legacy keys should have been migrated to stable keys
    stable_1 = sm.metadata_key_for_path(Path("IMG_0001.jpg"))
    stable_3 = sm.metadata_key_for_path(Path("IMG_0003.jpg"))
    assert stable_1 in sm.data.entries
    assert stable_3 in sm.data.entries
    assert "IMG_0001.jpg" not in sm.data.entries
    assert "IMG_0003.jpg" not in sm.data.entries


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


def test_dotted_string_with_image_extension_stays_exact(mock_sidecar_dir):
    """String key 'photo.CR2' must not be path-normalized to 'photo'."""
    d = mock_sidecar_dir()
    sm = SidecarManager(d, None)
    legacy = EntryMetadata(favorite=True)
    sm.data.entries["photo.CR2"] = legacy

    meta = sm.get_metadata("photo.CR2", create=False)

    assert meta is legacy
    assert "photo.CR2" in sm.data.entries
    assert "photo" not in sm.data.entries


def test_dotted_string_with_jpg_extension_stays_exact(mock_sidecar_dir):
    """String key 'IMG_0001.jpg' must not be path-normalized when used as string."""
    d = mock_sidecar_dir()
    sm = SidecarManager(d, None)
    legacy = EntryMetadata(uploaded=True)
    sm.data.entries["IMG_0001.jpg"] = legacy

    meta = sm.get_metadata("IMG_0001.jpg", create=False)

    assert meta is legacy
    assert "IMG_0001.jpg" in sm.data.entries


def test_string_with_path_separator_is_path_normalized(mock_sidecar_dir):
    """String containing a path separator should be treated as a path."""
    d = mock_sidecar_dir()
    sm = SidecarManager(d, None)

    meta = sm.get_metadata("subdir/photo.CR2")
    expected_key = sm.metadata_key_for_path(Path("subdir/photo.CR2"))

    assert expected_key in sm.data.entries
    assert expected_key == "subdir/photo"


def test_legacy_filename_key_migrates_via_path_lookup(mock_sidecar_dir):
    """Legacy 'photo.CR2' entry migrates when looked up via Path('photo.jpg')."""
    content = {
        "version": 2,
        "entries": {
            "photo.CR2": {"favorite": True},
        },
    }
    d = mock_sidecar_dir(content)
    sm = SidecarManager(d, None)

    # Path-based lookup should find and migrate the legacy key
    meta = sm.get_metadata(Path("photo.jpg"), create=False)

    assert meta is not None
    assert meta.favorite is True
    assert "photo" in sm.data.entries
    assert "photo.CR2" not in sm.data.entries


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
        assert key_upper != key_lower
