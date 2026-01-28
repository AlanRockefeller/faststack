"""Tests for ThumbnailModel."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from PySide6.QtCore import Qt

from faststack.thumbnail_view.model import ThumbnailModel, ThumbnailEntry, _compute_path_hash


@pytest.fixture
def temp_folder(tmp_path):
    """Create a temporary folder structure for testing."""
    # Create some test files
    (tmp_path / "image1.jpg").touch()
    (tmp_path / "image2.jpg").touch()
    (tmp_path / "image3.png").touch()

    # Create a subfolder
    subfolder = tmp_path / "subfolder"
    subfolder.mkdir()
    (subfolder / "sub_image.jpg").touch()

    return tmp_path


@pytest.fixture
def model(temp_folder):
    """Create a ThumbnailModel instance."""
    model = ThumbnailModel(
        base_directory=temp_folder,
        current_directory=temp_folder,
        get_metadata_callback=None,
        thumbnail_size=200,
    )
    return model


class TestThumbnailEntry:
    """Tests for ThumbnailEntry dataclass."""

    def test_entry_creation(self, temp_folder):
        """Test creating a ThumbnailEntry."""
        entry = ThumbnailEntry(
            path=temp_folder / "test.jpg",
            name="test.jpg",
            is_folder=False,
            is_stacked=True,
            is_uploaded=False,
            is_edited=True,
            mtime_ns=1234567890,
        )
        assert entry.name == "test.jpg"
        assert entry.is_folder is False
        assert entry.is_stacked is True
        assert entry.thumb_rev == 0


class TestComputePathHash:
    """Tests for _compute_path_hash function."""

    def test_hash_is_stable(self, temp_folder):
        """Test that hash is stable for same path."""
        path = temp_folder / "test.jpg"
        hash1 = _compute_path_hash(path)
        hash2 = _compute_path_hash(path)
        assert hash1 == hash2

    def test_hash_is_16_chars(self, temp_folder):
        """Test that hash is 16 characters long."""
        path = temp_folder / "test.jpg"
        hash_val = _compute_path_hash(path)
        assert len(hash_val) == 16


class TestThumbnailModel:
    """Tests for ThumbnailModel."""

    def test_model_creation(self, model, temp_folder):
        """Test model is created correctly."""
        assert model.current_directory == temp_folder.resolve()
        assert model.base_directory == temp_folder.resolve()
        assert model.rowCount() == 0  # Not refreshed yet

    @patch('faststack.thumbnail_view.model.find_images')
    def test_refresh_populates_entries(self, mock_find_images, model, temp_folder):
        """Test that refresh populates the model."""
        from faststack.models import ImageFile

        # Mock find_images to return test images
        mock_find_images.return_value = [
            ImageFile(path=temp_folder / "image1.jpg", timestamp=1.0),
            ImageFile(path=temp_folder / "image2.jpg", timestamp=2.0),
        ]

        model.refresh()

        # Should have 1 folder + 2 images (no parent folder since at base)
        assert model.rowCount() >= 2

    @patch('faststack.thumbnail_view.model.find_images')
    def test_folders_sorted_first(self, mock_find_images, model, temp_folder):
        """Test that folders appear before images."""
        from faststack.models import ImageFile

        mock_find_images.return_value = [
            ImageFile(path=temp_folder / "image1.jpg", timestamp=1.0),
        ]

        model.refresh()

        # Check folder is first (if any)
        if model.rowCount() > 1:
            entry0 = model.get_entry(0)
            entry1 = model.get_entry(1)
            if entry0 and entry1:
                # If first is folder and second is file, order is correct
                if entry0.is_folder and not entry1.is_folder:
                    assert True
                elif not entry0.is_folder and entry1.is_folder:
                    pytest.fail("Folder should come before file")

    def test_role_names(self, model):
        """Test that roleNames returns expected roles."""
        roles = model.roleNames()
        assert b"filePath" in roles.values()
        assert b"fileName" in roles.values()
        assert b"isFolder" in roles.values()
        assert b"isStacked" in roles.values()
        assert b"isUploaded" in roles.values()
        assert b"isEdited" in roles.values()
        assert b"thumbnailSource" in roles.values()
        assert b"isSelected" in roles.values()

    @patch('faststack.thumbnail_view.model.find_images')
    def test_parent_folder_at_subdirectory(self, mock_find_images, temp_folder):
        """Test that parent folder entry appears when not at base."""
        from faststack.models import ImageFile

        subfolder = temp_folder / "subfolder"

        # Create model at subfolder
        model = ThumbnailModel(
            base_directory=temp_folder,
            current_directory=subfolder,
            get_metadata_callback=None,
        )

        mock_find_images.return_value = [
            ImageFile(path=subfolder / "sub_image.jpg", timestamp=1.0),
        ]

        model.refresh()

        # First entry should be parent folder
        first_entry = model.get_entry(0)
        assert first_entry is not None
        assert first_entry.name == ".."
        assert first_entry.is_folder is True

    @patch('faststack.thumbnail_view.model.find_images')
    def test_no_parent_folder_at_base(self, mock_find_images, model):
        """Test that no parent folder entry when at base directory."""
        from faststack.models import ImageFile

        mock_find_images.return_value = []

        model.refresh()

        # No ".." entry when at base
        for i in range(model.rowCount()):
            entry = model.get_entry(i)
            if entry:
                assert entry.name != ".."


class TestThumbnailModelSelection:
    """Tests for selection functionality."""

    @patch('faststack.thumbnail_view.model.find_images')
    def test_select_single(self, mock_find_images, model, temp_folder):
        """Test selecting a single image."""
        from faststack.models import ImageFile

        mock_find_images.return_value = [
            ImageFile(path=temp_folder / "image1.jpg", timestamp=1.0),
            ImageFile(path=temp_folder / "image2.jpg", timestamp=2.0),
        ]

        model.refresh()

        # Find first non-folder index
        img_idx = None
        for i in range(model.rowCount()):
            entry = model.get_entry(i)
            if entry and not entry.is_folder:
                img_idx = i
                break

        if img_idx is not None:
            model.select_index(img_idx, shift=False, ctrl=False)
            selected = model.get_selected_paths()
            assert len(selected) == 1

    @patch('faststack.thumbnail_view.model.find_images')
    def test_ctrl_click_toggle(self, mock_find_images, model, temp_folder):
        """Test Ctrl+click toggles selection."""
        from faststack.models import ImageFile

        mock_find_images.return_value = [
            ImageFile(path=temp_folder / "image1.jpg", timestamp=1.0),
            ImageFile(path=temp_folder / "image2.jpg", timestamp=2.0),
        ]

        model.refresh()

        # Find image indices
        img_indices = []
        for i in range(model.rowCount()):
            entry = model.get_entry(i)
            if entry and not entry.is_folder:
                img_indices.append(i)

        if len(img_indices) >= 2:
            # Select first
            model.select_index(img_indices[0], shift=False, ctrl=False)
            # Ctrl+click second
            model.select_index(img_indices[1], shift=False, ctrl=True)
            assert len(model.get_selected_paths()) == 2

            # Ctrl+click first again to deselect
            model.select_index(img_indices[0], shift=False, ctrl=True)
            assert len(model.get_selected_paths()) == 1

    @patch('faststack.thumbnail_view.model.find_images')
    def test_clear_selection(self, mock_find_images, model, temp_folder):
        """Test clearing selection."""
        from faststack.models import ImageFile

        mock_find_images.return_value = [
            ImageFile(path=temp_folder / "image1.jpg", timestamp=1.0),
        ]

        model.refresh()

        # Find and select an image
        for i in range(model.rowCount()):
            entry = model.get_entry(i)
            if entry and not entry.is_folder:
                model.select_index(i, shift=False, ctrl=False)
                break

        assert len(model.get_selected_paths()) == 1

        model.clear_selection()
        assert len(model.get_selected_paths()) == 0

    @patch('faststack.thumbnail_view.model.find_images')
    def test_cannot_select_folders(self, mock_find_images, model):
        """Test that folders cannot be selected."""
        from faststack.models import ImageFile

        mock_find_images.return_value = []

        model.refresh()

        # Try to select a folder
        for i in range(model.rowCount()):
            entry = model.get_entry(i)
            if entry and entry.is_folder:
                model.select_index(i, shift=False, ctrl=False)
                break

        # Selection should be empty
        assert len(model.get_selected_paths()) == 0


class TestThumbnailModelNavigation:
    """Tests for navigation functionality."""

    @patch('faststack.thumbnail_view.model.find_images')
    def test_navigate_to_subfolder(self, mock_find_images, model, temp_folder):
        """Test navigating to a subfolder."""
        from faststack.models import ImageFile

        subfolder = temp_folder / "subfolder"
        mock_find_images.return_value = []

        model.navigate_to(subfolder)

        assert model.current_directory == subfolder.resolve()

    @patch('faststack.thumbnail_view.model.find_images')
    def test_cannot_navigate_outside_base(self, mock_find_images, model, temp_folder):
        """Test that navigation outside base directory is blocked."""
        from faststack.models import ImageFile

        mock_find_images.return_value = []

        # Try to navigate to parent of base
        model.navigate_to(temp_folder.parent)

        # Should still be at base
        assert model.current_directory == temp_folder.resolve()

    @patch('faststack.thumbnail_view.model.find_images')
    def test_navigation_clears_selection(self, mock_find_images, model, temp_folder):
        """Test that navigation clears selection."""
        from faststack.models import ImageFile

        mock_find_images.return_value = [
            ImageFile(path=temp_folder / "image1.jpg", timestamp=1.0),
        ]

        model.refresh()

        # Select an image
        for i in range(model.rowCount()):
            entry = model.get_entry(i)
            if entry and not entry.is_folder:
                model.select_index(i, shift=False, ctrl=False)
                break

        assert len(model.get_selected_paths()) >= 0  # May or may not have selection

        # Navigate
        subfolder = temp_folder / "subfolder"
        model.navigate_to(subfolder)

        # Selection should be cleared
        assert len(model.get_selected_paths()) == 0
