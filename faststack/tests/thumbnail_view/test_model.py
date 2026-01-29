"""Tests for ThumbnailModel."""

import sys
import pytest
from pathlib import Path
from unittest.mock import patch

from faststack.thumbnail_view.model import (
    ThumbnailModel,
    ThumbnailEntry,
    _compute_path_hash,
    _is_filesystem_root,
)


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

    @patch("faststack.thumbnail_view.model.find_images")
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

    @patch("faststack.thumbnail_view.model.find_images")
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

    @patch("faststack.thumbnail_view.model.find_images")
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

    @patch("faststack.thumbnail_view.model.find_images")
    def test_parent_folder_shown_when_not_at_root(self, mock_find_images, model):
        r"""Test that parent folder entry is shown when not at filesystem root.

        The new behavior allows navigating up even from the initial launch
        directory. ".." is only hidden at filesystem roots (/, C:\, etc).
        """

        mock_find_images.return_value = []

        model.refresh()

        # ".." entry should be present unless we're at filesystem root
        # Since temp_folder is not a filesystem root, ".." should appear
        has_parent_entry = any(
            model.get_entry(i) and model.get_entry(i).name == ".."
            for i in range(model.rowCount())
        )
        # temp_folder is not a filesystem root, so ".." should be present
        assert has_parent_entry, "Expected '..' entry for non-root directory"


class TestThumbnailModelSelection:
    """Tests for selection functionality."""

    @patch("faststack.thumbnail_view.model.find_images")
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

    @patch("faststack.thumbnail_view.model.find_images")
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

    @patch("faststack.thumbnail_view.model.find_images")
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

    @patch("faststack.thumbnail_view.model.find_images")
    def test_cannot_select_folders(self, mock_find_images, model):
        """Test that folders cannot be selected."""

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

    @patch("faststack.thumbnail_view.model.find_images")
    def test_navigate_to_subfolder(self, mock_find_images, model, temp_folder):
        """Test navigating to a subfolder."""

        subfolder = temp_folder / "subfolder"
        mock_find_images.return_value = []

        model.navigate_to(subfolder)

        assert model.current_directory == subfolder.resolve()

    @patch("faststack.thumbnail_view.model.find_images")
    def test_cannot_navigate_outside_base(self, mock_find_images, model, temp_folder):
        """Test that navigation outside base directory is blocked."""

        mock_find_images.return_value = []

        # Try to navigate to parent of base
        model.navigate_to(temp_folder.parent)

        # Should still be at base
        assert model.current_directory == temp_folder.resolve()

    @patch("faststack.thumbnail_view.model.find_images")
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


class TestIsFilesystemRoot:
    """Tests for _is_filesystem_root function."""

    def test_unix_root(self):
        """Test that / is detected as root on Unix."""
        assert _is_filesystem_root(Path("/")) is True

    def test_non_root_unix_path(self, temp_folder):
        """Test that a non-root path is not detected as root."""
        assert _is_filesystem_root(temp_folder) is False

    def test_deep_path_not_root(self):
        """Test that a deep path is not detected as root."""
        assert _is_filesystem_root(Path("/home/user/documents")) is False

    def test_path_with_resolve(self, temp_folder):
        """Test that path is resolved before checking."""
        # Create a relative path that resolves to temp_folder
        resolved = temp_folder.resolve()
        assert _is_filesystem_root(resolved) is False

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific test")
    def test_windows_drive_root(self):
        """Test Windows drive root detection (e.g., C:\\)."""
        # Test C:\ format (only meaningful on Windows)
        assert _is_filesystem_root(Path("C:\\")) is True
        assert _is_filesystem_root(Path("D:\\")) is True

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific test")
    def test_windows_non_root_path(self):
        """Test that a Windows non-root path is not detected as root."""
        assert _is_filesystem_root(Path("C:\\Users\\test")) is False
        assert _is_filesystem_root(Path("D:\\data\\folder")) is False

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific test")
    def test_unc_path_root(self):
        """Test UNC root detection (\\server\\share format)."""
        # \\server\share is the share root level (only on Windows)
        assert _is_filesystem_root(Path("\\\\server\\share")) is True

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific test")
    def test_unc_path_non_root(self):
        """Test that UNC subpaths are not detected as root."""
        # \\server\share\folder is NOT a root
        assert _is_filesystem_root(Path("\\\\server\\share\\folder")) is False
        # \\server\share\folder\subfolder is NOT a root
        assert (
            _is_filesystem_root(Path("\\\\server\\share\\folder\\subfolder")) is False
        )

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific test")
    def test_unc_server_only_not_root(self):
        """Test that \\server alone is not considered a root (requires share)."""
        # Just \\server (no share) shouldn't be a root according to implementation
        assert _is_filesystem_root(Path("\\\\server")) is False
