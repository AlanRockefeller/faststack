import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from faststack.app import (
    _shutdown_force_exit_allowed,
    _unresolved_shutdown_persistence,
)
from faststack.deletion_types import DeleteWorkItem
from faststack.io.deletion import capture_file_identity
from faststack.io.sidecar import SidecarManager
from faststack.io.utils import compute_path_hash
from faststack.models import EntryMetadata, ImageFile
from faststack.thumbnail_view.model import (
    ThumbnailEntry,
    ThumbnailModel,
    filesystem_source_identity,
)


def _successful_delete(job_id: int, source: Path, recycled: Path) -> dict:
    return {
        "job_id": job_id,
        "successes": [
            {
                "jpg": source,
                "recycled_jpg": recycled,
                "raw": None,
                "recycled_raw": None,
            }
        ],
        "failures": [],
        "cancelled": False,
    }


def test_batch_persistence_failure_keeps_state_and_warning(app_controller):
    path = app_controller.image_dir / "one.jpg"
    path.write_bytes(b"one")
    app_controller.image_files = [ImageFile(path)]
    app_controller._all_images = list(app_controller.image_files)
    app_controller.current_index = 0
    app_controller.batch_start_index = 0
    meta = EntryMetadata()
    app_controller.sidecar.get_metadata.return_value = meta
    app_controller.sidecar.save.return_value = False
    app_controller.sidecar.dirty = True
    app_controller.sidecar.last_save_error = "disk full"

    assert app_controller.end_current_batch() is False
    assert app_controller.batches == [[0, 0]]
    assert meta.batch is True
    assert app_controller.sidecar.dirty is True
    messages = [
        call.args[0] for call in app_controller.update_status_message.call_args_list
    ]
    assert "Metadata could not be saved; changes are retained for retry." in messages
    assert not any(message.startswith("Batch defined:") for message in messages)


def test_stack_persistence_success_and_failure_status(app_controller):
    app_controller.stacks = [[0, 0]]
    app_controller.sidecar.save.return_value = False
    app_controller.sidecar.dirty = True
    app_controller.sidecar.last_save_error = "read only"
    assert app_controller.clear_all_stacks() is False
    assert app_controller.stacks == []
    assert not any(
        call.args[0] == "All stacks cleared"
        for call in app_controller.update_status_message.call_args_list
    )

    app_controller.update_status_message.reset_mock()
    app_controller.sidecar.save.return_value = True
    assert app_controller.clear_all_stacks() is True
    app_controller.update_status_message.assert_called_with("All stacks cleared")


def test_overlapping_delete_completion_preserves_action_order(app_controller):
    path_a = app_controller.image_dir / "a.jpg"
    path_b = app_controller.image_dir / "b.jpg"
    path_a.write_bytes(b"a")
    path_b.write_bytes(b"b")
    app_controller.image_files = [ImageFile(path_a), ImageFile(path_b)]

    job_a = app_controller._delete_indices([0], "a")["job_id"]
    job_b = app_controller._delete_indices([0], "b")["job_id"]
    bin_dir = app_controller.image_dir / "image recycle bin"
    bin_dir.mkdir()
    app_controller._on_delete_finished(
        _successful_delete(job_a, path_a, bin_dir / "a._fs_12345678.jpg")
    )

    assert [entry[0] for entry in app_controller.undo_history] == [
        "delete",
        "pending_delete",
    ]
    assert app_controller.undo_history[-1][1] == job_b
    app_controller.undo_delete()
    assert app_controller._pending_delete_jobs[job_b].undo_requested is True
    assert [entry[0] for entry in app_controller.undo_history] == ["delete"]


