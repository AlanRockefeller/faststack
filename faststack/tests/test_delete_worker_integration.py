import os
import threading
from pathlib import Path

import pytest

from faststack.app import AppController


@pytest.fixture
def temp_env(tmp_path):
    """Creates a temporary environment with images and folders."""
    img_dir = tmp_path / "images"
    img_dir.mkdir()

    # Pair 1: JPG + RAW
    (img_dir / "test1.jpg").touch()
    (img_dir / "test1.CR2").touch()

    # Pair 2: JPG only
    (img_dir / "test2.jpg").touch()

    return img_dir


def test_delete_worker_integration_success(temp_env):
    """Verifies that _delete_worker correctly moves files and returns success dicts."""
    img_dir = temp_env

    job_id = 123
    images_to_delete = [
        (img_dir / "test1.jpg", img_dir / "test1.CR2"),
        (img_dir / "test2.jpg", None),
    ]
    cancel_event = threading.Event()

    result = AppController._delete_worker(job_id, images_to_delete, cancel_event)

    # Verify structure
    assert result["job_id"] == job_id
    assert result["status"] == "completed"
    assert len(result["successes"]) == 2
    assert len(result["warnings"]) == 0
    assert len(result["failures"]) == 0

    # Verify file movements
    successes = result["successes"]

    # Item 0 (JPG+RAW)
    item0 = successes[0]
    orig_jpg0 = Path(item0["jpg"])
    bin_jpg0 = Path(item0["recycled_jpg"])
    orig_raw0 = Path(item0["raw"]) if item0["raw"] else None
    bin_raw0 = Path(item0["recycled_raw"]) if item0["recycled_raw"] else None

    assert not orig_jpg0.exists()
    assert bin_jpg0.exists()
    if orig_raw0:
        assert not orig_raw0.exists()
    if bin_raw0:
        assert bin_raw0.exists()

    # Item 1 (JPG only)
    item1 = successes[1]
    orig_jpg1 = Path(item1["jpg"])
    bin_jpg1 = Path(item1["recycled_jpg"])
    assert not orig_jpg1.exists()
    assert bin_jpg1.exists()
    assert item1["raw"] is None

    # Verify recycle bin structure
    recycle_root = img_dir / "image recycle bin"
    assert recycle_root.exists()


def test_delete_worker_integration_rollback(temp_env, monkeypatch):
    """
    Verifies best-effort semantics when moving the RAW fails.

    Portable: we simulate the RAW move failure deterministically by patching BOTH
    faststack.app.os.replace and faststack.app.shutil.move and matching by basename.
    """
    img_dir = temp_env
    raw_path = img_dir / "test1.CR2"
    jpg_path = img_dir / "test1.jpg"

    job_id = 456
    images_to_delete = [(jpg_path, raw_path)]
    cancel_event = threading.Event()

    import faststack.app as app_mod

    raw_name = raw_path.name.lower()

    # Save real functions
    real_replace = app_mod.os.replace
    real_move = app_mod.shutil.move

    def _is_raw_src(src) -> bool:
        # src is typically a string in the worker (str(Path))
        s = os.fspath(src)
        # Match by basename only (robust across different path spellings)
        return Path(s).name.lower() == raw_name

    def replace_side_effect(src, dst, *args, **kwargs):
        if _is_raw_src(src):
            raise PermissionError("Mocked RAW move failure (os.replace)")
        return real_replace(src, dst)

    def move_side_effect(src, dst, *args, **kwargs):
        if _is_raw_src(src):
            raise PermissionError("Mocked RAW move failure (shutil.move)")
        return real_move(src, dst)

    monkeypatch.setattr(app_mod.os, "replace", replace_side_effect)
    monkeypatch.setattr(app_mod.shutil, "move", move_side_effect)

    result = AppController._delete_worker(job_id, images_to_delete, cancel_event)

    assert result["status"] == "completed"

    # Best-effort partial success:
    # - JPG moved => success
    # - RAW failed => warning
    assert len(result["successes"]) == 1
    assert len(result["warnings"]) == 1
    assert len(result["failures"]) == 0

    # Check Success entry (JPG moved, RAW not moved)
    s = result["successes"][0]
    assert Path(s["jpg"]) == jpg_path
    assert s["recycled_raw"] is None

    # Check Warning entry
    warning_entry = result["warnings"][0]
    assert Path(warning_entry["raw"]) == raw_path
    assert "message" in warning_entry

    # Verify JPG is gone (moved)
    assert not jpg_path.exists()
    # Verify RAW is still there (failed to move)
    assert raw_path.exists()
