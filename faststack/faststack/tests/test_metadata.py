
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
from faststack.imaging.metadata import get_exif_data, clean_exif_value
from PIL import ExifTags

class TestMetadata(unittest.TestCase):
    @patch('pathlib.Path.exists', return_value=True)
    @patch('faststack.imaging.metadata.Image.open')
    def test_get_exif_data_success(self, mock_open, mock_exists):
        try:
            # Setup mock image and exif data
            mock_img = MagicMock()
            
            # Create a reverse mapping for tags to IDs for easier setup
            tag_map = {v: k for k, v in ExifTags.TAGS.items()}
            
            exif_dict = {
                tag_map["DateTimeOriginal"]: "2023:01:01 12:00:00",
                tag_map["Make"]: "Canon\x00", # Null terminated
                tag_map["Model"]: "Canon EOS R5",
                tag_map["LensModel"]: "RF 24-70mm F2.8L IS USM",
                tag_map["ISOSpeedRatings"]: 100,
                tag_map["FNumber"]: (28, 10), # f/2.8
                tag_map["ExposureTime"]: (1, 200), # 1/200s
                tag_map["FocalLength"]: (50, 1), # 50mm
                tag_map["MakerNote"]: b'Some binary data \x00\x01\x02', # Binary data
                tag_map["UserComment"]: b'ASCII comment\x00', # ASCII bytes
                tag_map["Flash"]: 1, # Fired
                tag_map["GPSInfo"]: {
                    1: 'N',
                    2: (34.0, 0.0, 0.0), # 34 deg N
                    3: 'W',
                    4: (118.0, 15.0, 0.0) # 118 deg 15 min W
                }
            }
            
            mock_img._getexif.return_value = exif_dict
            mock_open.return_value = mock_img
            
            # Test
            result = get_exif_data(Path("dummy.jpg"))
            
            # Verify summary
            summary = result["summary"]
            self.assertEqual(summary["Date Taken"], "2023:01:01 12:00:00")
            self.assertEqual(summary["Camera"], "Canon EOS R5") # Make should be collapsed into Model
            self.assertEqual(summary["Lens"], "RF 24-70mm F2.8L IS USM")
            self.assertEqual(summary["ISO"], "100")
            self.assertEqual(summary["Aperture"], "f/2.8")
            self.assertEqual(summary["Shutter Speed"], "1/200s")
            self.assertEqual(summary["Focal Length"], "50mm")
            self.assertEqual(summary["Flash"], "1")
            # 34 + 0/60 + 0/3600 = 34.00000
            # 118 + 15/60 + 0/3600 = 118.25000 -> -118.25000 (W)
            self.assertEqual(summary["GPS"], "34.00000, -118.25000")
            
            # Verify full data contains keys and handles binary
            full = result["full"]
            self.assertIn("DateTimeOriginal", full)
            self.assertEqual(full["Model"], "Canon EOS R5")
            self.assertTrue(full["MakerNote"].startswith("<binary data:"))
            self.assertEqual(full["UserComment"], "ASCII comment") # Should be decoded
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            raise e

    def test_clean_exif_value(self):
        # Test string cleaning
        self.assertEqual(clean_exif_value("Hello\x00"), "Hello")
        self.assertEqual(clean_exif_value("  Spaces  "), "Spaces")
        
        # Test bytes decoding
        self.assertEqual(clean_exif_value(b"Hello"), "Hello")
        self.assertEqual(clean_exif_value(b"Hello\x00"), "Hello")
        
        # Test binary fallback
        binary = b'\x00\x01\xff\xfe'
        self.assertTrue(clean_exif_value(binary).startswith("<binary data:"))
        
        # Test numbers
        self.assertEqual(clean_exif_value(123), "123")
        self.assertEqual(clean_exif_value(12.34), "12.34")
        
        # Test lists
        self.assertEqual(clean_exif_value([1, 2]), "['1', '2']")

    @patch('faststack.imaging.metadata.Image.open')
    def test_get_exif_data_no_exif(self, mock_open):
        mock_img = MagicMock()
        mock_img._getexif.return_value = None
        mock_open.return_value = mock_img
        
        result = get_exif_data(Path("dummy.jpg"))
        self.assertEqual(result["summary"], {})
        self.assertEqual(result["full"], {})

    def test_get_exif_data_real_file_not_found(self):
        # Test with a non-existent file
        result = get_exif_data(Path("non_existent_file.jpg"))
        self.assertEqual(result["summary"], {})
        self.assertEqual(result["full"], {})

if __name__ == '__main__':
    unittest.main()
