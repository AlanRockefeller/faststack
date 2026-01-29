"""Tests for selection functionality in ThumbnailModel."""

import pytest

from faststack.thumbnail_view.model import ThumbnailModel, ThumbnailEntry


@pytest.fixture
def temp_folder(tmp_path):
    """Create a temporary folder structure for testing."""
    # Create some test files
    for i in range(5):
        (tmp_path / f"image{i}.jpg").touch()
    return tmp_path


@pytest.fixture
def model_with_images(temp_folder):
    """Create a ThumbnailModel with mock images."""
    model = ThumbnailModel(
        base_directory=temp_folder,
        current_directory=temp_folder,
        get_metadata_callback=None,
        thumbnail_size=200,
    )

    # Manually populate entries for testing
    model._entries = [
        ThumbnailEntry(
            path=temp_folder / "image0.jpg",
            name="image0.jpg",
            is_folder=False,
            mtime_ns=1000,
        ),
        ThumbnailEntry(
            path=temp_folder / "image1.jpg",
            name="image1.jpg",
            is_folder=False,
            mtime_ns=1001,
        ),
        ThumbnailEntry(
            path=temp_folder / "image2.jpg",
            name="image2.jpg",
            is_folder=False,
            mtime_ns=1002,
        ),
        ThumbnailEntry(
            path=temp_folder / "image3.jpg",
            name="image3.jpg",
            is_folder=False,
            mtime_ns=1003,
        ),
        ThumbnailEntry(
            path=temp_folder / "image4.jpg",
            name="image4.jpg",
            is_folder=False,
            mtime_ns=1004,
        ),
    ]

    return model


class TestPlainClick:
    """Tests for plain click selection behavior."""

    def test_plain_click_selects_single(self, model_with_images):
        """Test that plain click selects only the clicked item."""
        model_with_images.select_index(2, shift=False, ctrl=False)

        selected = model_with_images.get_selected_paths()
        assert len(selected) == 1
        assert selected[0].name == "image2.jpg"

    def test_plain_click_clears_previous_selection(self, model_with_images):
        """Test that plain click clears previous selection."""
        # Select multiple items first
        model_with_images.select_index(1, shift=False, ctrl=False)
        model_with_images.select_index(2, shift=False, ctrl=True)
        model_with_images.select_index(3, shift=False, ctrl=True)

        assert len(model_with_images.get_selected_paths()) == 3

        # Plain click should clear and select only one
        model_with_images.select_index(0, shift=False, ctrl=False)

        selected = model_with_images.get_selected_paths()
        assert len(selected) == 1
        assert selected[0].name == "image0.jpg"


class TestCtrlClick:
    """Tests for Ctrl+click selection behavior."""

    def test_ctrl_click_adds_to_selection(self, model_with_images):
        """Test that Ctrl+click adds to existing selection."""
        model_with_images.select_index(1, shift=False, ctrl=False)
        model_with_images.select_index(3, shift=False, ctrl=True)

        selected = model_with_images.get_selected_paths()
        assert len(selected) == 2

        names = [p.name for p in selected]
        assert "image1.jpg" in names
        assert "image3.jpg" in names

    def test_ctrl_click_toggles_off(self, model_with_images):
        """Test that Ctrl+click on selected item deselects it."""
        model_with_images.select_index(1, shift=False, ctrl=False)
        model_with_images.select_index(2, shift=False, ctrl=True)
        model_with_images.select_index(3, shift=False, ctrl=True)

        assert len(model_with_images.get_selected_paths()) == 3

        # Ctrl+click on already selected item
        model_with_images.select_index(2, shift=False, ctrl=True)

        selected = model_with_images.get_selected_paths()
        assert len(selected) == 2

        names = [p.name for p in selected]
        assert "image2.jpg" not in names

    def test_ctrl_click_non_contiguous(self, model_with_images):
        """Test Ctrl+click can select non-contiguous items."""
        model_with_images.select_index(0, shift=False, ctrl=False)
        model_with_images.select_index(2, shift=False, ctrl=True)
        model_with_images.select_index(4, shift=False, ctrl=True)

        selected = model_with_images.get_selected_paths()
        assert len(selected) == 3

        names = [p.name for p in selected]
        assert "image0.jpg" in names
        assert "image2.jpg" in names
        assert "image4.jpg" in names
        assert "image1.jpg" not in names
        assert "image3.jpg" not in names


class TestShiftClick:
    """Tests for Shift+click selection behavior."""

    def test_shift_click_selects_range(self, model_with_images):
        """Test that Shift+click selects a contiguous range."""
        model_with_images.select_index(1, shift=False, ctrl=False)
        model_with_images.select_index(4, shift=True, ctrl=False)

        selected = model_with_images.get_selected_paths()
        assert len(selected) == 4  # images 1, 2, 3, 4

        names = [p.name for p in selected]
        assert "image1.jpg" in names
        assert "image2.jpg" in names
        assert "image3.jpg" in names
        assert "image4.jpg" in names

    def test_shift_click_range_backwards(self, model_with_images):
        """Test Shift+click works when clicking backwards."""
        model_with_images.select_index(4, shift=False, ctrl=False)
        model_with_images.select_index(1, shift=True, ctrl=False)

        selected = model_with_images.get_selected_paths()
        assert len(selected) == 4  # images 1, 2, 3, 4

    def test_shift_click_adds_to_existing(self, model_with_images):
        """Test Shift+click adds to existing selection."""
        model_with_images.select_index(0, shift=False, ctrl=False)
        model_with_images.select_index(2, shift=True, ctrl=False)

        selected = model_with_images.get_selected_paths()
        assert len(selected) == 3  # images 0, 1, 2

    def test_shift_click_without_anchor(self, model_with_images):
        """Test Shift+click when no previous selection."""
        # Clear any existing selection
        model_with_images.clear_selection()
        model_with_images._last_selected_index = None

        # Shift+click without anchor should just select the single item
        model_with_images.select_index(2, shift=True, ctrl=False)

        # When no anchor exists, only the clicked item should be selected
        selected = model_with_images.get_selected_paths()
        assert len(selected) == 1


