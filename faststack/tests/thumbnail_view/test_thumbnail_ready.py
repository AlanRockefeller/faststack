"""Test that _on_thumbnail_ready only fires for matching size IDs."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from PySide6.QtCore import QCoreApplication

from faststack.io.utils import compute_path_hash
from faststack.thumbnail_view.model import ThumbnailEntry, ThumbnailModel


@pytest.fixture(scope="module")
def qapp():
    """Ensure a QCoreApplication exists for the test module."""
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication(sys.argv)
    yield app


@pytest.fixture()
def model_with_entry(tmp_path, qapp):
    """Create a ThumbnailModel with one image entry."""
    img = tmp_path / "photo.jpg"
    img.write_bytes(b"\xff\xd8")  # minimal JPEG magic
    mtime_ns = 1000000000

    model = ThumbnailModel(
        base_directory=tmp_path,
        current_directory=tmp_path,
        get_metadata_callback=None,
        thumbnail_size=200,
    )

    # Manually insert a single entry and rebuild the id mapping
    entry = ThumbnailEntry(
        path=img,
        name=img.name,
        is_folder=False,
        mtime_ns=mtime_ns,
    )
    model._entries = [entry]
    model._rebuild_id_mapping()

    path_hash = compute_path_hash(img)
    return model, entry, path_hash, mtime_ns


def test_wrong_size_ignored(model_with_entry):
    """thumbnailReady with wrong size should not bump thumb_rev."""
    model, entry, path_hash, mtime_ns = model_with_entry

    spy = MagicMock()
    model.dataChanged.connect(spy)

    wrong_id = f"160/{path_hash}/{mtime_ns}"
    model._on_thumbnail_ready(wrong_id)

    assert entry.thumb_rev == 0
    spy.assert_not_called()


def test_correct_size_bumps_rev(model_with_entry):
    """thumbnailReady with correct size should bump thumb_rev and emit dataChanged."""
    model, entry, path_hash, mtime_ns = model_with_entry

    spy = MagicMock()
    model.dataChanged.connect(spy)

    correct_id = f"200/{path_hash}/{mtime_ns}"
    model._on_thumbnail_ready(correct_id)

    assert entry.thumb_rev == 1
    spy.assert_called_once()

    # Verify roles include ThumbnailSourceRole and ThumbRevRole
    _, _, roles_arg = spy.call_args[0]
    assert model.ThumbnailSourceRole in roles_arg
    assert model.ThumbRevRole in roles_arg
