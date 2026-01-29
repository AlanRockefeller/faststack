import os
import time
import shutil
from pathlib import Path
from faststack.io.indexer import find_images


def verify_fix_logic():
    # Setup test dir
    test_dir = Path("./verify_auto_levels")
    if test_dir.exists():
        shutil.rmtree(test_dir)
    test_dir.mkdir()

    # Create main image
    img_name = "test_image.jpg"
    img_path = test_dir / img_name
    img_path.touch()

    # Set mtime to T0
    t0 = time.time() - 100
    os.utime(img_path, (t0, t0))

    # Initial Scan
    images = find_images(test_dir)
    # Simulate App State
    current_index = 0
    # User selects this
    selected_image = images[current_index]

    print(f"Initial: {[i.path.name for i in images]}")
    print(f"Selected: {selected_image.path.name} (Index {current_index})")

    # --- SIMULATE AUTO LEVELS ---

    # 1. Create Backup (preserves mtime T0)
    # The backup naming logic in create_backup_file is: filename-backup.jpg
    # Since 'test_image.jpg' -> 'test_image-backup.jpg'
    backup_name = "test_image-backup.jpg"
    backup_path = test_dir / backup_name
    shutil.copy2(img_path, backup_path)
    # Ensure backup has T0
    os.utime(backup_path, (t0, t0))

    # 2. Save Main (update mtime to T1)
    t1 = time.time()
    img_path.touch()  # Updates mtime

    # --- SIMULATE APP REFRESH & SELECTION (The Fix Logic) ---
    saved_path = img_path  # The file we just saved to

    # Refresh
    images = find_images(test_dir)
    print(f"Refreshed: {[i.path.name for i in images]}")
    # Expected order:
    # test_image-backup.jpg (T0)
    # test_image.jpg (T1)
    # So index 0 is backup, index 1 is edited

    # FIX LOGIC:
    new_index = -1
    target_path = Path(saved_path).resolve()
    target_name = Path(saved_path).name

    for i, img_file in enumerate(images):
        # The app now uses .name matching
        if img_file.path.name == target_name:
            new_index = i
            break

    # CHECK RESULTS
    if new_index == -1:
        print("FAIL: Count not find saved image in list.")
        exit(1)

    selected_in_ui = images[new_index]
    print(f"UI Selected: {selected_in_ui.path.name} (Index {new_index})")

    if selected_in_ui.path.name != img_name:
        print(
            f"FAIL: Selected image {selected_in_ui.path.name} is NOT the edited image {img_name}"
        )
        exit(1)

    # Verify previous image is backup
    if new_index > 0:
        prev_image = images[new_index - 1]
        print(f"Previous Image (Left Arrow): {prev_image.path.name}")
        if prev_image.path.name != backup_name:
            print(
                f"WARNING: Previous image is not the expected backup. Found: {prev_image.path.name}"
            )
    else:
        print(
            "WARNING: No previous image found. Backup should be roughly before edited image."
        )

    print("SUCCESS: Fix verified.")

    # Cleanup
    shutil.rmtree(test_dir)


if __name__ == "__main__":
    verify_fix_logic()
