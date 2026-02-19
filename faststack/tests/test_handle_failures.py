import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication

from faststack.deletion_types import (
    DeletionErrorCodes,
    DeleteFailure,
    DeleteJob,
    DeleteResult,
)

# Ensure QApplication exists before importing/using Qt classes
if not QApplication.instance():
    _app = QApplication(sys.argv)


class DummyController:
    """
    Minimal object to act as `self` for AppController._handle_delete_failures.

    We avoid MagicMock(spec=AppController) because many attributes used here
    (_delete_executor, _pending_delete_jobs, etc.) are instance attributes that
    won't exist on the class spec.
    """

    pass


def test_handle_delete_failures_recycle_codes():
    """
    Verify that _handle_delete_failures correctly matches string codes to the recycle_codes set.

    Expected behavior:
    - If failure code is a recycle failure (e.g. "recycle_failed"), we prompt for permanent delete
      and schedule the permanent delete worker.
    - If failure code is non-recycle, we rollback UI and do NOT prompt.
    """
    from faststack.app import AppController

    controller = DummyController()

    # State used by _handle_delete_failures
    controller.active_recycle_bins = set()
    controller.delete_history = []
    controller.undo_history = []
    controller._pending_delete_jobs = {}

    # Helpers / UI hooks used by the method
    controller._key = lambda p: str(p)
    controller.main_window = MagicMock()
    controller.update_status_message = MagicMock()
    controller._rollback_ui_items = MagicMock()
    controller._rebuild_path_to_index = MagicMock()
    controller.sync_ui_state = MagicMock()

    # Signals / executor used by the method
    controller._deleteFinished = MagicMock()
    controller._deleteFinished.emit = MagicMock()

    controller._delete_executor = MagicMock()
    fut = MagicMock()
    fut.add_done_callback = MagicMock()
    controller._delete_executor.submit.return_value = fut

    # Worker referenced by submit (doesn't need to run)
    controller._perm_delete_worker = MagicMock()

    # Bind the real method onto the dummy instance
    controller._handle_delete_failures = AppController._handle_delete_failures.__get__(
        controller, AppController
    )

    # Create a result with a RECYCLE_FAILED failure
    fail_code = DeletionErrorCodes.RECYCLE_FAILED.value  # "recycle_failed"
    result = DeleteResult(
        job_id=123,
        failures=[DeleteFailure(jpg=Path("foo.jpg"), code=fail_code)],
    )

    job = DeleteJob(
        job_id=123,
        removed_items=[(0, MagicMock(path=Path("foo.jpg")))],
        action_type="loupe",
        timestamp=0,
        cancel_event=threading.Event(),
        previous_index=0,
        images_to_delete=[],
    )

    # Patch confirm_permanent_delete in faststack.app (where it's used)
    with patch(
        "faststack.app.confirm_permanent_delete", return_value=True
    ) as mock_confirm:
        controller._handle_delete_failures(result, job)

        assert mock_confirm.called, "Should have prompted for permanent delete"
        assert (
            123 in controller._pending_delete_jobs
        ), "Job should be stored in pending map"
        assert (
            controller._delete_executor.submit.called
        ), "Should have submitted perm delete worker"
        assert (
            fut.add_done_callback.called
        ), "Should have registered callback on the future"

    # Non-recycle code: should rollback, not prompt
    controller._pending_delete_jobs.clear()
    controller._rollback_ui_items.reset_mock()
    controller._delete_executor.submit.reset_mock()
    fut.add_done_callback.reset_mock()

    result.failures[0].code = "some_other_error"
    with patch(
        "faststack.app.confirm_permanent_delete", return_value=True
    ) as mock_confirm:
        controller._handle_delete_failures(result, job)

        assert not mock_confirm.called, "Should NOT have prompted for non-recycle error"
        assert (
            controller._rollback_ui_items.called
        ), "Should have rolled back UI for non-recycle error"
        assert (
            123 not in controller._pending_delete_jobs
        ), "Job should NOT be kept pending for non-recycle error"
        assert (
            not controller._delete_executor.submit.called
        ), "Should NOT submit perm delete for non-recycle error"


if __name__ == "__main__":
    try:
        test_handle_delete_failures_recycle_codes()
        print("Test passed!")
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(1)
