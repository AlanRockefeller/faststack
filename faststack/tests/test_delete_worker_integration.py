
import os
import shutil
import threading
import uuid
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from faststack.app import AppController

@pytest.fixture
def temp_env(tmp_path):
    """Creates a temporary environment with images and folders."""
    # Create source images
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
    
    # Input for worker
    job_id = 123
    images_to_delete = [
        (img_dir / "test1.jpg", img_dir / "test1.CR2"),
        (img_dir / "test2.jpg", None)
    ]
    cancel_event = threading.Event()
    
    # Run worker (pure function)
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
    
    # Verify recycle bin structure (UUIDs)
    recycle_root = img_dir / "image recycle bin"
    assert recycle_root.exists()
    
def test_delete_worker_integration_rollback(temp_env):
    """Verifies best-effort semantics when a RAW file is locked."""
    img_dir = temp_env
    
    raw_path = img_dir / "test1.CR2"
    f = open(raw_path, "wb") 
    
    try:
        job_id = 456
        images_to_delete = [
            (img_dir / "test1.jpg", raw_path), 
        ]
        cancel_event = threading.Event()
        
        # We expect the worker to:
        # 1. Move JPG to bin
        # 2. Try to move RAW -> Fail (locked)
        # 3. Best-effort: Report success for JPG and warning for RAW
        
        result = AppController._delete_worker(job_id, images_to_delete, cancel_event)
        
        assert result["status"] == "completed" 
        
        # In best-effort partial success:
        # It appears in successes (JPG moved) AND warnings (RAW failed)
        assert len(result["successes"]) == 1
        assert len(result["warnings"]) == 1
        assert len(result["failures"]) == 0
        
        # Check Success entry
        s = result["successes"][0]
        assert Path(s["jpg"]) == img_dir / "test1.jpg"
        assert s["recycled_raw"] is None
        
        # Check Warning entry
        warning_entry = result["warnings"][0]
        assert Path(warning_entry["raw"]) == raw_path
        assert "message" in warning_entry
        
        # Verify JPG is GONE (No rollback)
        assert not (img_dir / "test1.jpg").exists()
        # Verify RAW is still there (failed to move)
        assert (img_dir / "test1.CR2").exists()
        
    finally:
        f.close()
