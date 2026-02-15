import pytest
import numpy as np
from pathlib import Path
from faststack.imaging.cache import ByteLRUCache
from faststack.models import DecodedImage
from faststack.imaging.editor import ImageEditor
from faststack.io.variants import build_variant_map, norm_path
from faststack.io.indexer import find_images_with_variants

def test_cache_evict_callback_payload():
    """Verify that ByteLRUCache passes (key, value) to on_evict."""
    evicted_items = []
    
    def on_evict(k, v):
        evicted_items.append((k, v))
    
    # Small cache to trigger eviction
    cache = ByteLRUCache(max_bytes=100, on_evict=on_evict)
    
    # Create some test images
    img1 = DecodedImage(buffer=np.zeros(60, dtype=np.uint8), width=60, height=1, bytes_per_line=60, format=1)
    img2 = DecodedImage(buffer=np.zeros(60, dtype=np.uint8), width=60, height=1, bytes_per_line=60, format=1)
    
    cache["k1"] = img1
    assert len(evicted_items) == 0
    
    # Adding k2 should evict k1
    cache["k2"] = img2
    assert len(evicted_items) == 1
    assert evicted_items[0][0] == "k1"
    assert evicted_items[0][1] == img1

def test_cache_popitem_callback_payload():
    """Verify popitem passes (key, value) to on_evict."""
    evicted_items = []
    def on_evict(k, v):
        evicted_items.append((k, v))
        
    cache = ByteLRUCache(max_bytes=1000, on_evict=on_evict)
    img = DecodedImage(buffer=np.zeros(10, dtype=np.uint8), width=10, height=1, bytes_per_line=10, format=1)
    cache["k1"] = img
    
    cache.popitem()
    assert len(evicted_items) == 1
    assert evicted_items[0] == ("k1", img)

def test_image_editor_save_exception(tmp_path):
    """Verify ImageEditor.save_image raises RuntimeError if no float_image."""
    editor = ImageEditor()
    # No image loaded
    with pytest.raises(RuntimeError, match="No file path set"):
        editor.save_image()

def test_variant_normalization_consistency():
    """Verify variant mapping handles mixed-case extensions and paths consistently."""
    # Simulate mixed case extensions
    paths = [
        Path("test.JPG"),
        Path("test-backup.jpeg"),
        Path("test-developed.jpg")
    ]
    
    vmap = build_variant_map(paths)
    key_cf = "test".casefold()
    assert key_cf in vmap
    
    group = vmap[key_cf]
    assert group.main_path is not None
    assert norm_path(group.main_path) == norm_path(Path("test.JPG"))
    assert group.developed_path is not None
    assert group.developed_path.name.lower() == "test-developed.jpg"

def test_orphan_developed_preservation():
    """Verify find_images_with_variants keeps developed JPGs even if no main JPG exists."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        # Create an orphan developed file
        orphan = tdp / "orphan-developed.jpg"
        orphan.write_text("dummy")
        
        # Also create a regular pair for comparison
        pair_main = tdp / "pair.jpg"
        pair_dev = tdp / "pair-developed.jpg"
        pair_main.write_text("dummy")
        pair_dev.write_text("dummy")
        
        visible, vmap = find_images_with_variants(tdp)
        
        visible_names = [img.path.name for img in visible]
        assert "orphan-developed.jpg" in visible_names
        assert "pair.jpg" in visible_names
        assert "pair-developed.jpg" not in visible_names

def test_cache_evict_lock_not_held():
    """Verify that on_evict is called OUTSIDE the cache lock."""
    lock_held_during_callback = False
    
    def on_evict(k, v):
        nonlocal lock_held_during_callback
        import threading
        def check_lock():
            nonlocal lock_held_during_callback
            if not cache._lock.acquire(blocking=False):
                lock_held_during_callback = True
            else:
                cache._lock.release()
        
        t = threading.Thread(target=check_lock)
        t.start()
        t.join()

    # Small cache to trigger eviction on second set
    cache = ByteLRUCache(max_bytes=100, on_evict=on_evict)
    img = DecodedImage(buffer=np.zeros(60, dtype=np.uint8), width=60, height=1, bytes_per_line=60, format=1)
    
    cache["k1"] = img
    cache["k2"] = img
    
    assert not lock_held_during_callback, "Lock was held during eviction callback!"
