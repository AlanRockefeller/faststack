
from PIL import Image, ExifTags
import io

def test_exif_sanitization():
    # 1. Create a dummy image with EXIF orientation = 6 (Rotated 90 CW)
    # We can't easily "create" raw EXIF bytes without saving, 
    # so we'll save a temp, change it, then load it.
    
    img = Image.new('RGB', (100, 100), color='red')
    exif = img.getexif()
    exif[ExifTags.Base.Orientation] = 6 # Simulate rotated
    
    buf = io.BytesIO()
    img.save(buf, format='JPEG', exif=exif.tobytes())
    buf.seek(0)
    
    # 2. Load it back (this simulates self.original_image)
    original_image = Image.open(buf)
    print(f"Original Orientation: {original_image.getexif().get(ExifTags.Base.Orientation)}")
    
    # 3. Simulate processing (we have a new image to save, but want metadata from original)
    # In Editor code: existing logic takes original_image.info.get('exif')
    # Proposed logic: take original_image.getexif(), mod it, tobytes()
    
    new_img = Image.new('RGB', (100, 100), color='blue') # The "edited" image
    
    # Proposed Fix Logic:
    exif_obj = original_image.getexif()
    if exif_obj:
        exif_obj[ExifTags.Base.Orientation] = 1
        try:
            exif_bytes = exif_obj.tobytes()
            print("Successfully serialized modified EXIF.")
        except Exception as e:
            print(f"Failed to serialize: {e}")
            exif_bytes = original_image.info.get('exif') # Fallback?
    else:
        exif_bytes = original_image.info.get('exif')

    # Save
    out_buf = io.BytesIO()
    new_img.save(out_buf, format='JPEG', exif=exif_bytes)
    out_buf.seek(0)
    
    # 4. Verify result
    result_img = Image.open(out_buf)
    res_orientation = result_img.getexif().get(ExifTags.Base.Orientation)
    print(f"Result Orientation: {res_orientation}")
    
    if res_orientation == 1:
        print("PASS: Orientation sanitized.")
    else:
        print("FAIL: Orientation NOT sanitized.")

if __name__ == "__main__":
    test_exif_sanitization()
