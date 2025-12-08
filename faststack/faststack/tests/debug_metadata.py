
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from PIL import ExifTags
import json

# Add parent directory to path to import faststack
sys.path.append(str(Path(__file__).parent.parent))

from faststack.imaging.metadata import get_exif_data

def debug_test():
    with open("debug_output.txt", "w") as f:
        f.write("Starting debug test...\n")
        try:
            # Patch PIL.Image.open directly
            with patch('PIL.Image.open') as mock_open, \
                 patch('pathlib.Path.exists', return_value=True):
                # Setup mock image and exif data
                mock_img = MagicMock()
                
                tag_map = {v: k for k, v in ExifTags.TAGS.items()}
                
                exif_dict = {
                    tag_map["DateTimeOriginal"]: "2023:01:01 12:00:00",
                    tag_map["Make"]: "Canon",
                    tag_map["Model"]: "Canon EOS R5",
                    tag_map["LensModel"]: "RF 24-70mm F2.8L IS USM",
                    tag_map["ISOSpeedRatings"]: 100,
                    tag_map["FNumber"]: (28, 10),
                    tag_map["ExposureTime"]: (1, 200),
                    tag_map["FocalLength"]: (50, 1),
                }
                
                mock_img._getexif.return_value = exif_dict
                mock_open.return_value = mock_img
                
                f.write("Calling get_exif_data...\n")
                result = get_exif_data(Path("dummy.jpg"))
                f.write(f"Result Summary: {json.dumps(result.get('summary', {}), indent=2)}\n")
                f.write(f"Result Full Keys: {list(result.get('full', {}).keys())}\n")
                
                summary = result["summary"]
                assert summary["Date Taken"] == "2023:01:01 12:00:00"
                assert summary["Camera"] == "Canon EOS R5"
                assert summary["Lens"] == "RF 24-70mm F2.8L IS USM"
                assert summary["ISO"] == "100"
                assert summary["Aperture"] == "f/2.8"
                assert summary["Shutter Speed"] == "1/200s"
                assert summary["Focal Length"] == "50mm"
                
                f.write("Test PASSED\n")
        except Exception as e:
            f.write("Test FAILED\n")
            import traceback
            traceback.print_exc(file=f)

if __name__ == "__main__":
    debug_test()
