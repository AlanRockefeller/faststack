from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from faststack.app import AppController


@dataclass(frozen=True)
class DummyImage:
    """Minimal stand-in for faststack.models.ImageFile."""

    path: Path
    raw_pair: Path | None = None


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
    ):
        controller = AppController(tmp_path, engine)

        # Prevent background jobs from actually running
        from concurrent.futures import Future

        controller._delete_executor = Mock()
        controller._delete_executor.submit.side_effect = lambda *a, **kw: Future()

        controller.dataChanged = Mock()
        controller.dataChanged.emit = Mock()
        controller.sync_ui_state = Mock()
        controller._do_prefetch = Mock()
        controller.update_status_message = Mock()

        controller._thumbnail_model = Mock()
        controller._thumbnail_model.rowCount.return_value = 0

        return controller


def _assert_cache_cleanup(mock_controller, deleted_paths):
    """
    Newer behavior: targeted eviction is preferred (evict_paths).
    Older behavior: clear().
    Accept either, but require at least one.
    """
    cache = mock_controller.image_cache
    called = False

    if hasattr(cache, "evict_paths") and cache.evict_paths.call_count:
        called = True
    if hasattr(cache, "clear") and cache.clear.call_count:
        called = True

    assert called, "Expected cache cleanup via evict_paths() or clear()"

    if hasattr(cache, "evict_paths") and cache.evict_paths.call_count:
        args, _kwargs = cache.evict_paths.call_args
        assert args, "evict_paths should receive at least one arg"
        arg0 = list(args[0])
        deleted_strs = {str(p) for p in deleted_paths}
        arg0_strs = {str(p) for p in arg0}
        assert deleted_strs & arg0_strs, "evict_paths should include deleted path(s)"


def test_delete_current_image_optimistic_ui(mock_controller):
    """Test that delete_current_image performs optimistic UI removal immediately."""
    img1 = DummyImage(Path("test1.jpg"))
    img2 = DummyImage(Path("test2.jpg"))

    mock_controller.image_files = [img1, img2]
    mock_controller.current_index = 0
    mock_controller.undo_history = []
    mock_controller.refresh_image_list = Mock()

    mock_controller.image_cache = Mock()
    mock_controller.image_cache.evict_paths = Mock()
    mock_controller.image_cache.clear = Mock()

    mock_controller.prefetcher = Mock()
    mock_controller.prefetcher.cancel_all = Mock()

    mock_controller.delete_current_image()

    assert len(mock_controller.image_files) == 1
    assert mock_controller.image_files[0] == img2

    _assert_cache_cleanup(mock_controller, deleted_paths=[img1.path])
    mock_controller.prefetcher.cancel_all.assert_called_once()
    mock_controller.sync_ui_state.assert_called_once()

    assert len(mock_controller.undo_history) == 1
    assert mock_controller.undo_history[0][0] == "pending_delete"


def test_delete_async_completion(mock_controller, tmp_path):
    """Test that async deletion completes and updates undo history."""
    img_path = tmp_path / "test1.jpg"
    img_path.write_text("content")

    img1 = DummyImage(img_path)
    img2 = DummyImage(Path("test2.jpg"))

    mock_controller.image_files = [img1, img2]
    mock_controller.current_index = 0
    mock_controller.undo_history = []
    mock_controller.refresh_image_list = Mock()

    mock_controller.image_cache = Mock()
    mock_controller.image_cache.evict_paths = Mock()
    mock_controller.image_cache.clear = Mock()

    mock_controller.prefetcher = Mock()
    mock_controller.prefetcher.cancel_all = Mock()

    mock_controller.delete_current_image()

    job_id = list(mock_controller._pending_delete_jobs.keys())[0]

    img_path_resolved = img_path.resolve()
    recycle_bin = (tmp_path / "image recycle bin").resolve()
    recycle_bin.mkdir(exist_ok=True)
    recycled = (recycle_bin / img_path.name).resolve()

    result = {
        "job_id": job_id,
        "successes": [
            {
                "jpg": img_path_resolved,
                "recycled_jpg": recycled,
                "raw": None,
                "recycled_raw": None,
            }
        ],
        "failures": [],
        "cancelled": False,
    }
    mock_controller._on_delete_finished(result)

    delete_entries = [e for e in mock_controller.undo_history if e[0] == "delete"]
    assert len(delete_entries) == 1
    pending_entries = [
        e for e in mock_controller.undo_history if e[0] == "pending_delete"
    ]
    assert len(pending_entries) == 0

    mock_controller.update_status_message.assert_called_with(
        "Image moved to recycle bin"
    )


def test_delete_current_image_cancel(mock_controller):
    """Test undo while pending preserves image."""
    img1 = DummyImage(Path("test1.jpg"))

    mock_controller.image_files = [img1]
    mock_controller.current_index = 0

    mock_controller.image_cache = Mock()
    mock_controller.image_cache.evict_paths = Mock()
    mock_controller.image_cache.clear = Mock()

    mock_controller.prefetcher = Mock()
    mock_controller.prefetcher.cancel_all = Mock()

    mock_controller.delete_current_image()
    assert len(mock_controller.image_files) == 0

    mock_controller.undo_delete()

    assert len(mock_controller.image_files) == 1
    assert mock_controller.image_files[0] == img1


def test_recycle_failure_restores_image_automatically(mock_controller):
    """
    Recycle-bin failure: app prompts for permanent delete.
    If user declines, image should be restored to the UI.
    """
    img1 = DummyImage(Path("test1.jpg"))
    mock_controller.image_files = [img1]
    mock_controller.current_index = 0

    mock_controller.image_cache = Mock()
    mock_controller.image_cache.evict_paths = Mock()
    mock_controller.image_cache.clear = Mock()

    mock_controller.prefetcher = Mock()
    mock_controller.prefetcher.cancel_all = Mock()

    summary = mock_controller._delete_indices([0], "test")
    job_id = summary["job_id"]

    result = {
        "job_id": job_id,
        "successes": [],
        "failures": [
            {
                "jpg": img1.path.resolve(),
                "raw": None,
                "code": "recycle_failed",
            }
        ],
        "cancelled": False,
    }

    # User declines permanent delete -> expect rollback/restore
    with patch(
        "faststack.app.confirm_permanent_delete", return_value=False
    ) as mock_confirm:
        mock_controller._on_delete_finished(result)

        mock_confirm.assert_called_once()
        # The exact arg shape may vary; just sanity-check reason kwarg if present.
        _args, kwargs = mock_confirm.call_args
        if "reason" in kwargs:
            assert kwargs["reason"] == "Recycle bin failure"

    assert len(mock_controller.image_files) == 1
    assert mock_controller.image_files[0] == img1
