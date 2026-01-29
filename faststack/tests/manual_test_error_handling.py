import sys
from unittest.mock import MagicMock, patch
import numpy as np
from pathlib import Path
import logging

# Configure logging to swallow output
logging.basicConfig(level=logging.CRITICAL)


def test_load_image_raises():
    print("Running test_load_image_raises...")
    try:
        from faststack.imaging.editor import ImageEditor

        editor = ImageEditor()

        # Patch Image.open to raise an exception
        with patch("PIL.Image.open", side_effect=OSError("Mocked file error")):
            with patch.dict(sys.modules, {"cv2": MagicMock()}):
                sys.modules["cv2"].imread.return_value = None

                try:
                    editor.load_image("non_existent_file.jpg")
                    print("FAILURE: load_image did NOT raise exception")
                    return False
                except OSError as e:
                    if "Mocked file error" in str(e):
                        print("SUCCESS: load_image raised expected exception")
                        return True
                    else:
                        print(f"FAILURE: load_image raised wrong exception: {e}")
                        return False
                except Exception as e:
                    print(
                        f"FAILURE: load_image raised unexpected exception type: {type(e)} {e}"
                    )
                    return False
    except ImportError as e:
        print(f"ImportError in test setup: {e}")
        return False
    except Exception as e:
        print(f"Unexpected error in test setup: {e}")
        return False


def test_save_image_raises():
    print("Running test_save_image_raises...")
    try:
        from faststack.imaging.editor import ImageEditor

        editor = ImageEditor()
        editor.float_image = np.zeros((10, 10, 3), dtype=np.float32)
        editor.current_filepath = Path("fake_path.jpg")

        with patch(
            "faststack.imaging.editor.create_backup_file",
            return_value=Path("backup.jpg"),
        ):
            mock_img = MagicMock()
            # fail ANY save call
            mock_img.save.side_effect = PermissionError("Mocked save error")

            with patch("PIL.Image.fromarray", return_value=mock_img):
                try:
                    editor.save_image()
                    print("FAILURE: save_image did NOT raise exception")
                    return False
                except PermissionError as e:
                    if "Mocked save error" in str(e):
                        print("SUCCESS: save_image raised expected exception")
                        return True
                    else:
                        print(f"FAILURE: save_image raised wrong exception: {e}")
                        return False
                except Exception as e:
                    print(
                        f"FAILURE: save_image raised unexpected exception type: {type(e)} {e}"
                    )
                    return False
    except Exception as e:
        print(f"Unexpected error in test setup: {e}")
        return False


if __name__ == "__main__":
    # Ensure parent path in sys.path
    root_dir = Path(__file__).parent.parent.parent
    if str(root_dir) not in sys.path:
        sys.path.insert(0, str(root_dir))

    success = True
    if not test_load_image_raises():
        success = False
    print("-" * 20)
    if not test_save_image_raises():
        success = False

    if not success:
        sys.exit(1)
    print("ALL TESTS PASSED")
