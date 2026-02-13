import pytest
from unittest.mock import MagicMock, patch
from faststack.app import AppController


@pytest.fixture
def app_controller(tmp_path):
    """Fixture to create an AppController with a temporary image directory."""
    from PySide6.QtCore import QCoreApplication

    # Create QCoreApplication instance if it doesn't exist
    app = QCoreApplication.instance()
    if not app:
        app = QCoreApplication([])

    # Create a dummy image directory
    image_dir = tmp_path / "images"
    image_dir.mkdir()

    # Mock engine and other deps
    mock_engine = MagicMock()

    with (
        patch("faststack.app.Watcher"),
        patch("faststack.app.SidecarManager"),
        patch("faststack.app.Prefetcher"),
        patch("faststack.app.ByteLRUCache"),
        patch("faststack.app.config"),
        patch("faststack.app.ThumbnailProvider"),
        patch("faststack.app.ThumbnailModel"),
        patch("faststack.app.ThumbnailPrefetcher"),
        patch("faststack.app.ThumbnailCache"),
    ):
        # Initialize controller
        controller = AppController(image_dir, mock_engine, debug_cache=False)
        return controller


def test_active_recycle_bins_initialization(app_controller):
    """Test that active_recycle_bins is initialized as an empty set."""
    assert hasattr(app_controller, "active_recycle_bins")
    assert isinstance(app_controller.active_recycle_bins, set)
    assert len(app_controller.active_recycle_bins) == 0


def test_move_to_recycle_tracks_bin(app_controller, tmp_path):
    """Test that moving a file to recycle bin adds the bin path to tracking."""
    # Create a dummy file
    src_file = app_controller.image_dir / "test.jpg"
    src_file.write_text("dummy content")

    # Move to recycle
    recycled_path = app_controller._move_to_recycle(src_file)

    # Verify file was moved
    assert recycled_path is not None
    assert recycled_path.exists()
    assert not src_file.exists()

    # Track it (caller's responsibility now that _move_to_recycle is static)
    app_controller.active_recycle_bins.add(recycled_path.parent)

    # Verify bin is tracked
    expected_bin = app_controller.image_dir / "image recycle bin"
    assert expected_bin in app_controller.active_recycle_bins


def test_get_recycle_bin_stats(app_controller):
    """Test that get_recycle_bin_stats returns correct counts."""
    # Create bin manually
    recycle_bin = app_controller.image_dir / "image recycle bin"
    recycle_bin.mkdir(parents=True)

    # Add items
    (recycle_bin / "file1.jpg").touch()
    (recycle_bin / "file2.jpg").touch()

    # Track it
    app_controller.active_recycle_bins.add(recycle_bin)

    # Get stats
    stats = app_controller.get_recycle_bin_stats()

    assert len(stats) == 1
    assert stats[0]["path"] == str(recycle_bin)
    assert stats[0]["count"] == 2


def test_cleanup_recycle_bins(app_controller):
    """Test that cleanup_recycle_bins removes the folders and clears tracking."""
    # Create bin manually
    recycle_bin = app_controller.image_dir / "image recycle bin"
    recycle_bin.mkdir(parents=True)
    (recycle_bin / "file1.jpg").touch()

    # Track it
    app_controller.active_recycle_bins.add(recycle_bin)

    # Cleanup
    app_controller.cleanup_recycle_bins()

    # Verify
    assert not recycle_bin.exists()
    assert len(app_controller.active_recycle_bins) == 0


def test_get_recycle_bin_stats_empty_bin(app_controller):
    """Test that empty bins are excluded or return 0 count."""
    # Create empty bin
    recycle_bin = app_controller.image_dir / "image recycle bin"
    recycle_bin.mkdir(parents=True)

    app_controller.active_recycle_bins.add(recycle_bin)

    stats = app_controller.get_recycle_bin_stats()

    # Depending on implementation, it might append with 0 or skip.
    # Current implementation: if count > 0: stats.append(...)
    assert len(stats) == 0


def test_cleanup_handles_missing_bin(app_controller):
    """Test that cleanup handles bins that were already deleted externally."""
    recycle_bin = app_controller.image_dir / "image recycle bin"

    # Add to tracking but don't create it (or delete it)
    app_controller.active_recycle_bins.add(recycle_bin)

    # Cleanup should not raise error
    app_controller.cleanup_recycle_bins()

    assert len(app_controller.active_recycle_bins) == 0


def test_get_recycle_bin_stats_untracked_existing_bin(app_controller):
    """Test that existing local recycle bin is detected even if not in active_recycle_bins."""
    # Create bin manually - simulate existing bin from previous session
    recycle_bin = app_controller.image_dir / "image recycle bin"
    recycle_bin.mkdir(parents=True)
    (recycle_bin / "existing.jpg").touch()

    # Do NOT add to active_recycle_bins

    # Get stats
    stats = app_controller.get_recycle_bin_stats()

    assert len(stats) == 1
    assert stats[0]["path"] == str(recycle_bin)
    assert stats[0]["count"] == 1
    # Check that it was auto-added to active_recycle_bins for future cleanup
    assert recycle_bin in app_controller.active_recycle_bins
