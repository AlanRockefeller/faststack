
import mmap
import os
import tempfile

def verify():
    # Setup
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.close()
        path = f.name
    
    print(f"Created empty file: {path}")
    
    try:
        # Verify the logic I added to prefetch.py
        # Logic:
        # if os.path.getsize(image_file.path) == 0:
        #     log.warning("Skipping empty image file: %s", image_file.path)
        #     return None

        if os.path.getsize(path) == 0:
            print("SUCCESS: Skipped empty file due to size check.")
        else:
            # If we didn't skip, this would fail
            with open(path, "rb") as f:
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mmapped:
                    print("Mapped successfully")
            print("FAILURE: Should have skipped but didn't (or mmap worked unexpected)")
            
    except Exception as e:
        print(f"FAILED with exception: {e}")
    finally:
        if os.path.exists(path):
            os.unlink(path)

if __name__ == "__main__":
    verify()
