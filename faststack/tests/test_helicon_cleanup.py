import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
import tempfile
import os
from faststack.app import AppController
from faststack.models import ImageFile


@pytest.fixture
def mock_controller():
    # Mock dependencies required by AppController init
    engine = MagicMock()
    with (
        patch("faststack.app.Watcher"),
        patch("faststack.app.SidecarManager"),
        patch("faststack.app.ImageEditor"),
        patch("faststack.app.ByteLRUCache"),
        patch("faststack.app.Prefetcher"),
        patch("faststack.app.ThumbnailCache"),
        patch("faststack.app.PathResolver"),
        patch("faststack.app.ThumbnailPrefetcher"),
        patch("faststack.app.ThumbnailModel"),
        patch("faststack.app.ThumbnailProvider"),
        patch("faststack.app.concurrent.futures.ThreadPoolExecutor"),
        patch("faststack.app.QTimer"),
        patch("faststack.app.QApplication"),
    ):  # Mock QApplication to avoid segfaults

        controller = AppController(image_dir=Path("c:/images"), engine=engine)

        # Setup dummy images for the controller
        img1 = ImageFile(Path("c:/images/img1.jpg"))
        controller.image_files = [img1]

    return controller


def test_deferred_cleanup(mock_controller):
    """Verify that temp files are tracked and deleted on shutdown."""

    # Create a real temporary file to verify deletion
    with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
        tmp.write("test_file_list")
        tmp_path = Path(tmp.name)

    try:
        # Mock launch_helicon_focus to return success and our real temp path
        with patch("faststack.app.launch_helicon_focus", return_value=(True, tmp_path)):

            # Simulate launching helicon with some files
            # We bypass the stack logic and call _launch_helicon_with_files directly or via launch_helicon if valid
            # Let's call _launch_helicon_with_files directly for simplicity
            files = [Path("c:/images/img1.jpg")]
            success = mock_controller._launch_helicon_with_files(files)

            assert success is True
            assert tmp_path in mock_controller._temp_files_to_clean
            assert tmp_path.exists(), "File should still exist before shutdown"

            # Now simulate shutdown
            mock_controller.shutdown_nonqt()

            assert not tmp_path.exists(), "File should be deleted after shutdown"
            assert len(mock_controller._temp_files_to_clean) == 0

    finally:
        # Cleanup if test fails
        if tmp_path.exists():
            os.remove(tmp_path)
