import pytest
import time
from unittest.mock import MagicMock, patch, Mock
from pathlib import Path
from faststack.models import ImageFile


@pytest.fixture
def app_controller(tmp_path):
    from PySide6.QtCore import QCoreApplication
    from faststack.app import AppController

    app = QCoreApplication.instance()
    if not app:
        app = QCoreApplication([])

    image_dir = tmp_path / "images"
    image_dir.mkdir()

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
        patch("faststack.app.Keybinder"),
        patch("faststack.app.UIState"),
    ):
        controller = AppController(image_dir, mock_engine, debug_cache=False)

        # Mock the executor to return a real Future we can control
        from concurrent.futures import Future

        controller._delete_executor = MagicMock()
        controller._delete_executor.submit.side_effect = lambda *a, **kw: Future()

        controller.refresh_image_list = MagicMock()
        controller.update_status_message = MagicMock()
        controller.sync_ui_state = MagicMock()
        controller.image_cache = MagicMock()
        controller.prefetcher = MagicMock()
        controller._thumbnail_model = MagicMock()
        controller._thumbnail_model.rowCount.return_value = 0

        return controller


def test_optimistic_ui_removal(app_controller):
    """Test that delete immediately removes image from UI (optimistic pattern)."""
    img_path = app_controller.image_dir / "test.jpg"
    img_path.write_text("content")

    img_file = ImageFile(img_path)
    app_controller.image_files = [img_file]
    app_controller.current_index = 0

    app_controller.delete_current_image()

    # Image removed immediately (optimistic)
    assert len(app_controller.image_files) == 0

    # Cache evicted for deleted paths (targeted, not blanket clear)
    app_controller.image_cache.evict_paths.assert_called_once()
    app_controller.prefetcher.cancel_all.assert_called_once()
    app_controller.sync_ui_state.assert_called_once()


def test_undo_pending_delete_no_disk_ops(app_controller):
    """Test that undo during pending delete restores without disk operations."""
    img_path = app_controller.image_dir / "test.jpg"
    img_path.write_text("content")

    img_file = ImageFile(img_path)
    app_controller.image_files = [img_file]
    app_controller.current_index = 0

    app_controller.delete_current_image()
    assert len(app_controller.image_files) == 0

    # Undo while still pending — should restore in-memory
    app_controller.undo_delete()

    assert len(app_controller.image_files) == 1
    assert img_path.exists()


def test_async_delete_completion(app_controller):
    """Test full async cycle: delete, worker runs, completion handler processes."""
    img_path = (app_controller.image_dir / "test.jpg").resolve()
    img_path.write_text("content")

    img_file = ImageFile(img_path)
    app_controller.image_files = [img_file]
    app_controller.current_index = 0

    # 1. Enqueue
    app_controller.delete_current_image()
    future = app_controller._delete_executor.submit.return_value

    # 2. Simulate worker side-effects
    recycle_bin = (app_controller.image_dir / "image recycle bin").resolve()
    recycle_bin.mkdir(exist_ok=True)
    recycled_path = (recycle_bin / img_path.name).resolve()
    img_path.rename(recycled_path)

    job_id = list(app_controller._pending_delete_jobs.keys())[0]

    # 3. Resolve future
    result = {
        "job_id": job_id,
        "successes": [
            {
                "jpg": img_path,
                "recycled_jpg": recycled_path,
                "raw": None,
                "recycled_raw": None,
            }
        ],
        "failures": [],
        "cancelled": False,
    }
    app_controller._on_delete_finished(result)

    # Verify completion bookkeeping (undo entries should be added)
    delete_entries = [e for e in app_controller.undo_history if e[0] == "delete"]
    assert len(delete_entries) == 1
    assert len(app_controller._pending_delete_jobs) == 0


def test_delete_rollback_on_cancel(app_controller):
    """
    Test that cancellation restores images to the list.

    NOTE:
    In the current design, cancellation is user-driven (undo while pending),
    which sets the job's cancel_event and restores the UI immediately.
    The later completion callback (cancelled=True) should not remove it again.
    """
    img_path = (app_controller.image_dir / "test.jpg").resolve()
    img_path.write_text("content")

    img_file = ImageFile(img_path)
    app_controller.image_files = [img_file]
    app_controller.current_index = 0

    # Enqueue + optimistic removal
    app_controller.delete_current_image()
    assert len(app_controller.image_files) == 0

    # User cancels via undo => immediate UI restore
    app_controller.undo_delete()
    assert len(app_controller.image_files) == 1
    assert img_path.exists()

    # Completion arrives marked cancelled
    job_id = list(app_controller._pending_delete_jobs.keys())[0]
    result = {
        "job_id": job_id,
        "successes": [],
        "failures": [{"jpg": img_path, "raw": None, "code": "cancelled"}],
        "cancelled": True,
    }
    app_controller._on_delete_finished(result)

    # Image should remain in list (no double-remove)
    assert len(app_controller.image_files) == 1
    assert img_path.exists()


