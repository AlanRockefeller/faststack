
import sys
from unittest.mock import MagicMock

# Hardcode path to project root
sys.path.insert(0, r"c:\code\faststack")
sys.modules['cv2'] = MagicMock()

import unittest
from unittest.mock import patch
from PIL import Image

from faststack.imaging.editor import ImageEditor

class TestExifReproduction(unittest.TestCase):
    def setUp(self):
        self.editor = ImageEditor()
        # Create a dummy image for testing
        self.editor.original_image = Image.new('RGB', (10, 10))
        self.editor._source_exif_bytes = b"original source exif"

    def test_tobytes_failure_drops_exif(self):
        """Verify that a failure in tobytes() currently drops EXIF data."""
        mock_exif = MagicMock()
        mock_exif.tobytes.side_effect = Exception("failed to serialize")
        
        # Patch Image.Exif to return our mock
        with patch('PIL.Image.Exif', return_value=mock_exif):
            res = self.editor._get_sanitized_exif_bytes()
            
            # DESIRED BEHAVIOR: It returns the original bytes if sanitization fails
            self.assertEqual(res, b"original source exif")
            
    def test_missing_tobytes_drops_exif(self):
        """Verify that missing tobytes() currently drops EXIF data."""
        mock_exif = MagicMock(spec=[]) # No tobytes
        
        with patch('PIL.Image.Exif', return_value=mock_exif):
            res = self.editor._get_sanitized_exif_bytes()
            # DESIRED BEHAVIOR: It returns the original bytes 
            self.assertEqual(res, b"original source exif")

if __name__ == '__main__':
    suite = unittest.TestLoader().loadTestsFromTestCase(TestExifReproduction)
    result = unittest.TestResult()
    suite.run(result)
    
    if result.wasSuccessful():
        print("Success!")
    else:
        print(f"FAILED with {len(result.failures)} failures and {len(result.errors)} errors")
        for f in result.failures:
            print("FAILURE in", f[0])
            print(f[1])
        for e in result.errors:
            print("ERROR in", e[0])
            print(e[1])
        sys.exit(1)
