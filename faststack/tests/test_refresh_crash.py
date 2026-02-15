import pytest
from pathlib import Path
from unittest.mock import Mock, patch
from faststack.thumbnail_view import ThumbnailModel

@pytest.fixture
def model(tmp_path):
    # Mocking dependencies that might trigger complex I/O or UI logic
    with (
        patch('faststack.thumbnail_view.model.count_images_in_folder', return_value=0),
        patch('faststack.thumbnail_view.model.read_folder_stats', return_value=None),
        patch('faststack.thumbnail_view.model.find_images', return_value=[]),
    ):
        model = ThumbnailModel(tmp_path, tmp_path)
        # Mock Qt-specific calls that need a running event loop or app
        model.beginResetModel = Mock()
        model.endResetModel = Mock()
        model.selectionChanged = Mock()
        yield model

def test_refresh_no_name_error(model):
    """Verify that refresh() doesn't raise NameError (fix for regression)."""
    # This should not raise NameError for t0, t1, t2, t3
    model.refresh()

def test_refresh_from_controller_no_name_error(model):
    """Verify that refresh_from_controller() doesn't raise NameError."""
    # This should not raise NameError
    model.refresh_from_controller([], metadata_map={})