class TestFolderSelection:
    """Tests for folder selection behavior."""

    def test_cannot_select_folder(self, temp_folder):
        """Test that folders cannot be selected."""
        model = ThumbnailModel(
            base_directory=temp_folder,
            current_directory=temp_folder,
            get_metadata_callback=None,
        )

        # Add a folder entry
        model._entries = [
            ThumbnailEntry(
                path=temp_folder / "subfolder", name="subfolder", is_folder=True
            ),
            ThumbnailEntry(
                path=temp_folder / "image.jpg", name="image.jpg", is_folder=False
            ),
        ]

        # Try to select folder
        model.select_index(0, shift=False, ctrl=False)

        # Should have no selection
        assert len(model.get_selected_paths()) == 0

    def test_shift_click_skips_folders(self, temp_folder):
        """Test that Shift+click range selection skips folders."""
        model = ThumbnailModel(
            base_directory=temp_folder,
            current_directory=temp_folder,
            get_metadata_callback=None,
        )

        # Add mixed entries
        model._entries = [
            ThumbnailEntry(
                path=temp_folder / "image0.jpg", name="image0.jpg", is_folder=False
            ),
            ThumbnailEntry(
                path=temp_folder / "subfolder", name="subfolder", is_folder=True
            ),
            ThumbnailEntry(
                path=temp_folder / "image1.jpg", name="image1.jpg", is_folder=False
            ),
        ]

        # Select first image, then shift-click third
        model.select_index(0, shift=False, ctrl=False)
        model.select_index(2, shift=True, ctrl=False)

        selected = model.get_selected_paths()

        # Should have 2 images selected (folder skipped)
        assert len(selected) == 2

        names = [p.name for p in selected]
        assert "image0.jpg" in names
        assert "image1.jpg" in names
        assert "subfolder" not in names


class TestClearSelection:
    """Tests for clearing selection."""

    def test_clear_selection(self, model_with_images):
        """Test that clear_selection removes all selections."""
        model_with_images.select_index(1, shift=False, ctrl=False)
        model_with_images.select_index(3, shift=False, ctrl=True)

        assert len(model_with_images.get_selected_paths()) == 2

        model_with_images.clear_selection()

        assert len(model_with_images.get_selected_paths()) == 0

    def test_clear_selection_empty(self, model_with_images):
        """Test that clear_selection on empty selection is safe."""
        assert len(model_with_images.get_selected_paths()) == 0

        # Should not error
        model_with_images.clear_selection()

        assert len(model_with_images.get_selected_paths()) == 0


class TestSelectionDataChanged:
    """Tests for dataChanged signal emission on selection changes."""

    def test_select_emits_data_changed(self, model_with_images):
        """Test that selection changes emit dataChanged signal."""
        # Track signal emission
        signal_emitted = []
        model_with_images.dataChanged.connect(lambda *args: signal_emitted.append(args))

        model_with_images.select_index(2, shift=False, ctrl=False)

        # Signal should have been emitted
        assert len(signal_emitted) > 0

    def test_clear_selection_emits_data_changed(self, model_with_images):
        """Test that clear_selection emits dataChanged for selected rows."""
        model_with_images.select_index(2, shift=False, ctrl=False)

        # Track signal emission
        signal_emitted = []
        model_with_images.dataChanged.connect(lambda *args: signal_emitted.append(args))

        model_with_images.clear_selection()

        # Signal should have been emitted
        assert len(signal_emitted) > 0


class TestGetSelectedPaths:
    """Tests for get_selected_paths method."""

    def test_returns_paths_in_order(self, model_with_images):
        """Test that get_selected_paths returns paths in index order."""
        model_with_images.select_index(3, shift=False, ctrl=False)
        model_with_images.select_index(1, shift=False, ctrl=True)
        model_with_images.select_index(4, shift=False, ctrl=True)

        selected = model_with_images.get_selected_paths()

        # Should be sorted by index
        assert selected[0].name == "image1.jpg"
        assert selected[1].name == "image3.jpg"
        assert selected[2].name == "image4.jpg"

    def test_returns_only_files(self, temp_folder):
        """Test that get_selected_paths only returns file paths."""
        model = ThumbnailModel(
            base_directory=temp_folder,
            current_directory=temp_folder,
            get_metadata_callback=None,
        )

        model._entries = [
            ThumbnailEntry(
                path=temp_folder / "image.jpg", name="image.jpg", is_folder=False
            ),
        ]

        model.select_index(0, shift=False, ctrl=False)
        selected = model.get_selected_paths()

        assert len(selected) == 1
        assert all(not p.is_dir() for p in selected if p.exists())
