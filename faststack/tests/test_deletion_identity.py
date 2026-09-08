"""Regression coverage for FS-P9-001 deletion authorization."""

import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import faststack.app as app_module
from faststack.app import AppController
from faststack.deletion_types import (
    DeleteFailure,
    DeleteJob,
    DeleteResult,
    DeleteWorkItem,
    DeletionErrorCodes,
)
from faststack.io.deletion import (
    SourceChangedError,
    capture_file_identity,
    permanently_delete_image_files,
)
from faststack.models import ImageFile


def _work_item(jpg: Path, raw: Path | None = None) -> DeleteWorkItem:
    return DeleteWorkItem(
        jpg,
        raw,
        capture_file_identity(jpg),
        capture_file_identity(raw) if raw else None,
    )


def _replace(path: Path, contents: bytes = b"replacement") -> None:
    replacement = path.with_name(f".{path.name}.replacement")
    replacement.write_bytes(contents)
    os.replace(replacement, path)


@pytest.mark.parametrize("target", ["jpg", "raw"])
def test_replaced_pair_member_is_not_recycled(tmp_path, target):
    jpg = tmp_path / "image.jpg"
    raw = tmp_path / "image.CR2"
    jpg.write_bytes(b"original-jpg")
    raw.write_bytes(b"original-raw")
    item = _work_item(jpg, raw)

    _replace(jpg if target == "jpg" else raw)
    result = AppController._delete_worker(1, [item], threading.Event())

    assert result["successes"] == []
    assert result["failures"][0]["code"] == DeletionErrorCodes.SOURCE_CHANGED
    assert jpg.exists() and raw.exists()
    assert (jpg if target == "jpg" else raw).read_bytes() == b"replacement"


def test_raw_expected_absent_but_appearing_is_not_recycled(tmp_path):
    jpg = tmp_path / "image.jpg"
    raw = tmp_path / "image.CR2"
    jpg.write_bytes(b"original")
    item = _work_item(jpg, raw)
    raw.write_bytes(b"new raw")

    result = AppController._delete_worker(2, [item], threading.Event())

    assert result["failures"][0]["code"] == DeletionErrorCodes.SOURCE_CHANGED
    assert jpg.exists() and raw.read_bytes() == b"new raw"


def test_unchanged_jpg_and_pair_recycle_successfully(tmp_path):
    jpg_only = tmp_path / "single.jpg"
    pair_jpg = tmp_path / "pair.jpg"
    pair_raw = tmp_path / "pair.CR2"
    for path in (jpg_only, pair_jpg, pair_raw):
        path.write_bytes(b"unchanged")

    result = AppController._delete_worker(
        3,
        [_work_item(jpg_only), _work_item(pair_jpg, pair_raw)],
        threading.Event(),
    )

    assert len(result["successes"]) == 2
    assert result["failures"] == []
    assert not jpg_only.exists() and not pair_jpg.exists() and not pair_raw.exists()


def test_replacement_during_windows_sharing_retry_is_preserved(tmp_path, monkeypatch):
    source = tmp_path / "retry.jpg"
    source.write_bytes(b"original")
    expected = capture_file_identity(source)
    first_attempt = threading.Event()
    replacement_done = threading.Event()
    real_replace = os.replace
    calls = 0

    def replace(src, dest):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_attempt.set()
            assert replacement_done.wait(2)
            error = OSError("sharing violation")
            error.winerror = 32
            raise error
        return real_replace(src, dest)

    os_proxy = SimpleNamespace(name="nt", replace=replace)
    monkeypatch.setattr(app_module, "os", os_proxy)
    monkeypatch.setattr(app_module, "_RECYCLE_SHARING_RETRY_DELAYS", (0.0,))

    def replace_source():
        assert first_attempt.wait(2)
        _replace(source)
        replacement_done.set()

    thread = threading.Thread(target=replace_source)
    thread.start()
    with pytest.raises(SourceChangedError):
        AppController._move_to_recycle(source, expected_identity=expected)
    thread.join(timeout=2)
    assert source.read_bytes() == b"replacement"


def test_replacement_before_shutil_fallback_is_preserved(tmp_path, monkeypatch):
    source = tmp_path / "fallback.jpg"
    source.write_bytes(b"original")
    expected = capture_file_identity(source)
    replace_called = threading.Event()
    replacement_done = threading.Event()
    real_replace = os.replace

    def fail_replace(src, dest):
        replace_called.set()
        assert replacement_done.wait(2)
        raise OSError("force fallback")

    def replace_source():
        assert replace_called.wait(2)
        replacement = source.with_name(f".{source.name}.replacement")
        replacement.write_bytes(b"replacement")
        real_replace(replacement, source)
        replacement_done.set()

    monkeypatch.setattr(app_module.os, "replace", fail_replace)
    move = MagicMock()
    monkeypatch.setattr(app_module.shutil, "move", move)
    thread = threading.Thread(target=replace_source)
    thread.start()
    with pytest.raises(SourceChangedError):
        AppController._move_to_recycle(source, expected_identity=expected)
    thread.join(timeout=2)
    move.assert_not_called()
    assert source.read_bytes() == b"replacement"


def test_replacement_while_permanent_confirmation_open_is_preserved(tmp_path):
    jpg = tmp_path / "permanent.jpg"
    jpg.write_bytes(b"original")
    img = ImageFile(jpg)
    identity = capture_file_identity(jpg)
    confirmation_open = threading.Event()
    replacement_done = threading.Event()

    def replace_source():
        confirmation_open.wait(2)
        _replace(jpg)
        replacement_done.set()

    thread = threading.Thread(target=replace_source)
    thread.start()
    confirmation_open.set()
    assert replacement_done.wait(2)
    with pytest.raises(SourceChangedError):
        permanently_delete_image_files(img, (identity, None))
    thread.join(timeout=2)
    assert jpg.read_bytes() == b"replacement"


def test_unchanged_confirmed_permanent_delete_succeeds(tmp_path):
    jpg = tmp_path / "confirmed.jpg"
    raw = tmp_path / "confirmed.CR2"
    jpg.write_bytes(b"jpg")
    raw.write_bytes(b"raw")
    img = ImageFile(jpg, raw)
    work_item = _work_item(jpg, raw)

    result = AppController._perm_delete_worker(9, [(0, img, work_item)])

    assert result["perm_success"] == [(0, img)]
    assert result["perm_fail"] == []
    assert not jpg.exists() and not raw.exists()


def test_identity_mismatch_rolls_back_without_permanent_prompt(tmp_path):
    jpg = tmp_path / "rollback.jpg"
    jpg.write_bytes(b"original")
    img = ImageFile(jpg)
    controller = MagicMock()
    controller._key.side_effect = lambda path: str(path)
    controller._rollback_ui_items = MagicMock()
    controller._rebuild_path_to_index = MagicMock()
    controller.sync_ui_state = MagicMock()
    result = DeleteResult(
        job_id=7,
        failures=[
            DeleteFailure(
                jpg=jpg,
                code=DeletionErrorCodes.SOURCE_CHANGED.value,
                message="changed",
            )
        ],
    )
    job = DeleteJob(
        7,
        [(0, img)],
        "test",
        0.0,
        threading.Event(),
        0,
        [img],
    )

    with patch("faststack.app.confirm_permanent_delete") as confirm:
        AppController._handle_delete_failures(controller, result, job)

    controller._rollback_ui_items.assert_called_once_with([(0, img)], job)
    confirm.assert_not_called()