def test_overlapping_delete_reverse_completion_still_uses_action_order(app_controller):
    path_a = app_controller.image_dir / "a.jpg"
    path_b = app_controller.image_dir / "b.jpg"
    path_a.write_bytes(b"a")
    path_b.write_bytes(b"b")
    app_controller.image_files = [ImageFile(path_a), ImageFile(path_b)]
    job_a = app_controller._delete_indices([0], "a")["job_id"]
    job_b = app_controller._delete_indices([0], "b")["job_id"]
    bin_dir = app_controller.image_dir / "image recycle bin"
    bin_dir.mkdir()

    app_controller._on_delete_finished(
        _successful_delete(job_b, path_b, bin_dir / "b._fs_12345678.jpg")
    )
    assert [entry[0] for entry in app_controller.undo_history] == [
        "pending_delete",
        "delete",
    ]
    app_controller._on_delete_finished(
        _successful_delete(job_a, path_a, bin_dir / "a._fs_12345678.jpg")
    )
    assert [entry[0] for entry in app_controller.undo_history] == ["delete", "delete"]
    assert [entry[1][0][0] for entry in app_controller.undo_history] == [path_a, path_b]


def test_permanent_fallback_keeps_placeholder_until_final_settlement(app_controller):
    path = app_controller.image_dir / "fallback.jpg"
    path.write_bytes(b"data")
    image = ImageFile(path)
    app_controller.image_files = [image]
    job_id = app_controller._delete_indices([0], "fallback")["job_id"]
    recycle_failure = {
        "job_id": job_id,
        "successes": [],
        "failures": [{"jpg": path, "raw": None, "code": "recycle_failed"}],
        "cancelled": False,
    }
    with patch("faststack.app.confirm_permanent_delete", return_value=True):
        app_controller._on_delete_finished(recycle_failure)
    assert app_controller.undo_history[-1][:2] == ("pending_delete", job_id)

    app_controller._on_delete_finished(
        {
            "job_id": job_id,
            "_perm_result": True,
            "perm_success": [(0, image)],
            "perm_fail": [],
            "perm_changed": [],
        }
    )
    assert all(entry[0] != "pending_delete" for entry in app_controller.undo_history)


def test_delete_placeholder_multi_image_and_failed_settlement(app_controller):
    paths = [app_controller.image_dir / f"{name}.jpg" for name in ("a", "b")]
    for path in paths:
        path.write_bytes(path.stem.encode())
    app_controller.image_files = [ImageFile(path) for path in paths]
    job_id = app_controller._delete_indices([0, 1], "multi")["job_id"]
    bin_dir = app_controller.image_dir / "image recycle bin"
    bin_dir.mkdir()
    result = {
        "job_id": job_id,
        "successes": [
            _successful_delete(job_id, path, bin_dir / f"{path.stem}._fs_12345678.jpg")[
                "successes"
            ][0]
            for path in paths
        ],
        "failures": [],
        "cancelled": False,
    }
    app_controller._on_delete_finished(result)
    assert [entry[0] for entry in app_controller.undo_history] == ["delete", "delete"]
    assert [entry[1][0][0] for entry in app_controller.undo_history] == paths

    failed_path = app_controller.image_dir / "failed.jpg"
    failed_path.write_bytes(b"failed")
    app_controller.image_files = [ImageFile(failed_path)]
    failed_job = app_controller._delete_indices([0], "failed")["job_id"]
    app_controller._on_delete_finished(
        {"job_id": failed_job, "successes": [], "failures": [], "cancelled": True}
    )
    assert all(
        not (entry[0] == "pending_delete" and entry[1] == failed_job)
        for entry in app_controller.undo_history
    )


def test_pending_delete_suppression_is_job_owned_not_timed(app_controller):
    path = app_controller.image_dir / "slow.jpg"
    path.write_bytes(b"original")
    identity = capture_file_identity(path)
    item = DeleteWorkItem(path, None, identity, None)
    key = app_controller._key(path)

    app_controller._register_pending_delete_paths(10, [item])
    app_controller._register_pending_delete_paths(20, [item])
    assert app_controller._is_pending_delete_path(key, path, time.monotonic() + 10_000)
    app_controller._release_pending_delete_paths(10, [item])
    assert app_controller._is_pending_delete_path(key, path, time.monotonic() + 10_000)

    replacement = app_controller.image_dir / "replacement.jpg"
    replacement.write_bytes(b"changed!")
    os.utime(replacement, ns=(identity.mtime_ns, identity.mtime_ns))
    os.replace(replacement, path)
    assert not app_controller._is_pending_delete_path(key, path, time.monotonic())
    app_controller._release_pending_delete_paths(20, [item])
    assert key not in app_controller._pending_delete_identities


