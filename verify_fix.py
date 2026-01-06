
import os
import sys
import logging
from pathlib import Path
import tempfile

# Add project root to path
sys.path.insert(0, os.getcwd())

# Mock Qt if needed, but prefetch.py handles it.
# However, faststack.models might import Qt?
# Let's check imports if it fails.

try:
    from faststack.models import ImageFile
    from faststack.imaging.prefetch import Prefetcher
except ImportError as e:
    print(f"ImportError: {e}")
    # Maybe need dependencies installed?
    # Assuming environment is set up.
    sys.exit(1)

# Verify the fix
def verify():
    # Setup
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.close()
        path = f.name
    
    print(f"Created empty file: {path}")
    
    try:
        # Create dummy ImageFile
        img_file = ImageFile(path=Path(path), name="empty.jpg", size=0, modified=0)
        
        def mock_cache_put(key, val):
            pass
            
        def mock_get_info():
            return 100, 100, 1
            
        # Instantiate Prefetcher
        # It creates a thread pool, so we should shut it down.
        prefetcher = Prefetcher([], mock_cache_put, 1, mock_get_info, debug=True)
        
        try:
            # Call _decode_and_cache
            # It checks self.generation (initially 0) against passed generation
            print("Calling _decode_and_cache...")
            result = prefetcher._decode_and_cache(img_file, 0, 0, 100, 100, 1)
            
            if result is None:
                print("SUCCESS: Returned None for empty file (graceful failure).")
            else:
                print(f"FAILURE: Returned {result}")
        finally:
            prefetcher.shutdown()
            
    except Exception as e:
        print(f"FAILED with exception: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if os.path.exists(path):
            os.unlink(path)

if __name__ == "__main__":
    # Configure logging to see the warning
    logging.basicConfig(level=logging.INFO)
    verify()
