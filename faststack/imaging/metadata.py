import logging
import math
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Optional, Union
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
            decoded = value.decode("utf-8").strip("\x00")
            # Check if the result is printable
            if decoded.isprintable():
                return decoded
            return f"<binary data: {len(value)} bytes>"
        except UnicodeDecodeError:
            return f"<binary data: {len(value)} bytes>"

    if isinstance(value, str):
        # Strip null bytes and other common garbage
        cleaned = value.strip("\x00").strip()
        # Remove other non-printable characters if necessary, but keep basic text
        # For now, just stripping nulls is the most important
        return cleaned

    if isinstance(value, (list, tuple)):
        return str([clean_exif_value(v) for v in value])

    return str(value)


# Camera-style 1/3-stop shutter speed labels (Nikon/Canon convention)
_SHUTTER_TABLE = [
    (30.0, "30s"), (25.0, "25s"), (20.0, "20s"), (15.0, "15s"), (13.0, "13s"),
    (10.0, "10s"), (8.0, "8s"), (6.0, "6s"), (5.0, "5s"), (4.0, "4s"),
    (3.2, "3.2s"), (2.5, "2.5s"), (2.0, "2s"), (1.6, "1.6s"), (1.3, "1.3s"),
    (1.0, "1s"), (0.8, "0.8s"), (0.6, "0.6s"), (0.5, "0.5s"), (0.4, "0.4s"), (0.3, "0.3s"),
    (1/4, "1/4s"), (1/5, "1/5s"), (1/6, "1/6s"), (1/8, "1/8s"),
    (1/10, "1/10s"), (1/13, "1/13s"), (1/15, "1/15s"),
    (1/20, "1/20s"), (1/25, "1/25s"), (1/30, "1/30s"),
    (1/40, "1/40s"), (1/50, "1/50s"), (1/60, "1/60s"),
    (1/80, "1/80s"), (1/100, "1/100s"), (1/125, "1/125s"),
    (1/160, "1/160s"), (1/200, "1/200s"), (1/250, "1/250s"),
    (1/320, "1/320s"), (1/400, "1/400s"), (1/500, "1/500s"),
    (1/640, "1/640s"), (1/800, "1/800s"), (1/1000, "1/1000s"),
    (1/1250, "1/1250s"), (1/1600, "1/1600s"), (1/2000, "1/2000s"),
    (1/2500, "1/2500s"), (1/3200, "1/3200s"), (1/4000, "1/4000s"),
    (1/5000, "1/5000s"), (1/6400, "1/6400s"), (1/8000, "1/8000s"),
]
_SHUTTER_SECONDS = [t for (t, _) in _SHUTTER_TABLE]
_SHUTTER_LOG_SECONDS = [math.log(t) for t in _SHUTTER_SECONDS]


def _exif_rational_to_seconds(x: Any) -> Optional[float]:
    """Convert various EXIF rational-ish representations to seconds."""
    if x is None:
        return None
    if hasattr(x, "numerator") and hasattr(x, "denominator"):
        try:
            n, d = int(x.numerator), int(x.denominator)
            if d != 0:
                return float(Fraction(n, d))
        except Exception as e:
            log.debug(
                "_exif_rational_to_seconds failed for rational object %r (%s): %s",
                x,
                type(x).__name__,
                e,
            )
    if isinstance(x, (tuple, list)) and len(x) == 2:
        try:
            n, d = int(x[0]), int(x[1])
            if d != 0:
                return float(Fraction(n, d))
        except Exception as e:
            log.debug(
                "_exif_rational_to_seconds failed for tuple/list %r (%s): %s",
                x,
                type(x).__name__,
                e,
            )
    try:
        return float(x)
    except Exception as e:
        if x is not None:
            log.debug(
                "_exif_rational_to_seconds failed for value %r (%s): %s",
                x,
                type(x).__name__,
                e,
            )
        return None


def format_shutter_speed_camera_style(exposure_value: Any) -> str:
    """Format ExposureTime like a camera UI (nearest standard 1/3-stop step)."""
    sec = _exif_rational_to_seconds(exposure_value)
    if sec is None or not math.isfinite(sec) or sec <= 0:
        return ""
    if sec >= _SHUTTER_SECONDS[0]:
        return _SHUTTER_TABLE[0][1]
    if sec <= _SHUTTER_SECONDS[-1]:
        return _SHUTTER_TABLE[-1][1]
    log_sec = math.log(sec)
    best_i = 0
    best_err = float("inf")
    for i, log_t in enumerate(_SHUTTER_LOG_SECONDS):
        err = abs(log_t - log_sec)
        if err < best_err:
            best_err = err
            best_i = i
    return _SHUTTER_TABLE[best_i][1]


