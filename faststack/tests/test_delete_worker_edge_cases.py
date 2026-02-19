import threading
from unittest.mock import MagicMock, patch
import pytest
from pathlib import Path
from faststack.app import AppController
from faststack.deletion_types import DeletionErrorCodes


def test_delete_worker_invalid_item_shape(tmp_path):
    """Verify worker handles invalid item shapes gracefully (no crash)."""
    job_id = 999
    # Invalid items: not a tuple/list, or wrong length
    images_to_delete = [
        "not_a_tuple",
        ("only_one_item",),
        (
            tmp_path / "ok.jpg",
            tmp_path / "ok.raw",
        ),  # Valid one to ensure continued processing
    ]

    # Create the valid files
    (tmp_path / "ok.jpg").touch()
    (tmp_path / "ok.raw").touch()

    cancel_event = threading.Event()

    result = AppController._delete_worker(job_id, images_to_delete, cancel_event)

    # Should complete without crashing
    assert result["status"] == "completed"

    # 2 invalid items should result in 2 failures with INVALID_WORK_ITEM
    # 1 valid item should be a success
    assert len(result["failures"]) == 2
    assert result["failures"][0]["code"] == DeletionErrorCodes.INVALID_WORK_ITEM
    assert result["failures"][1]["code"] == DeletionErrorCodes.INVALID_WORK_ITEM

    assert len(result["successes"]) == 1
    assert Path(result["successes"][0]["jpg"]) == tmp_path / "ok.jpg"


@patch("faststack.app.AppController._move_to_recycle")
def test_delete_worker_permission_error(mock_recycle, tmp_path):
    """Verify PermissionError is mapped to PERMISSION_DENIED code."""
    job_id = 888
    img_path = tmp_path / "locked.jpg"
    img_path.touch()

    images_to_delete = [(img_path, None)]
    cancel_event = threading.Event()

    # Mock recycle to raise PermissionError
    mock_recycle.side_effect = PermissionError("Access denied")

    result = AppController._delete_worker(job_id, images_to_delete, cancel_event)

    assert len(result["failures"]) == 1
    failure = result["failures"][0]
    assert failure["code"] == DeletionErrorCodes.PERMISSION_DENIED
    assert Path(failure["jpg"]) == img_path


def test_delete_worker_cancellation_safe_unpack(tmp_path):
    """Verify cancellation loop also handles invalid shapes safely."""
    job_id = 777
    # 1. First item valid (will be processed)
    # 2. Second item INVALID (will be skipped in main loop? No, we want to cancel BEFORE it)
    # Actually, to test cancellation loop, we need to set cancel_event.

    img1 = tmp_path / "1.jpg"
    img1.touch()

    # We want to simulate cancellation happening.
    # We can't easily interrupt the loop from outside in a synchronous test without threading.
    # But we can pass a pre-set cancel_event!
    # If cancel_event is set at start, ALL items go to cancellation loop immediately?
    # Let's check the code:
    # for i, item in enumerate(images_to_delete):
    #     if cancel_event.is_set(): ... break

    # So if we set it immediately, item 0 triggers the break.
    # formatting: cancel_index = 0.
    # remaining = images_to_delete[0:] -> All items.

    cancel_event = threading.Event()
    cancel_event.set()

    images_to_delete = [
        (img1, None),  # Valid
        "invalid_shape",  # Invalid
        (1, 2, 3),  # Invalid length
    ]

    result = AppController._delete_worker(job_id, images_to_delete, cancel_event)

    assert result["cancelled"] is True

    # The cancellation loop should run for all 3 items.
    # It should record failures for valid items (as CANCELLED).
    # It should gracefully skip invalid items (no crash).

    # We expect 1 failure (the valid item, code=CANCELLED)
    # The invalid items are skipped in the cancellation loop with "continue"
    assert len(result["failures"]) == 1
    assert result["failures"][0]["code"] == DeletionErrorCodes.CANCELLED
    assert Path(result["failures"][0]["jpg"]) == img1
