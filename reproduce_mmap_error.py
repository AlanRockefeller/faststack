
import mmap
import os
import tempfile

def reproduce():
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.close()
        path = f.name

    print(f"Created empty file: {path}")
    try:
        with open(path, "rb") as f:
            # excessive logic to match the app code pattern
            # "with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mmapped:"
            try:
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mmapped:
                    print("Mapped successfully (unexpected for empty file)")
            except ValueError as e:
                print(f"Caught expected error: {e}")
                if "cannot mmap an empty file" in str(e):
                    print("VERIFIED: Reproduction successful.")
                else:
                    print("VERIFIED: Reproduction successful (different message).")
                    
    except Exception as e:
        print(f"Caught unexpected top level error: {e}")
    finally:
        os.unlink(path)

if __name__ == "__main__":
    reproduce()
