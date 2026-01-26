
import sys
from unittest.mock import MagicMock, patch
import os
import tempfile

print("START_TEST")
try:
    from faststack.imaging.editor import ImageEditor
    editor = ImageEditor()
    
    # Test 1: Missing file raises FileNotFoundError
    print("Test 1: Missing file...")
    try:
        editor.load_image("non_existent_file.jpg")
        print("FAIL 1: No exception raised for missing file")
    except FileNotFoundError:
        print("PASS 1: Caught FileNotFoundError")
    except Exception as e:
        print(f"FAIL 1: Unexpected exception: {type(e)} {e}")

    # Test 2: Existing file but load fails (OSError)
    print("Test 2: Bad file load...")
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_name = tmp.name
    
    try:
        with patch('PIL.Image.open', side_effect=OSError("FAIL_PIL")):
             with patch.dict(sys.modules, {'cv2': MagicMock()}):
                 sys.modules['cv2'].imread.return_value = None
                 try:
                     editor.load_image(tmp_name)
                     print("FAIL 2: No exception raised for bad load")
                 except OSError as e:
                     if "FAIL_PIL" in str(e):
                         print("PASS 2: Caught expected OSError")
                     else:
                         print(f"FAIL 2: Wrong error: {e}")
                 except Exception as e:
                     print(f"FAIL 2: Unexpected exception: {type(e)} {e}")
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)

except Exception as e:
    print(f"CRASH: {e}")
print("END_TEST")