def get_exif_brief(path: Union[str, Path]) -> str:
    """Return a compact EXIF summary for the status bar.

    Opens only the image header (Pillow lazy-loads), extracts ISO, aperture,
    shutter speed, and capture time.  Returns a pipe-separated string like
    ``"ISO 800 | f/2.8 | 1/500s | 14:30:25"`` or ``""`` if no EXIF is found.

    Supported formats: JPEG, TIFF, HEIF.
    """
    path = Path(path)
    if path.suffix.lower() not in {".jpg", ".jpeg", ".jpe", ".tif", ".tiff", ".heif", ".heic"}:
        return ""
    if not path.exists():
        return ""

    try:
        with Image.open(path) as img:
            exif = img.getexif()
            # getexif() nests EXIF sub-IFD tags; merge them for flat access
            # Read them while file is open to avoid "I/O on closed file"
            exif_ifd = dict(exif.get_ifd(ExifTags.IFD.Exif) if hasattr(ExifTags, "IFD") else {})
        
        if not exif:
            return ""
    except Exception:
        return ""

    tags = dict(exif)
    tags.update(exif_ifd)

    parts: list[str] = []

    # ISO (tag 0x8827 / ISOSpeedRatings)
    iso = tags.get(0x8827)
    if iso is not None:
        # Some cameras return a list/tuple for ISO
        if isinstance(iso, (list, tuple)) and len(iso) > 0:
            iso = iso[0]
        try:
            parts.append(f"ISO {int(iso)}")
        except (ValueError, TypeError):
            parts.append(f"ISO {iso}")

    # Aperture / FNumber (tag 0x829D)
    f_number = tags.get(0x829D)
    if f_number is not None:
        try:
            val = float(f_number)
            parts.append(f"f/{val:.1f}")
        except (ValueError, TypeError) as e:
            log.debug(f"Failed to convert f_number {f_number!r}: {e}", exc_info=True)

    # Shutter speed / ExposureTime (tag 0x829A)
    # Note: Pillow's ExifTags maps 0x829A to ExposureTime.
    exposure = tags.get(0x829A)
    if exposure is not None:
        try:
            s = format_shutter_speed_camera_style(exposure)
            if s:
                parts.append(s)
        except (ValueError, TypeError) as e:
            log.debug(f"Failed to convert exposure {exposure!r}: {e}", exc_info=True)

    # Capture time / DateTimeOriginal (tag 0x9003), fallback DateTime (tag 0x0132)
    dt = tags.get(0x9003) or tags.get(0x0132)
    if dt:
        try:
            cleaned = clean_exif_value(dt)
            # Format is "YYYY:MM:DD HH:MM:SS" — extract time portion
            if " " in cleaned:
                parts.append(cleaned.split(" ", 1)[1])
            else:
                parts.append(cleaned)
        except Exception as e:
            log.error(f"Failed to parse EXIF datetime {dt!r}: {e}", exc_info=True)

    return " | ".join(parts)


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
            exif_obj = img.getexif()
            if not exif_obj:
                return {"summary": {}, "full": {}}
            
            # Merge sub-IFD tags (ISO, Lens, etc.)
            exif_ifd = dict(exif_obj.get_ifd(ExifTags.IFD.Exif) if hasattr(ExifTags, "IFD") else {})

            # Fetch GPS sub-IFD while image is still open (Pillow ≥8.2
            # stores GPSInfo as an integer IFD offset, not a dict)
            gps_ifd = dict(exif_obj.get_ifd(0x8825)) if hasattr(ExifTags, "IFD") else {}

        # Normalize to a dict for consistency
        exif = dict(exif_obj)
        exif.update(exif_ifd)
        
    except Exception as e:
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
        try:
            summary["Date Taken"] = clean_exif_value(date_taken)
        except Exception as e:
            log.debug("failed parsing EXIF date %r: %s", date_taken, e)

    # Camera Model
    make = get_val("Make")
    model = get_val("Model")

    # Clean make and model first
    if make:
        make = clean_exif_value(make)
    if model:
        model = clean_exif_value(model)

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
        if isinstance(iso, (list, tuple)) and len(iso) > 0:
            iso = iso[0]
        try:
            summary["ISO"] = str(int(iso))
        except (ValueError, TypeError):
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
                summary["Shutter Speed"] = f"1/{int(1 / val)}s"
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

    # GPS — prefer the resolved sub-IFD dict; fall back to decoded tag only
    # if it is already a mapping (older Pillow versions).
    gps_raw = get_val("GPSInfo")
    gps_info = gps_ifd if gps_ifd else (gps_raw if isinstance(gps_raw, dict) else None)
    if gps_info:
        try:

            def convert_to_degrees(value):
                d = float(value[0])
                m = float(value[1])
                s = float(value[2])
                return d + (m / 60.0) + (s / 3600.0)

            # GPSInfo keys are integers.
            # 1: GPSLatitudeRef, 2: GPSLatitude
            # 3: GPSLongitudeRef, 4: GPSLongitude

            if 2 in gps_info and 4 in gps_info:
                lat = convert_to_degrees(gps_info[2])
                lon = convert_to_degrees(gps_info[4])

                if 1 in gps_info and gps_info[1] == "S":
                    lat = -lat
                if 3 in gps_info and gps_info[3] == "W":
                    lon = -lon

                summary["GPS"] = f"{lat:.5f}, {lon:.5f}"
        except Exception as e:
            log.warning(f"Failed to parse GPS info: {e}")
            pass

    # Convert all values in full dict to string to ensure JSON serializability for QML
    # Apply cleaning to all values
    full_str = {str(k): clean_exif_value(v) for k, v in decoded_exif.items()}

    return {"summary": summary, "full": full_str}
