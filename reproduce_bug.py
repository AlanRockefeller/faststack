
import os
import time
import shutil
from pathlib import Path
from faststack.io.indexer import find_images

def test_refresh_logic():
    # Setup test dir
    test_dir = Path("./test_images_refresh")
    if test_dir.exists():
        shutil.rmtree(test_dir)
    test_dir.mkdir()

    # Create main image
    img_path = test_dir / "test.jpg"
    img_path.touch()
    
    # Set mtime to T0
    t0 = time.time() - 100
    os.utime(img_path, (t0, t0))

    # Initial Scan
    images = find_images(test_dir)
    print(f"Initial images: {[i.path.name for i in images]}")
    
    current_index = 0
    original_path = images[current_index].path
    print(f"Current selection: {original_path.name} (Index {current_index})")

    # Simulate Auto-Levels Save
    # 1. Create Backup (preserves mtime T0)
    backup_path = test_dir / "test-backup.jpg"
    shutil.copy2(img_path, backup_path)
    
    # 2. Save Main (update mtime to T1)
    t1 = time.time()
    img_path.touch() # Updates mtime
    
    # Refresh
    images = find_images(test_dir)
    print(f"Refreshed images: {[i.path.name for i in images]}") 
    # Expect: [test-backup.jpg, test.jpg] due to T0 < T1
    
    # Selection Logic
    new_index = -1
    for i, img_file in enumerate(images):
        if img_file.path == original_path:
            new_index = i
            break
            
    print(f"Old Index: {current_index}")
    print(f"New Index found: {new_index}")
    
    if new_index == -1:
        print("FAIL: Did not find original path in refreshed list.")
        # If we failed to find, current_index stays 0
        # Index 0 is now 'test-backup.jpg'
        print(f"Effective selection would remain index {current_index}: {images[current_index].path.name}")
    else:
        print(f"Selected: {images[new_index].path.name} (Index {new_index})")

    # Cleanup
    shutil.rmtree(test_dir)

if __name__ == "__main__":
    test_refresh_logic()