def test_delete_settlement_coalesces_final_reconciliation(app_controller):
    app_controller._pending_delete_jobs = {}
    app_controller._index_rescan_needed = True
    app_controller._index_scan_inflight = False

    def settle_flag():
        app_controller._index_rescan_needed = False

    app_controller._maybe_start_index_scan = MagicMock(side_effect=settle_flag)
    app_controller._clear_delete_identity_state_if_settled()
    app_controller._clear_delete_identity_state_if_settled()
    app_controller._maybe_start_index_scan.assert_called_once_with()


def test_recycle_purge_removes_persisted_jpg_raw_metadata(app_controller, tmp_path):
    image_dir = tmp_path / "purge"
    image_dir.mkdir()
    jpg = image_dir / "pair.jpg"
    raw = image_dir / "pair.nef"
    jpg.write_bytes(b"jpg")
    raw.write_bytes(b"raw")
    sidecar = SidecarManager(image_dir, None)
    sidecar.get_metadata(jpg).favorite = True
    assert sidecar.save()
    app_controller.sidecar = sidecar

    bin_dir = image_dir / "image recycle bin"
    bin_dir.mkdir()
    recycled_jpg = bin_dir / "pair._fs_12345678.jpg"
    recycled_raw = bin_dir / "pair._fs_12345678.nef"
    jpg.rename(recycled_jpg)
    raw.rename(recycled_raw)
    record = ((jpg, recycled_jpg), (raw, recycled_raw))
    app_controller.delete_history = [record]
    app_controller.undo_history = [("delete", record, time.time())]
    app_controller.active_recycle_bins = {bin_dir}

    assert sidecar.get_metadata(jpg, create=False) is not None
    assert app_controller.empty_recycle_bin() is True
    assert not bin_dir.exists()
    reloaded = SidecarManager(image_dir, None)
    assert reloaded.get_metadata(jpg, create=False, migrate=False) is None
    jpg.write_bytes(b"new")
    assert reloaded.get_metadata(jpg, create=False, migrate=False) is None


def test_recycle_purge_failure_keeps_bin_history_and_dirty_metadata(app_controller):
    bin_dir = app_controller.image_dir / "image recycle bin"
    bin_dir.mkdir()
    source = app_controller.image_dir / "photo.jpg"
    recycled = bin_dir / "photo._fs_12345678.jpg"
    recycled.write_bytes(b"photo")
    record = ((source, recycled), (None, None))
    app_controller.delete_history = [record]
    app_controller.undo_history = [("delete", record, time.time())]
    app_controller.active_recycle_bins = {bin_dir}
    with patch("faststack.app.shutil.rmtree", side_effect=OSError("busy")):
        assert app_controller.empty_recycle_bin() is False
    assert bin_dir in app_controller.active_recycle_bins
    assert app_controller.delete_history == [record]


def test_recycle_purge_mixed_bins_only_retires_success(app_controller):
    good_bin = app_controller.image_dir / "good" / "image recycle bin"
    bad_bin = app_controller.image_dir / "bad" / "image recycle bin"
    good_bin.mkdir(parents=True)
    bad_bin.mkdir(parents=True)
    good_record = (
        (good_bin.parent / "good.jpg", good_bin / "good._fs_12345678.jpg"),
        (None, None),
    )
    bad_record = (
        (bad_bin.parent / "bad.jpg", bad_bin / "bad._fs_12345678.jpg"),
        (None, None),
    )
    good_record[0][1].write_bytes(b"good")
    bad_record[0][1].write_bytes(b"bad")
    app_controller.delete_history = [good_record, bad_record]
    app_controller.undo_history = [
        ("delete", good_record, 1.0),
        ("delete", bad_record, 2.0),
    ]
    app_controller.active_recycle_bins = {good_bin, bad_bin}
    real_rmtree = __import__("shutil").rmtree

    def selective_rmtree(path):
        if Path(path) == bad_bin:
            raise OSError("busy")
        real_rmtree(path)

    with patch("faststack.app.shutil.rmtree", side_effect=selective_rmtree):
        assert app_controller.cleanup_recycle_bins() is False
    assert not good_bin.exists()
    assert bad_bin.exists()
    assert app_controller.delete_history == [bad_record]
    assert app_controller.active_recycle_bins == {bad_bin}


