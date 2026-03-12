import pytest
from unittest.mock import Mock, patch
from pathlib import Path
from faststack.app import AppController
from faststack.models import ImageFile, EntryMetadata


@pytest.fixture(scope="session")
def qapp():
    """Ensure a QApplication exists for tests that might touch UI elements."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def mock_controller(tmp_path, qapp):
    """Creates an AppController with mocked dependencies."""
    _ = qapp
    engine = Mock()
    with (
        patch("faststack.app.Watcher"),
        patch("faststack.app.SidecarManager"),
        patch("faststack.app.ImageEditor"),
        patch("faststack.app.ByteLRUCache"),
        patch("faststack.app.Prefetcher"),
        patch("faststack.app.ThumbnailCache"),
        patch("faststack.app.ThumbnailPrefetcher"),
        patch("faststack.app.ThumbnailModel"),
        patch("faststack.app.ThumbnailProvider"),
        patch("faststack.app.UIState"),
        patch("faststack.app.QCoreApplication"),
        patch("faststack.app.Keybinder"),
        patch("faststack.app.find_images", return_value=[]),
    ):
        controller = AppController(tmp_path, engine)

        # Additional mocks needed for jump_to_last_uploaded
        controller.ui_state = Mock()
        controller.sidecar = Mock()
        controller.update_status_message = Mock()
        controller.jump_to_image = Mock()

        # Make jump_to_image actually update the index to support state-based assertions
        def update_index(index):
            controller.current_index = index

        controller.jump_to_image.side_effect = update_index

        return controller


def test_jump_to_last_uploaded_success(mock_controller):
    """Tests jumping to the last uploaded image in a list."""
    img1 = ImageFile(Path("img1.jpg"))
    img2 = ImageFile(Path("img2.jpg"))
    img3 = ImageFile(Path("img3.jpg"))
    mock_controller.image_files = [img1, img2, img3]
    mock_controller.current_index = 0

    # Define metadata: img1 and img3 are uploaded
    meta1 = EntryMetadata(uploaded=True)
    meta2 = EntryMetadata(uploaded=False)
    meta3 = EntryMetadata(uploaded=True)

    def side_effect(stem, **kwargs):
        return {"img1": meta1, "img2": meta2, "img3": meta3}.get(stem, EntryMetadata())

    mock_controller.sidecar.get_metadata.side_effect = side_effect

    mock_controller.jump_to_last_uploaded()

    # Should jump to index 2 (img3)
    assert mock_controller.current_index == 2
    # Should emit grid scroll signal
    mock_controller.ui_state.gridScrollToIndex.emit.assert_called_once_with(2)


def test_jump_to_last_uploaded_already_there(mock_controller):
    """Tests behavior when already at the last uploaded image."""
    img1 = ImageFile(Path("img1.jpg"))
    mock_controller.image_files = [img1]
    mock_controller.current_index = 0

    meta1 = EntryMetadata(uploaded=True)
    mock_controller.sidecar.get_metadata.return_value = meta1

    mock_controller.jump_to_last_uploaded()

    # Should stay at index 0
    assert mock_controller.current_index == 0
    mock_controller.update_status_message.assert_called_with(
        "Already at last uploaded image"
    )


def test_jump_to_last_uploaded_none_found(mock_controller):
    """Tests behavior when no images are marked as uploaded."""
    img1 = ImageFile(Path("img1.jpg"))
    img2 = ImageFile(Path("img2.jpg"))
    mock_controller.image_files = [img1, img2]
    mock_controller.current_index = 0

    mock_controller.sidecar.get_metadata.return_value = EntryMetadata(uploaded=False)

    mock_controller.jump_to_last_uploaded()

    # Should stay at index 0
    assert mock_controller.current_index == 0
    mock_controller.update_status_message.assert_called_with(
        "No uploaded images found in this folder"
    )


def test_jump_to_last_uploaded_empty_folder(mock_controller):
    """Tests behavior when the folder is empty."""
    mock_controller.image_files = []
    mock_controller.jump_to_last_uploaded()
    mock_controller.update_status_message.assert_called_with(
        "No images in current folder"
    )


def test_jump_to_last_uploaded_one(mock_controller):
    """Tests jumping when only one uploaded image exists."""
    # Only index 1 is uploaded
    meta1 = EntryMetadata(uploaded=False)
    meta2 = EntryMetadata(uploaded=True)
    meta3 = EntryMetadata(uploaded=False)

    img1 = ImageFile(Path("img1.jpg"))
    img2 = ImageFile(Path("img2.jpg"))
    img3 = ImageFile(Path("img3.jpg"))
    mock_controller.image_files = [img1, img2, img3]
    mock_controller.current_index = 0

    def side_effect(stem, **kwargs):
        return {"img1": meta1, "img2": meta2, "img3": meta3}.get(stem, EntryMetadata())

    mock_controller.sidecar.get_metadata.side_effect = side_effect

    mock_controller.jump_to_last_uploaded()

    # Should jump to index 1
    assert mock_controller.current_index == 1
