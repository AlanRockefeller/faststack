
import logging
from pathlib import Path
from typing import Dict, Any, Union
from PIL import Image, ExifTags

log = logging.getLogger(__name__)

def clean_exif_value(value: Any) -> str:
    """
    Cleans EXIF values for display.
    - Decodes bytes if possible, otherwise returns a placeholder.
    - Strips null bytes and unprintable characters from strings.
    - Formats tuples/lists recursively.
    """
    if isinstance(value, bytes):
        try:
            # Try to decode as UTF-8, stripping nulls
            decoded = value.decode('utf-8').strip('\x00')
            # Check if the result is printable
            if decoded.isprintable():
                return decoded
            return f"<binary data: {len(value)} bytes>"
        except UnicodeDecodeError:
            return f"<binary data: {len(value)} bytes>"
    
    if isinstance(value, str):
        # Strip null bytes and other common garbage
        cleaned = value.strip('\x00').strip()
        # Remove other non-printable characters if necessary, but keep basic text
        # For now, just stripping nulls is the most important
        return cleaned
        
    if isinstance(value, (list, tuple)):
        return str([clean_exif_value(v) for v in value])
        
    return str(value)

def get_exif_data(path: Union[str, Path]) -> Dict[str, Any]:
    """
    Extracts EXIF data from an image file.
    
    Returns a dictionary with two keys:
    - 'summary': A dictionary of formatted common fields (Date, ISO, Aperture, etc.)
    - 'full': A dictionary of all decoded EXIF tags.
    """
    path = Path(path)
    if not path.exists():
        return {"summary": {}, "full": {}}

    try:
        with Image.open(path) as img:
            exif = img._getexif()
        if not exif:
            return {"summary": {}, "full": {}}
    except Exception as e:  # noqa: BLE001 - defensive catch for arbitrary EXIF parsing issues
        log.warning(f"Failed to extract EXIF from {path}: {e}")
        return {"summary": {}, "full": {}}

    decoded_exif = {}
    for tag_id, value in exif.items():
        tag_name = ExifTags.TAGS.get(tag_id, tag_id)
        decoded_exif[tag_name] = value

    summary = {}
    
    # Helper to safely get value
    def get_val(key):
        return decoded_exif.get(key)

    # Date Taken
    date_taken = get_val("DateTimeOriginal") or get_val("DateTime")
    if date_taken:
        summary["Date Taken"] = clean_exif_value(date_taken)

    # Camera Model
    make = get_val("Make")
    model = get_val("Model")
    
    # Clean make and model first
    if make: make = clean_exif_value(make)
    if model: model = clean_exif_value(model)
    
    if make and model:
        if make.lower() in model.lower():
             summary["Camera"] = model
        else:
            summary["Camera"] = f"{make} {model}"
    elif model:
        summary["Camera"] = model
    elif make:
        summary["Camera"] = make

    # Lens
    lens = get_val("LensModel") or get_val("LensInfo")
    if lens:
        summary["Lens"] = clean_exif_value(lens)

    # ISO
    iso = get_val("ISOSpeedRatings")
    if iso:
        summary["ISO"] = clean_exif_value(iso)

    # Aperture (FNumber)
    f_number = get_val("FNumber")
    if f_number:
        try:
            # FNumber is often a tuple (numerator, denominator) or a float
            if isinstance(f_number, tuple) and len(f_number) == 2:
                 val = f_number[0] / f_number[1]
            else:
                val = float(f_number)
            summary["Aperture"] = f"f/{val:.1f}"
        except Exception:
            summary["Aperture"] = clean_exif_value(f_number)

    # Shutter Speed (ExposureTime)
    exposure_time = get_val("ExposureTime")
    if exposure_time:
        try:
            if isinstance(exposure_time, tuple) and len(exposure_time) == 2:
                val = exposure_time[0] / exposure_time[1]
            else:
                val = float(exposure_time)
            
            if val < 1:
                summary["Shutter Speed"] = f"1/{int(1/val)}s"
            else:
                summary["Shutter Speed"] = f"{val}s"
        except Exception:
            summary["Shutter Speed"] = clean_exif_value(exposure_time)
            
    # Focal Length
    focal_length = get_val("FocalLength")
    if focal_length:
        try:
             if isinstance(focal_length, tuple) and len(focal_length) == 2:
                 val = focal_length[0] / focal_length[1]
             else:
                 val = float(focal_length)
             summary["Focal Length"] = f"{int(val)}mm"
        except Exception:
             summary["Focal Length"] = clean_exif_value(focal_length)

    # Flash
    flash = get_val("Flash")
    if flash is not None:
        # Flash is a bitmask, but for now just showing the value or a simple string is a good start.
        # Common values: 0 (No Flash), 1 (Fired), 16 (No Flash, Auto), 24 (No Flash, Auto), 25 (Fired, Auto)
        # We can just clean it for now.
        summary["Flash"] = clean_exif_value(flash)

    # GPS
    gps_info = get_val("GPSInfo")
    if gps_info:
        try:
            def convert_to_degrees(value):
                d = float(value[0])
                m = float(value[1])
                s = float(value[2])
                return d + (m / 60.0) + (s / 3600.0)

            lat = None
            lon = None
            
            # GPSInfo keys are integers. 
            # 1: GPSLatitudeRef, 2: GPSLatitude
            # 3: GPSLongitudeRef, 4: GPSLongitude
            
            if 2 in gps_info and 4 in gps_info:
                lat = convert_to_degrees(gps_info[2])
                lon = convert_to_degrees(gps_info[4])
                
                if 1 in gps_info and gps_info[1] == 'S':
                    lat = -lat
                if 3 in gps_info and gps_info[3] == 'W':
                    lon = -lon
                    
                summary["GPS"] = f"{lat:.5f}, {lon:.5f}"
        except Exception as e:
            log.warning(f"Failed to parse GPS info: {e}")
            # Fallback to cleaning the raw info if parsing fails
            # But user specifically asked for decimal, so maybe just don't show if it fails or show raw?
            # Let's show raw if parsing fails but cleaned
            # summary["GPS"] = clean_exif_value(gps_info)
            pass

    # Convert all values in full dict to string to ensure JSON serializability for QML
    # Apply cleaning to all values
    full_str = {str(k): clean_exif_value(v) for k, v in decoded_exif.items()}

    return {
        "summary": summary,
        "full": full_str
    }