def test_recycle_purge_sidecar_failure_retains_retry_state(app_controller, tmp_path):
    image_dir = tmp_path / "purge-failure"
    image_dir.mkdir()
    source = image_dir / "photo.jpg"
    sidecar = SidecarManager(image_dir, None)
    sidecar.get_metadata(source).favorite = True
    assert sidecar.save()
    app_controller.sidecar = sidecar
    bin_dir = image_dir / "image recycle bin"
    bin_dir.mkdir()
    recycled = bin_dir / "photo._fs_12345678.jpg"
    recycled.write_bytes(b"photo")
    record = ((source, recycled), (None, None))
    app_controller.delete_history = [record]
    app_controller.undo_history = [("delete", record, 1.0)]
    app_controller.active_recycle_bins = {bin_dir}

    def fail_save():
        sidecar._dirty = True
        return False

    with patch.object(sidecar, "save", side_effect=fail_save):
        assert app_controller.empty_recycle_bin() is False
    assert not bin_dir.exists()
    assert sidecar.dirty is True
    assert app_controller.delete_history == [record]
    assert app_controller.active_recycle_bins == {bin_dir}
    assert not any(
        call.args[0] == "Recycle bin emptied"
        for call in app_controller.update_status_message.call_args_list
    )


def test_shutdown_watchdog_protects_deferred_save_until_final_attempt():
    controller = SimpleNamespace(
        _pending_save_recovery={"photo.jpg": {"snapshot": object()}},
        _pending_edit_save_requests={},
        _saves_in_flight={},
        _pending_delete_jobs={},
        _critical_user_data_state=lambda: {},
        _shutting_down=True,
        _shutdown_final_sidecar_attempted=False,
        sidecar=SimpleNamespace(dirty=False),
    )
    assert _unresolved_shutdown_persistence(controller)
    assert not _shutdown_force_exit_allowed(controller)
    controller._pending_save_recovery.clear()
    controller._shutdown_final_sidecar_attempted = True
    assert _shutdown_force_exit_allowed(controller)


def test_thumbnail_identity_changes_on_same_mtime_replacement(tmp_path):
    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None
    path = tmp_path / "photo.jpg"
    replacement = tmp_path / "replacement.jpg"
    path.write_bytes(b"AAAA")
    controlled_mtime = int(time.time()) - 10
    os.utime(path, (controlled_mtime, controlled_mtime))
    first_stat = path.stat()
    first_identity = filesystem_source_identity(first_stat)
    replacement.write_bytes(b"different contents")
    os.utime(replacement, ns=(first_stat.st_mtime_ns, first_stat.st_mtime_ns))
    os.replace(replacement, path)
    os.utime(path, ns=(first_stat.st_mtime_ns, first_stat.st_mtime_ns))
    second_stat = path.stat()
    second_identity = filesystem_source_identity(second_stat)

    model = ThumbnailModel(tmp_path, tmp_path, thumbnail_size=200)
    first = ThumbnailEntry(
        path=path,
        name=path.name,
        is_folder=False,
        mtime_ns=first_stat.st_mtime_ns,
        source_identity=first_identity,
    )
    second = ThumbnailEntry(
        path=path,
        name=path.name,
        is_folder=False,
        mtime_ns=second_stat.st_mtime_ns,
        source_identity=second_identity,
    )
    assert first_stat.st_mtime_ns == second_stat.st_mtime_ns
    assert first_identity != second_identity
    assert compute_path_hash(first.path) == compute_path_hash(second.path)
    assert model._make_thumbnail_id(first) != model._make_thumbnail_id(second)
