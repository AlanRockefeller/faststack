
import sys
from unittest.mock import MagicMock, patch
from pathlib import Path
from faststack.deletion_types import DeletionErrorCodes, DeleteResult, DeleteFailure, DeleteJob

# Mocks for global functions that might be called
confirm_permanent_delete = MagicMock(return_value=True)
confirm_batch_permanent_delete = MagicMock(return_value=True)

class MockController:
    def __init__(self):
        self._pending_delete_jobs = {}
        self._deleteFinished = MagicMock()
        self._delete_executor = MagicMock()
        self._rollback_ui_items = MagicMock()
        self._rebuild_path_to_index = MagicMock()
        self.sync_ui_state = MagicMock()
        self.update_status_message = MagicMock()
        self._perm_delete_worker = MagicMock()

    def _key(self, p):
        return str(p) if p else None

    # Dynamically bind the real method we want to test
    # This ensures we test the ACTUAL logic in app.py, not a copy
    from faststack.app import AppController
    _handle_delete_failures = AppController._handle_delete_failures

@patch("faststack.app.confirm_permanent_delete", return_value=True)
@patch("faststack.app.confirm_batch_permanent_delete", return_value=True)
def test_handle_delete_failures_recycle_codes_isolation(mock_confirm, mock_batch_confirm):
    controller = MockController()
    
    # Create failure with RECYCLE_FAILED code
    fail_code = DeletionErrorCodes.RECYCLE_FAILED.value
    
    result = DeleteResult(
        job_id=1,
        failures=[
            DeleteFailure(jpg=Path("foo.jpg"), code=fail_code)
        ]
    )
    
    job = DeleteJob(
        job_id=1,
        removed_items=[(0, MagicMock(path=Path("foo.jpg")))],
        action_type="loupe", # dummy
        timestamp=0,
        cancel_event=None,
        previous_index=0,
        images_to_delete=[]
    )
    
    controller._handle_delete_failures(result, job)
    
    # Since we found a recycle code, we should NOT have called _rollback_ui_items 
    # for the full list (it would happen for non-candidates).
    # In this case, foo.jpg IS a candidate.
    # So _rollback_ui_items should NOT be called for foo.jpg
    
    # The code path:
    # if perm_candidates:
    #   to_rollback = [items NOT in candidates] -> empty
    #   if to_rollback: call... (not called)
    #   # prompt logic (omitted in copy, satisfied by pass)
    # else:
    #   call _rollback_ui_items(all)
    
    assert not controller._rollback_ui_items.called, "Should not rollback candidate for perm delete"

    # Now test with NON-recycle code
    controller._rollback_ui_items.reset_mock()
    result.failures[0].code = "some_other_error"
    
    controller._handle_delete_failures(result, job)
    
    assert controller._rollback_ui_items.called, "Should rollback non-recycle failure"

if __name__ == "__main__":
    try:
        test_handle_delete_failures_recycle_codes_isolation()
        print("Test passed!")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Test failed: {e}")