def test_debounced_refresh(app_controller):
    """Test that refresh is debounced (not called per delete)."""
    img1 = ImageFile(Path("test1.jpg"))
    img2 = ImageFile(Path("test2.jpg"))
    app_controller.image_files = [img1, img2]

    # Delete both images rapidly
    app_controller._delete_indices([0], "test1")
    app_controller._delete_indices([0], "test2")

    # refresh_image_list should not have been called yet (it's debounced)
    app_controller.refresh_image_list.assert_not_called()


def test_cancel_midlight_with_real_files(app_controller):
    """
    Worker cancels after some files moved; user hits Undo mid-flight.

    Current semantics:
    - "Cancel" is initiated by undo_delete() while the job is pending.
    - undo_delete() restores UI immediately (optimistic rollback) and sets cancel_event.
    - When completion arrives with cancelled=True and some successes, the controller
      should auto-restore any moved files (best-effort) and keep the UI restored.
    """
    p1 = (app_controller.image_dir / "a.jpg").resolve()
    p2 = (app_controller.image_dir / "b.jpg").resolve()
    p3 = (app_controller.image_dir / "c.jpg").resolve()
    p1.write_text("1")
    p2.write_text("2")
    p3.write_text("3")

    img1, img2, img3 = ImageFile(p1), ImageFile(p2), ImageFile(p3)
    app_controller.image_files = [img1, img2, img3]

    summary = app_controller._delete_indices([0, 1, 2], "test")
    job_id = summary["job_id"]

    # Optimistic removal of all three
    assert len(app_controller.image_files) == 0

    # Simulate: worker moved a.jpg before cancel was processed
    recycle_bin = (app_controller.image_dir / "image recycle bin").resolve()
    recycle_bin.mkdir(exist_ok=True)
    recycled_a = (recycle_bin / "a.recycled.jpg").resolve()
    p1.rename(recycled_a)

    # User cancels mid-flight (Undo) => immediate UI restore
    app_controller.undo_delete()
    assert len(app_controller.image_files) == 3
    assert p2.exists()
    assert p3.exists()

    # Completion arrives: one success + cancellations
    app_controller._restore_from_recycle_bin_safe = Mock(return_value=(True, ""))

    result = {
        "job_id": job_id,
        "successes": [
            {"jpg": p1, "recycled_jpg": recycled_a, "raw": None, "recycled_raw": None}
        ],
        "failures": [
            {"jpg": p2, "raw": None, "code": "cancelled"},
            {"jpg": p3, "raw": None, "code": "cancelled"},
        ],
        "cancelled": True,
    }
    app_controller._on_delete_finished(result)

    # UI stays restored (no double-remove)
    assert len(app_controller.image_files) == 3

    # Auto-restore attempted for moved file
    app_controller._restore_from_recycle_bin_safe.assert_called_with(p1, recycled_a)

    # No "delete" undo entry should be added (Undo consumed it)
    delete_entries = [e for e in app_controller.undo_history if e[0] == "delete"]
    assert len(delete_entries) == 0


def test_undo_midflight_auto_restores(app_controller, tmp_path):
    """Test Policy 1: Undo mid-flight causes auto-restore of moved files without new undo entries."""
    p1 = (app_controller.image_dir / "test.jpg").resolve()
    p1.write_text("content")
    img1 = ImageFile(p1)
    app_controller.image_files = [img1]

    # Mock restore to avoid real file ops and ensure success
    app_controller._restore_from_recycle_bin_safe = Mock(return_value=(True, ""))

    summary = app_controller._delete_indices([0], "test")
    job_id = summary["job_id"]

    # Removed optimistically
    assert len(app_controller.image_files) == 0

    # User undoes immediately -> undo_requested=True
    app_controller.undo_delete()
    assert len(app_controller.image_files) == 1

    # Completion arrives (file was moved before cancel took effect)
    result = {
        "job_id": job_id,
        "successes": [
            {
                "jpg": p1,
                "recycled_jpg": Path("recycle/test.jpg"),
                "raw": None,
                "recycled_raw": None,
            }
        ],
        "failures": [],
        "cancelled": True,
    }
    app_controller._on_delete_finished(result)

    # 1. No new undo entry (consumed by auto-restore)
    delete_entries = [e for e in app_controller.undo_history if e[0] == "delete"]
    assert len(delete_entries) == 0

    # 2. UI keeps the image (restored by undo, kept by auto-restore)
    assert len(app_controller.image_files) == 1

    # 3. Restore was attempted
    app_controller._restore_from_recycle_bin_safe.assert_called_with(
        p1, Path("recycle/test.jpg")
    )
