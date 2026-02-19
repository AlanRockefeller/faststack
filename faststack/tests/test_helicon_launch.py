from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from faststack.app import AppController


@dataclass(frozen=True)
class DummyImage:
    """Minimal stand-in for faststack.models.ImageFile used by launch_helicon()."""

    path: Path
    raw_pair: Path | None = None


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
    ):

        controller = AppController(image_dir=Path("c:/images"), engine=engine)

    # Provide image_files as simple objects with `.path` and `.raw_pair`
    img1 = DummyImage(
        path=Path("c:/images/img1.jpg"), raw_pair=Path("c:/images/img1.CR2")
    )
    img2 = DummyImage(path=Path("c:/images/img2.jpg"), raw_pair=None)  # No RAW fallback
    controller.image_files = [img1, img2]

    # Define a single stack covering both images
    controller.stacks = [[0, 1]]

    # Mock internal methods called by launch_helicon()
    controller._launch_helicon_with_files = MagicMock(return_value=True)
    controller.clear_all_stacks = MagicMock()
    controller.sync_ui_state = MagicMock()

    return controller


def _called_file_list(controller: AppController) -> list[Path]:
    """Helper to extract the list[Path] passed to _launch_helicon_with_files."""
    controller._launch_helicon_with_files.assert_called_once()
    # _launch_helicon_with_files(files) => first positional arg is the list
    return controller._launch_helicon_with_files.call_args[0][0]


def test_launch_helicon_raw_preferred(mock_controller):
    """use_raw=True: prefer RAW when available, fall back to JPG."""
    mock_controller.launch_helicon(use_raw=True)

    expected_files = [
        Path("c:/images/img1.CR2"),  # RAW preferred
        Path("c:/images/img2.jpg"),  # fallback
    ]

    assert _called_file_list(mock_controller) == expected_files


def test_launch_helicon_jpg_only(mock_controller):
    """use_raw=False: always use JPG."""
    mock_controller.launch_helicon(use_raw=False)

    expected_files = [
        Path("c:/images/img1.jpg"),
        Path("c:/images/img2.jpg"),
    ]

    assert _called_file_list(mock_controller) == expected_files


def test_launch_helicon_no_stacks(mock_controller):
    """If no stacks defined, it should not launch."""
    mock_controller.stacks = []
    mock_controller.launch_helicon()

    mock_controller._launch_helicon_with_files.assert_not_called()


def test_uistate_delegation(mock_controller):
    """UIState should delegate launch_helicon(use_raw) correctly."""
    from faststack.ui.provider import UIState

    ui_state = UIState(mock_controller)

    # use_raw=True
    ui_state.launch_helicon(True)
    files = _called_file_list(mock_controller)
    assert files[0].suffix.upper() == ".CR2"

    # Reset mock for the next call
    mock_controller._launch_helicon_with_files.reset_mock()

    # use_raw=False
    ui_state.launch_helicon(False)
    files = _called_file_list(mock_controller)
    assert files[0].suffix.lower() == ".jpg"
