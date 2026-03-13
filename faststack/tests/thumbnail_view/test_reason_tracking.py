"""Tests for thumbnail source reason tracking in ThumbnailModel."""

from pathlib import Path
import pytest
from PySide6.QtCore import Qt, QModelIndex
from faststack.thumbnail_view.model import ThumbnailModel, ThumbnailEntry


@pytest.fixture
def model():
    """Create a ThumbnailModel with fake entries."""
    model = ThumbnailModel(
        base_directory=Path("/fake/dir"),
        current_directory=Path("/fake/dir"),
        thumbnail_size=200,
    )

    # Manually add entries
    entries = [
        ThumbnailEntry(
            path=Path("/fake/dir/img1.jpg"),
            name="img1.jpg",
            is_folder=False,
            mtime_ns=1000,
        ),
        ThumbnailEntry(
            path=Path("/fake/dir/img2.jpg"),
            name="img2.jpg",
            is_folder=False,
            mtime_ns=2000,
        ),
        ThumbnailEntry(
            path=Path("/fake/dir/img3.jpg"),
            name="img3.jpg",
            is_folder=False,
            mtime_ns=3000,
        ),
    ]
    model._entries = entries
    model._rebuild_id_mapping()
    return model


def test_reason_persistence_across_all_items(model):
    """All visible items in a batch should carry the same reason."""
    model._next_source_reason = "filter"

    src1 = model.data(model.index(0), model.ThumbnailSourceRole)
    assert "reason=filter" in src1

    src2 = model.data(model.index(1), model.ThumbnailSourceRole)
    assert "reason=filter" in src2

    src3 = model.data(model.index(2), model.ThumbnailSourceRole)
    assert "reason=filter" in src3


def test_reason_defaults_to_scroll_when_none(model):
    """When no reason is set, data() returns reason=scroll."""
    model._next_source_reason = None

    src = model.data(model.index(0), model.ThumbnailSourceRole)
    assert "reason=scroll" in src


def test_reason_survives_endResetModel(model):
    """Reason must still be readable after endResetModel, before deferred clear."""
    model._next_source_reason = "jump"

    # Simulate what refresh() does: beginResetModel + endResetModel
    model.beginResetModel()
    model.endResetModel()

    # Reason should still be alive — QML reads data() after modelReset
    src = model.data(model.index(0), model.ThumbnailSourceRole)
    assert "reason=jump" in src


def test_clear_next_source_reason_method(model):
    """_clear_next_source_reason() resets to None so data() returns scroll."""
    model._next_source_reason = "refresh"

    # Reason is alive
    src = model.data(model.index(0), model.ThumbnailSourceRole)
    assert "reason=refresh" in src

    # Simulate deferred clear (what QTimer.singleShot(0, ...) will call)
    model._clear_next_source_reason()

    assert model._next_source_reason is None
    src = model.data(model.index(0), model.ThumbnailSourceRole)
    assert "reason=scroll" in src


def test_refresh_sets_deferred_clear(model):
    """refresh() should NOT synchronously clear reason."""
    from unittest.mock import patch
    from faststack.models import ImageFile

    with patch("faststack.thumbnail_view.model.find_images") as mock_find:
        # Note: This only verifies model._next_source_reason is preserved synchronously
        # after refresh() returns; the deferred clearing via QTimer is not tested here.
        mock_find.return_value = [
            ImageFile(path=Path("/fake/dir/img1.jpg")),
            ImageFile(path=Path("/fake/dir/img2.jpg")),
        ]

        model.set_filter("img")  # sets reason="filter", calls refresh()

        # Reason should still be alive right after refresh() returns
        # (deferred clear hasn't fired yet — no event loop iteration)
        assert model._next_source_reason == "filter"
