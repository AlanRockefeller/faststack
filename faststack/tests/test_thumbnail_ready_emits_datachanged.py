"""Test that _on_thumbnail_ready correctly emits dataChanged for the matching row."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Minimal Qt imports needed for the model
from PySide6.QtCore import Qt, QModelIndex, QCoreApplication


@pytest.fixture(scope="session")
def qapp():
    """Ensure a QCoreApplication exists for the test session."""
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    yield app


@pytest.fixture
def thumbnail_model(qapp):
    """Create a ThumbnailModel with fake entries for testing."""
    from faststack.thumbnail_view.model import ThumbnailModel, ThumbnailEntry

    model = ThumbnailModel(
        base_directory=Path("/fake/dir"),
        current_directory=Path("/fake/dir"),
        thumbnail_size=200,
    )

    # Manually add entries (bypass refresh which would scan disk)
    entries = [
        ThumbnailEntry(
            path=Path("/fake/dir/img001.jpg"),
            name="img001.jpg",
            is_folder=False,
            mtime_ns=1000,
        ),
        ThumbnailEntry(
            path=Path("/fake/dir/img002.jpg"),
            name="img002.jpg",
            is_folder=False,
            mtime_ns=2000,
        ),
        ThumbnailEntry(
            path=Path("/fake/dir/img003.jpg"),
            name="img003.jpg",
            is_folder=False,
            mtime_ns=3000,
        ),
    ]

    model.beginResetModel()
    model._entries = entries
    model._rebuild_id_mapping()
    model.endResetModel()

    return model


def test_id_to_row_uses_full_thumbnail_id(thumbnail_model):
    """_id_to_row keys must match the '{size}/{path_hash}/{mtime_ns}' format
    that the prefetcher emits as cache_key."""
    from faststack.io.utils import compute_path_hash

    entry = thumbnail_model._entries[0]
    expected_key = thumbnail_model._make_thumbnail_id(entry)

    # The key must be in the mapping
    assert expected_key in thumbnail_model._id_to_row
    assert thumbnail_model._id_to_row[expected_key] == 0

    # Plain path_hash must NOT be in the mapping (this was the old, broken format)
    plain_hash = compute_path_hash(entry.path)
    assert plain_hash not in thumbnail_model._id_to_row


def test_on_thumbnail_ready_emits_data_changed(thumbnail_model):
    """When _on_thumbnail_ready is called with a valid thumbnail_id,
    it must bump thumb_rev and emit dataChanged for the correct row."""
    spy = MagicMock()
    thumbnail_model.dataChanged.connect(spy)

    entry = thumbnail_model._entries[1]  # second entry
    tid = thumbnail_model._make_thumbnail_id(entry)

    old_rev = entry.thumb_rev
    thumbnail_model._on_thumbnail_ready(tid)

    # Revision should be bumped
    assert entry.thumb_rev == old_rev + 1

    # dataChanged should have been emitted exactly once
    assert spy.call_count == 1
    top_left, bottom_right, roles = spy.call_args[0]
    assert top_left.row() == 1
    assert bottom_right.row() == 1
    assert thumbnail_model.ThumbnailSourceRole in roles
    assert thumbnail_model.ThumbRevRole in roles


def test_on_thumbnail_ready_ignores_unknown_id(thumbnail_model):
    """If the thumbnail_id doesn't match any entry, nothing should happen."""
    spy = MagicMock()
    thumbnail_model.dataChanged.connect(spy)

    thumbnail_model._on_thumbnail_ready("200/nonexistent_hash/999")

    assert spy.call_count == 0


def test_all_entries_have_mapping(thumbnail_model):
    """Every non-folder entry must have a mapping in _id_to_row."""
    for i, entry in enumerate(thumbnail_model._entries):
        if not entry.is_folder:
            tid = thumbnail_model._make_thumbnail_id(entry)
            assert (
                tid in thumbnail_model._id_to_row
            ), f"Entry {i} ({entry.name}) not in _id_to_row"
            assert thumbnail_model._id_to_row[tid] == i
