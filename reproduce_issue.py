import os
import pathlib
import sys

def reproduction_step():
    base_dir = pathlib.Path("test_deletion_repro")
    base_dir.mkdir(exist_ok=True)
    
    recycle_bin = base_dir / "recycle_bin"
    recycle_bin.mkdir(exist_ok=True)
    
    file_name = "test_image.jpg"
    source_file = base_dir / file_name
    dest_file = recycle_bin / file_name
    
    # Clean up previous run
    if source_file.exists(): source_file.unlink()
    if dest_file.exists(): dest_file.unlink()
    
    # 1. Simulate state: File exists in BOTH source and recycle bin
    source_file.touch()
    dest_file.touch()
    
    print(f"Created {source_file} and {dest_file}")
    
    # 2. Try rename (Current Code)
    try:
        print("Attempting rename (should fail on Windows)...")
        source_file.rename(dest_file)
        print("SUCCESS: Rename worked (unexpected on Windows if dest exists)")
    except FileExistsError:
        print("CAUGHT EXPECTED ERROR: FileExistsError during rename")
    except OSError as e:
        print(f"CAUGHT OTHER ERROR: {type(e).__name__}: {e}")
        
    # Reset for fix test
    if not source_file.exists(): source_file.touch()
    if not dest_file.exists(): dest_file.touch()
    
    # 3. Try replace (Proposed Fix)
    try:
        print("Attempting replace (should succeed)...")
        source_file.replace(dest_file)
        print("SUCCESS: Replace worked")
        if not source_file.exists() and dest_file.exists():
            print("Verified: Source is gone, dest exists.")
        else:
            print("Validation FAILED: File states not correct.")
    except Exception as e:
        print(f"FAILED: Replace raised {type(e).__name__}: {e}")

if __name__ == "__main__":
    reproduction_step()
