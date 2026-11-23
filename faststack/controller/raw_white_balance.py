"""Re-render the current JPG from its RAW with automatic white balance (W).

Flash shots often come out of the camera with a colour cast too strong for
the JPG-domain white balance tools to fix. The RAW still has the full colour
information, so this hands it to ``rawtherapee-cli`` with automatic white
balance, writes the result as an 8-bit JPG and puts it in place of the
camera JPG. The old JPG is kept as ``<name>-backup.jpg`` (like any other
FastStack edit), so Ctrl+Z puts it back.

How a run goes:

* Unsaved edits on the photo are saved first; W starts once that save is
  on disk, so they end up in the backup and are not applied twice.
* RawTherapee renders the RAW (worker thread). When that render clips
  highlights, a second render compresses them so detail comes back.
* The render is turned to show the photo the way the JPG did, trimmed to
  the camera JPG's frame when it is the whole frame, and swapped in on the
  controller's Qt thread with a backup, an undo entry and a sidecar update.
* Edits made while RawTherapee runs are not lost and not written onto the
  old pixels: a live editor session gets the new pixels under its edits,
  and a save of the photo (navigating away, Ctrl+S) waits ("parked") and
  is then rendered from the new pixels.

The status bar meter follows ``UIState.rawAwbProgress``. RawTherapee
reports nothing while it works, so the meter is an estimate from how long
earlier runs took; it only reaches 100% when the run is done.

Mixed into AppController (faststack/app.py); every method runs on the
controller instance.
"""

import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
from PIL import Image, ImageOps
from PySide6.QtCore import Qt, QTimer, Slot

from faststack.config import config
from faststack.imaging.editor import ImageEditor, _safe_replace, create_backup_file
from faststack.imaging.mask_engine import LayerDensityCache
from faststack.imaging.optional_deps import get_cv2
from faststack.io.child_process import child_environment, log_tool_output
from faststack.io.executable_validator import validate_executable_path
from faststack.io.indexer import JPG_EXTENSIONS

log = logging.getLogger(__name__)

# Undo action type recorded for a RAW white balance re-render.
RAW_WHITE_BALANCE_ACTION = "raw_white_balance"

# RawTherapee's [White Balance] Setting. "Auto" is the value every version
# understands: up to 5.8 it is the automatic method, and 5.9+ maps it to its
# default automatic method (temperature correlation). "autold" (RGB grey) and
# "autitcgreen" (temperature correlation) pick one explicitly on 5.9+.
DEFAULT_RAW_AWB_METHOD = "Auto"
DEFAULT_RAW_AWB_JPEG_QUALITY = 95
_RAW_AWB_TIMEOUT_SECONDS = 180

# Highlights. The first render is measured: when at least this share of its
# pixels has a channel at _CLIP_LEVEL or above, it is rendered again with
# RawTherapee's highlight compression. The camera JPG's own clipping says
# little about the render's (1.9% -> 0.2%, 11.5% -> 20.3% on real photos),
# so the render itself is what decides.
_CLIP_LEVEL = 250
_CLIP_DECODE_BOX = (512, 512)
_HIGHLIGHT_TRIGGER = 0.005
_HIGHLIGHT_HEAVY = 0.05
_HIGHLIGHT_COMPRESSION = 250
_HIGHLIGHT_COMPRESSION_HEAVY = 400

# Trimming RawTherapee's slightly larger frame to the camera JPG's: the
# camera's frame sits a few pixels off centre, so the offset is measured.
_ALIGN_REGIONS = (
    (0.35, 0.65, 0.35, 0.65),
    (0.05, 0.30, 0.05, 0.30),
    (0.05, 0.30, 0.70, 0.95),
    (0.70, 0.95, 0.05, 0.30),
    (0.70, 0.95, 0.70, 0.95),
)
_ALIGN_MAX_SHIFT_PX = 32
_ALIGN_MIN_RESPONSE = 0.15

# Progress meter: share of the bar each stage fills, and stage estimates.
_DEFAULT_PASS_SECONDS = 10.0
_SAVE_SECONDS = 3.0
_FINISH_SECONDS = 2.5
_STAGE_SPANS = {
    "save": (0.0, 0.08),
    "develop": (0.08, 0.62),
    "develop_only": (0.08, 0.88),
    "highlights": (0.62, 0.88),
    "finish": (0.88, 0.97),
}
_METER_DONE_MS = 1800
_METER_FAILED_MS = 4500
# How long W waits for the save of unsaved edits before giving up.
_SAVE_WAIT_SECONDS = 90.0

# Below this aspect ratio an image counts as square: no portrait/landscape.
_SQUARE_ASPECT = 1.05
# Rotation matching compares small greyscale copies of the two images.
_MATCH_DECODE_BOX = (256, 256)
_MATCH_COMPARE_SIZE = (48, 48)
# A correlation at least this high is a confident match.
_MATCH_MIN_CORRELATION = 0.5
# A JPG cropped in FastStack is matched as a crop: found inside each turn of
# RawTherapee's full frame. Crops keep the camera's pixel scale, and
# RawTherapee's frame is only a little bigger or smaller than the camera's.
_CROP_MATCH_BOX = (384, 384)
_CROP_MATCH_SCALES = (0.94, 0.97, 1.0, 1.03, 1.06)
_CROP_MATCH_BLUR = 1.5  # Gaussian sigma, in small-copy pixels
# Long-to-short side ratios this close count as the same shape.
_SAME_ASPECT_TOLERANCE = 0.03
# Within this (per side) the JPG is taken to be the whole frame.
_FULL_FRAME_TOLERANCE = 0.06
_CROP_MIN_TEMPLATE_PX = 16
# The best turn must score at least this, and this much above the next one.
_CROP_MIN_CORRELATION = 0.6
_CROP_MIN_MARGIN = 0.1


@dataclass
class RawWhiteBalanceOperation:
    """One RawTherapee auto white balance run for one JPG."""

    operation_id: str
    jpg_key: str
    jpg_path: Path
    raw_path: Path
    jpg_fingerprint: Optional[tuple]
    directory_key: Optional[str]
    sidecar: Any
    process: Optional[subprocess.Popen] = None
    worker: Optional[threading.Thread] = None
    cancel_requested: bool = False
    staged_path: Optional[Path] = None
    # The JPG's saved edit state is hidden from the editor (in memory only)
    # while W runs; see _raw_awb_zero_baseline.
    recipe_hidden: bool = False
    # Newest save of this JPG made while W runs: {"request", "saving_status"}.
    parked: Optional[dict] = None


@dataclass
class RawWhiteBalanceResult:
    operation_id: str
    jpg_key: str
    success: bool
    error: Optional[str] = None
    staged_path: Optional[Path] = None
    detail: str = ""


@dataclass
class _WaitingForSave:
    """A W press waiting for the save of the photo's unsaved edits."""

    operation_id: str
    jpg_path: Path
    raw_path: Path
    rt_exe: str
    deadline: float
    sidecar: Any = None
    directory_key: Optional[str] = None
    extra: dict = field(default_factory=dict)


def build_raw_awb_profile(method: str, highlight_compression: int = 0) -> str:
    """Partial RawTherapee profile: auto white balance, optionally with
    highlight compression.

    Applied with ``-p`` on top of ``-d`` (the user's default RAW profile), so
    every other setting keeps what RawTherapee would normally use. The
    default profiles already reconstruct clipped channels (HLRecovery).
    """
    profile = f"[White Balance]\nEnabled=true\nSetting={method}\n"
    if highlight_compression > 0:
        profile += (
            "\n[Exposure]\n"
            f"HighlightCompr={int(highlight_compression)}\n"
            "HighlightComprThreshold=0\n"
        )
    return profile


def build_raw_awb_command(
    rt_exe: str,
    raw_path: Path,
    output_path: Path,
    profile_path: Path,
    *,
    quality: int,
) -> list[str]:
    """argv for rawtherapee-cli: default profile + auto WB, 8-bit 4:4:4 JPEG."""
    return [
        rt_exe,
        "-o",
        str(output_path),
        f"-j{quality}",
        "-js3",
        "-Y",
        "-d",
        "-p",
        str(profile_path),
        "-c",  # must be last: everything after it is an input file
        str(raw_path),
    ]


def _raw_awb_method() -> str:
    method = str(config.get("rawtherapee", "awb_method") or "").strip()
    # One word, as RawTherapee spells its WB entries; anything else would be
    # injected into the profile file.
    if not method or "\n" in method or "\r" in method or "=" in method:
        return DEFAULT_RAW_AWB_METHOD
    return method


def _raw_awb_jpeg_quality() -> int:
    try:
        quality = int(
            config.get("rawtherapee", "awb_jpeg_quality")
            or DEFAULT_RAW_AWB_JPEG_QUALITY
        )
    except (TypeError, ValueError):
        return DEFAULT_RAW_AWB_JPEG_QUALITY
    return max(1, min(100, quality))


def _raw_awb_highlight_mode() -> Optional[int]:
    """``awb_highlights``: None for auto, 0 for off, N to always use N."""
    value = str(config.get("rawtherapee", "awb_highlights") or "auto")
    value = value.strip().lower()
    if value in ("", "auto"):
        return None
    if value in ("off", "no", "false", "none"):
        return 0
    try:
        number = float(value)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return max(0, min(500, int(number)))


def clipped_fraction(path: Path) -> float:
    """Share of pixels with a channel at or above _CLIP_LEVEL."""
    with Image.open(path) as img:
        img.draft("RGB", _CLIP_DECODE_BOX)
        arr = np.asarray(img.convert("RGB"))
    return float((arr.max(axis=2) >= _CLIP_LEVEL).mean())


def highlight_compression_for(clipped: float) -> int:
    """RawTherapee HighlightCompr for a render with this much clipping."""
    if clipped < _HIGHLIGHT_TRIGGER:
        return 0
    if clipped >= _HIGHLIGHT_HEAVY:
        return _HIGHLIGHT_COMPRESSION_HEAVY
    return _HIGHLIGHT_COMPRESSION


def _small_grey(
    path: Path, *, displayed: bool, box: tuple[int, int] = _MATCH_DECODE_BOX
) -> Image.Image:
    """A small greyscale copy, turned as a viewer shows it when ``displayed``."""
    with Image.open(path) as img:
        img.draft("L", box)  # JPEG: decode at reduced scale
        if displayed:
            img = ImageOps.exif_transpose(img)
        small = img.convert("L")
    small.thumbnail(box)
    return small


def _displayed_size(path: Path) -> tuple[int, int]:
    """Full-resolution (width, height) as a viewer shows the image."""
    with Image.open(path) as img:
        width, height = img.size
        orientation = img.getexif().get(0x0112, 1)
    return (height, width) if orientation in (5, 6, 7, 8) else (width, height)


def _is_whole_frame(size: tuple[int, int], frame: tuple[int, int]) -> bool:
    """Whether ``size`` is ``frame``'s size, either way round, give or take
    the small difference between the camera's frame and RawTherapee's."""
    for width, height in (frame, frame[::-1]):
        if (
            abs(size[0] - width) <= _FULL_FRAME_TOLERANCE * width
            and abs(size[1] - height) <= _FULL_FRAME_TOLERANCE * height
        ):
            return True
    return False


def _crop_match_scores(original_jpg: Path, new_jpg: Path) -> dict[int, float]:
    """For each turn of the new JPG, how well the current JPG matches some
    part of it at the camera's pixel scale (normalised cross-correlation).
    Turns the current JPG cannot fit inside are left out."""
    cv2 = get_cv2()
    if cv2 is None:
        return {}
    ref_w, ref_h = _displayed_size(original_jpg)
    reference = np.asarray(
        _small_grey(original_jpg, displayed=True, box=_CROP_MATCH_BOX), np.float32
    )
    candidate = _small_grey(new_jpg, displayed=True, box=_CROP_MATCH_BOX)
    # Small-copy pixels per full-resolution pixel of the new JPG.
    k = max(candidate.size) / max(_displayed_size(new_jpg))
    # A light blur on both sides: the scale is only known to a few percent,
    # and fine detail that lands a pixel off would otherwise not match.
    blurred = cv2.GaussianBlur(
        np.asarray(candidate, np.float32), (0, 0), _CROP_MATCH_BLUR
    )
    scores: dict[int, float] = {}
    for angle in (0, 90, 180, 270):
        search = np.ascontiguousarray(np.rot90(blurred, angle // 90))
        for scale in _CROP_MATCH_SCALES:
            tw, th = round(ref_w * k * scale), round(ref_h * k * scale)
            if min(tw, th) < _CROP_MIN_TEMPLATE_PX:
                continue
            if tw > search.shape[1] or th > search.shape[0]:
                continue
            template = cv2.resize(reference, (tw, th), interpolation=cv2.INTER_AREA)
            if float(template.std()) < 1.0:
                continue  # featureless: says nothing about which way is up
            template = cv2.GaussianBlur(template, (0, 0), _CROP_MATCH_BLUR)
            result = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
            result = result[np.isfinite(result)]
            if result.size:
                value = min(1.0, float(result.max()))
                scores[angle] = max(scores.get(angle, -1.0), value)
    return scores


def _crop_rotation(original_jpg: Path, new_jpg: Path) -> Optional[int]:
    """The turn that shows the cropped current JPG as part of the new one,
    or None when no turn clearly wins."""
    scores = _crop_match_scores(original_jpg, new_jpg)
    log.debug("RAW white balance crop match scores: %s", scores)
    if not scores:
        return None
    best = max(scores, key=scores.get)
    runner_up = max((v for a, v in scores.items() if a != best), default=-1.0)
    if (
        scores[best] >= _CROP_MIN_CORRELATION
        and scores[best] - runner_up >= _CROP_MIN_MARGIN
    ):
        return best
    return None


def _same_aspect(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """Same shape either way round (a smaller camera JPG of the same frame)."""
    ra = max(a) / max(1, min(a))
    rb = max(b) / max(1, min(b))
    return abs(ra / rb - 1.0) <= _SAME_ASPECT_TOLERANCE


def _aspect_class(size: tuple[int, int]) -> Optional[bool]:
    """True portrait, False landscape, None (near) square."""
    width, height = size
    if min(width, height) <= 0:
        return None
    if max(width, height) / min(width, height) < _SQUARE_ASPECT:
        return None
    return height > width


def _correlation(a: Image.Image, b: Image.Image) -> float:
    va = np.asarray(a.resize(_MATCH_COMPARE_SIZE, Image.BILINEAR), np.float64)
    vb = np.asarray(b.resize(_MATCH_COMPARE_SIZE, Image.BILINEAR), np.float64)
    va = va - va.mean()
    vb = vb - vb.mean()
    denom = float(np.sqrt((va * va).sum() * (vb * vb).sum()))
    return float((va * vb).sum() / denom) if denom > 0 else 0.0


# PIL's rotate() angle (counter-clockwise) -> the matching lossless transpose.
_ROTATE_CCW = {
    90: Image.Transpose.ROTATE_90,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_270,
}


def match_output_rotation(original_jpg: Path, new_jpg: Path) -> int:
    """Counter-clockwise turn (0/90/180/270) that makes the new JPG show the
    photo the way the current JPG shows it.

    RawTherapee turns its output by the RAW's orientation tag, which is wrong
    whenever the camera guessed wrong (shooting straight down) or the JPG was
    turned in FastStack afterwards. The current JPG is what the user sees,
    so the new one is matched to it picture against picture. A current JPG
    smaller than the new one's frame (cropped, or a smaller camera JPG) is
    first looked for inside that frame; whole pictures are compared when
    that does not clearly settle it. (Comparing whole pictures first gets a
    crop that kept the frame's shape confidently wrong.) Raises RuntimeError
    when the pictures do not say which way is up and the shapes disagree.
    """
    ref_size = _displayed_size(original_jpg)
    new_size = _displayed_size(new_jpg)
    if not _is_whole_frame(ref_size, new_size):
        angle = _crop_rotation(original_jpg, new_jpg)
        if angle is not None:
            return angle
        if not _same_aspect(ref_size, new_size):
            # A crop: its own shape says nothing about which way is up, so
            # filtering turns by it can pick a sideways one. Unsure: keep
            # RawTherapee's turn.
            return 0
    reference = _small_grey(original_jpg, displayed=True)
    candidate = _small_grey(new_jpg, displayed=True)
    ref_aspect = _aspect_class(reference.size)
    scores = {}
    for angle in (0, 90, 180, 270):
        turned = candidate.rotate(angle, expand=True) if angle else candidate
        aspect = _aspect_class(turned.size)
        if ref_aspect is not None and aspect is not None and aspect != ref_aspect:
            continue  # a portrait photo never matches a landscape one
        scores[angle] = _correlation(reference, turned)
    log.debug("RAW white balance rotation scores: %s", scores)
    if scores:
        best = max(scores, key=scores.get)
        if scores[best] >= _MATCH_MIN_CORRELATION:
            return best
    # Unsure: keep RawTherapee's turn if its shape fits, else give up.
    if 0 in scores:
        return 0
    raise RuntimeError("could not match the photo's orientation.")


def frame_crop_box(
    reference: np.ndarray, candidate: np.ndarray
) -> Optional[tuple[int, int, int, int]]:
    """Where the camera's frame (``reference``, greyscale) sits inside
    RawTherapee's slightly larger frame (``candidate``): (left, top, right,
    bottom), or None when the candidate is smaller.

    Starts centred and moves by the median offset phase correlation finds
    in a few regions; regions that disagree or show nothing are ignored.
    """
    rh, rw = reference.shape[:2]
    ch, cw = candidate.shape[:2]
    if ch < rh or cw < rw:
        return None
    x0, y0 = (cw - rw) // 2, (ch - rh) // 2
    cv2 = get_cv2()
    if cv2 is not None:
        window = candidate[y0 : y0 + rh, x0 : x0 + rw]
        shifts = []
        for top, bottom, left, right in _ALIGN_REGIONS:
            rows = slice(int(top * rh), int(bottom * rh))
            cols = slice(int(left * rw), int(right * rw))
            a = np.ascontiguousarray(reference[rows, cols], np.float64)
            b = np.ascontiguousarray(window[rows, cols], np.float64)
            if a.std() < 2.0 or b.std() < 2.0:
                continue
            # The window tapers the region's edges, which would otherwise
            # dominate the spectrum and pull the peak to zero.
            hann = cv2.createHanningWindow((a.shape[1], a.shape[0]), cv2.CV_64F)
            (sx, sy), response = cv2.phaseCorrelate(a, b, window=hann)
            if (
                response >= _ALIGN_MIN_RESPONSE
                and abs(sx) <= _ALIGN_MAX_SHIFT_PX
                and abs(sy) <= _ALIGN_MAX_SHIFT_PX
            ):
                shifts.append((sx, sy))
        if shifts:
            sx = float(np.median([s[0] for s in shifts]))
            sy = float(np.median([s[1] for s in shifts]))
            x0 = min(max(0, x0 + round(sx)), cw - rw)
            y0 = min(max(0, y0 + round(sy)), ch - rh)
            log.debug("RAW white balance frame offset %+.1f, %+.1f px", sx, sy)
    return (x0, y0, x0 + rw, y0 + rh)


def finalize_output(original_jpg: Path, new_jpg: Path, *, quality: int) -> dict:
    """Turn RawTherapee's JPG to match the current JPG and trim it to the
    camera's frame, re-encoding once if either changed anything.

    Returns {"angle": int, "crop": box or None}.
    """
    angle = match_output_rotation(original_jpg, new_jpg)
    ref_w, ref_h = _displayed_size(original_jpg)
    with Image.open(new_jpg) as img:
        exif = img.getexif()
        icc = img.info.get("icc_profile")
        upright = ImageOps.exif_transpose(img)
    if angle:
        upright = upright.transpose(_ROTATE_CCW[angle])
    box = None
    if (
        upright.size != (ref_w, ref_h)
        and upright.width >= ref_w
        and upright.height >= ref_h
        and _is_whole_frame((ref_w, ref_h), upright.size)
    ):
        with Image.open(original_jpg) as ref:
            reference = np.asarray(ImageOps.exif_transpose(ref).convert("L"))
        box = frame_crop_box(reference, np.asarray(upright.convert("L")))
        if box is not None:
            upright = upright.crop(box)
    if not angle and box is None and not exif.get(0x0112, 1) != 1:
        return {"angle": 0, "crop": None}
    exif[0x0112] = 1  # the pixels are upright now
    save_kwargs: dict[str, Any] = {
        "quality": quality,
        "subsampling": 0,
        "exif": exif.tobytes(),
    }
    if icc:
        save_kwargs["icc_profile"] = icc
    tmp = new_jpg.with_name(new_jpg.stem + ".final" + new_jpg.suffix)
    upright.save(tmp, "JPEG", **save_kwargs)
    os.replace(tmp, new_jpg)
    return {"angle": angle, "crop": box}


def rotate_jpeg(path: Path, angle: int, *, quality: int) -> None:
    """Turn a JPEG counter-clockwise by ``angle`` in place, keeping metadata."""
    with Image.open(path) as img:
        exif = img.getexif()
        icc = img.info.get("icc_profile")
        turned = ImageOps.exif_transpose(img).transpose(_ROTATE_CCW[angle])
    exif[0x0112] = 1  # the pixels are upright now
    save_kwargs: dict[str, Any] = {
        "quality": quality,
        "subsampling": 0,
        "exif": exif.tobytes(),
    }
    if icc:
        save_kwargs["icc_profile"] = icc
    tmp = path.with_name(path.stem + ".rot" + path.suffix)
    turned.save(tmp, "JPEG", **save_kwargs)
    os.replace(tmp, path)


class RawWhiteBalanceMixin:
    def _init_raw_white_balance(self) -> None:
        self._raw_awb_lock = threading.Lock()
        self._raw_awb_operations: dict[str, RawWhiteBalanceOperation] = {}
        self._raw_awb_waiting: dict[str, _WaitingForSave] = {}
        self._raw_awb_pass_seconds = _DEFAULT_PASS_SECONDS
        self._raw_awb_meter: dict[str, dict] = {}
        self._raw_awb_hidden_recipes: set[str] = set()
        self._raw_awb_wait_timer = QTimer(self)
        self._raw_awb_wait_timer.setInterval(150)
        self._raw_awb_wait_timer.timeout.connect(self._poll_raw_awb_waiting)
        self._rawWhiteBalanceFinished.connect(
            self._on_raw_white_balance_finished,
            Qt.ConnectionType.QueuedConnection,
        )
        self._rawWhiteBalanceProgress.connect(
            self._on_raw_white_balance_progress,
            Qt.ConnectionType.QueuedConnection,
        )

    # -- Entry point ---------------------------------------------------------

    @Slot()
    def raw_white_balance_to_jpg(self) -> bool:
        """Replace the current JPG with RawTherapee's auto-WB render of its RAW."""
        if getattr(self, "_shutting_down", False) or not self.image_files:
            return False
        if not (0 <= self.current_index < len(self.image_files)):
            return False
        if self._block_auto_adjust_during_crop():
            return False
        if getattr(self, "_is_grid_view_active", False):
            # Never overwrite a photo the user is not looking at.
            self.update_status_message(
                "Open the photo first; W works in the image view."
            )
            return False
        if getattr(self, "view_override_path", None):
            self.update_status_message(
                "W replaces the main JPG; go back to it from the backup/developed view first."
            )
            return False

        image_file = self.image_files[self.current_index]
        jpg_path = Path(image_file.path)
        raw_path = image_file.raw_path
        if (
            raw_path is None
            or jpg_path.suffix.lower() not in JPG_EXTENSIONS
            or self._key(raw_path) == self._key(jpg_path)
        ):
            self.update_status_message(
                "RAW white balance needs a JPG with a matching RAW file."
            )
            return False
        if not Path(raw_path).is_file():
            self.update_status_message(f"RAW file is missing: {Path(raw_path).name}")
            return False
        if not jpg_path.is_file():
            self.update_status_message(f"JPG is missing: {jpg_path.name}")
            return False

        jpg_key = self._key(jpg_path) or str(jpg_path)
        with self._raw_awb_lock:
            busy = jpg_key in self._raw_awb_operations
        if busy or jpg_key in self._raw_awb_waiting:
            self.update_status_message(
                "RAW white balance is already running for this image."
            )
            return False

        rt_exe = self._resolve_raw_awb_executable()
        if rt_exe is None:
            return False

        operation_id = uuid.uuid4().hex
        saving = False
        if self._raw_awb_current_session_dirty(jpg_path):
            # Save first: the edits land in the JPG (and so in its backup),
            # and W starts from what was saved.
            if not self._flush_current_live_edit_session_for_navigation():
                error = getattr(self, "_last_save_prepare_error", None)
                self.update_status_message(
                    f"Could not save the edits ({error or 'save refused'}); "
                    "W cancelled.",
                    timeout=6000,
                )
                return False
            saving = True
        if saving or self._raw_awb_target_busy(jpg_path, jpg_key):
            self._raw_awb_waiting[jpg_key] = _WaitingForSave(
                operation_id=operation_id,
                jpg_path=jpg_path,
                raw_path=Path(raw_path),
                rt_exe=rt_exe,
                deadline=time.monotonic() + _SAVE_WAIT_SECONDS,
                sidecar=self.sidecar,
                directory_key=self._key(self.image_dir),
            )
            self._raw_awb_meter_stage(
                operation_id, "save", _SAVE_SECONDS, jpg_path.name
            )
            self.update_status_message(
                f"Saving your edits to {jpg_path.name}, then RAW white balance..."
            )
            self._raw_awb_wait_timer.start()
            return True
        return self._start_raw_awb(
            operation_id, jpg_path, Path(raw_path), jpg_key, rt_exe
        )

    def _poll_raw_awb_waiting(self) -> None:
        """Start the W presses whose edits have finished saving."""
        now = time.monotonic()
        for jpg_key, waiting in list(self._raw_awb_waiting.items()):
            if waiting.directory_key != self._key(self.image_dir):
                # Its undo entry and sidecar belong to the folder W was
                # pressed in, which is no longer open.
                self._raw_awb_waiting.pop(jpg_key, None)
                self._raw_awb_meter_end(waiting.operation_id, ok=False)
                self.update_status_message(
                    f"W cancelled for {waiting.jpg_path.name}: another folder "
                    "was opened.",
                    timeout=6000,
                )
                continue
            if self._raw_awb_save_failed(waiting.jpg_path, jpg_key):
                self._raw_awb_waiting.pop(jpg_key, None)
                self._raw_awb_meter_end(waiting.operation_id, ok=False)
                self.update_status_message(
                    f"Saving the edits to {waiting.jpg_path.name} failed; "
                    "W cancelled.",
                    timeout=6000,
                )
                continue
            if self._raw_awb_target_busy(waiting.jpg_path, jpg_key):
                if now > waiting.deadline:
                    self._raw_awb_waiting.pop(jpg_key, None)
                    self._raw_awb_meter_end(waiting.operation_id, ok=False)
                    self.update_status_message(
                        "W gave up waiting for the edits to save.", timeout=6000
                    )
                continue
            self._raw_awb_waiting.pop(jpg_key, None)
            self._start_raw_awb(
                waiting.operation_id,
                waiting.jpg_path,
                waiting.raw_path,
                jpg_key,
                waiting.rt_exe,
            )
        if not self._raw_awb_waiting:
            self._raw_awb_wait_timer.stop()

    def _raw_awb_save_failed(self, jpg_path: Path, jpg_key: str) -> bool:
        """Whether the save W was waiting for failed (kept for retry on quit,
        or the live session reads unsaved again)."""
        recovery = getattr(self, "_pending_save_recovery", {}) or {}
        for target in recovery:
            try:
                if self._key(Path(target)) == jpg_key:
                    return True
            except (OSError, TypeError, ValueError):
                continue
        if self._current_image_path_key() == jpg_key and not self._is_image_saving(
            str(jpg_path)
        ):
            return self._raw_awb_current_session_dirty(jpg_path)
        return False

    def _start_raw_awb(
        self,
        operation_id: str,
        jpg_path: Path,
        raw_path: Path,
        jpg_key: str,
        rt_exe: str,
    ) -> bool:
        if not jpg_path.is_file() or not raw_path.is_file():
            self._raw_awb_meter_end(operation_id, ok=False)
            self.update_status_message(f"{jpg_path.name} or its RAW is missing.")
            return False
        # Settings first: nothing is registered or hidden until they are read.
        method = _raw_awb_method()
        quality = _raw_awb_jpeg_quality()
        highlights = _raw_awb_highlight_mode()
        operation = RawWhiteBalanceOperation(
            operation_id=operation_id,
            jpg_key=jpg_key,
            jpg_path=jpg_path,
            raw_path=raw_path,
            jpg_fingerprint=None,
            directory_key=self._key(self.image_dir),
            sidecar=self.sidecar,
        )
        self._raw_awb_zero_baseline(operation)
        operation.jpg_fingerprint = self._file_state_fingerprint(jpg_path)
        with self._raw_awb_lock:
            self._raw_awb_operations[jpg_key] = operation

        log.info(
            "Starting RAW auto white balance (%s, highlights %s): %s -> %s",
            method,
            "auto" if highlights is None else highlights,
            raw_path,
            jpg_path,
        )
        self._raw_awb_meter_stage(
            operation_id,
            "develop" if highlights is None else "develop_only",
            self._raw_awb_pass_seconds,
            raw_path.name,
        )
        self.update_status_message(
            f"Developing {raw_path.name} with auto white balance...",
            timeout=_RAW_AWB_TIMEOUT_SECONDS * 1000,
        )
        thread = threading.Thread(
            target=self._raw_awb_worker,
            args=(operation, rt_exe, method, quality, highlights),
            daemon=True,
            name="RawWhiteBalance",
        )
        operation.worker = thread
        try:
            thread.start()
        except RuntimeError as exc:
            log.exception("Could not start the RAW white balance worker")
            with self._raw_awb_lock:
                current = self._raw_awb_operations.get(jpg_key)
                if current is not None and current.operation_id == operation_id:
                    self._raw_awb_operations.pop(jpg_key, None)
            self._raw_awb_restore_stash(operation)
            self._raw_awb_meter_end(operation_id, ok=False)
            self.update_status_message(f"RAW white balance could not start: {exc}")
            return False
        return True

    def _raw_awb_zero_baseline(self, operation: RawWhiteBalanceOperation) -> None:
        """Make edits made while W runs start from zero on the JPG as saved.

        Reopening a FastStack-edited JPG loads its backup and replays the
        saved edits; a tweak made then would carry those earlier edits onto
        the new render as well (a white balance fix applied twice). So the
        saved edit state is hidden from the editor until W ends, and an
        editor session still holding the JPG is dropped. It is hidden in
        memory only: the sidecar keeps it until the new JPG is in place, so
        quitting (or a crash) while RawTherapee works loses nothing.
        """
        jpg_path = operation.jpg_path
        operation.recipe_hidden = True
        self._raw_awb_hidden_recipes.add(operation.jpg_key)
        if self._raw_awb_editor_holds(jpg_path):
            # An editor open on the photo shown stays open, reloaded from the
            # JPG as saved (sliders at zero); one left on another photo closes.
            reload_editor = (
                self.ui_state.isEditorOpen
                and self._current_image_path_key() == operation.jpg_key
            )
            if self.ui_state.isEditorOpen and not reload_editor:
                self.ui_state.isEditorOpen = False
            self._clear_active_auto_adjust_state(
                "RAW white balance starts from the saved JPG", clear_editor=False
            )
            self.image_editor.clear()
            self._clear_live_edit_session_state()
            with self._preview_lock:
                self._clear_last_rendered_preview_locked()
            if reload_editor and not self.load_image_for_editing():
                self.ui_state.isEditorOpen = False
            self.sync_ui_state()

    def _raw_awb_restore_stash(self, operation: RawWhiteBalanceOperation) -> None:
        """Show the JPG's saved edit state to the editor again."""
        if not operation.recipe_hidden:
            return
        operation.recipe_hidden = False
        self._raw_awb_hidden_recipes.discard(operation.jpg_key)

    def raw_awb_recipe_hidden(self, metadata_key: Optional[str]) -> bool:
        """Whether W hides this JPG's saved edit state (see save_pipeline)."""
        hidden = getattr(self, "_raw_awb_hidden_recipes", None)
        return bool(hidden) and metadata_key in hidden

    # -- Guards ----------------------------------------------------------------

    def _raw_awb_target_busy(
        self, jpg_path: Path, jpg_key: str, *, ignore_request: Any = None
    ) -> bool:
        """True while a FastStack save of this JPG is running or queued."""
        if self._is_image_saving(str(jpg_path)):
            return True
        pending = getattr(self, "_pending_edit_save_requests", {}) or {}
        for key, request in pending.items():
            if ignore_request is not None and request is ignore_request:
                continue
            if key == jpg_key:
                return True
            context = request.get("context", {}) if isinstance(request, dict) else {}
            target = context.get("target")
            try:
                if target and self._key(Path(target)) == jpg_key:
                    return True
            except (OSError, TypeError, ValueError):
                continue
        return False

    def _raw_awb_current_session_dirty(self, jpg_path: Path) -> bool:
        """True when the open editor session for this JPG has unsaved edits."""
        if getattr(self, "_auto_adjust_save_pending_action", None) is not None:
            return True
        if not self._is_current_live_edit_session_dirty():
            return False
        editor_path = getattr(self.image_editor, "current_filepath", None)
        if editor_path is None:
            return True
        try:
            return self._key(Path(editor_path)) == self._key(jpg_path)
        except (OSError, TypeError, ValueError):
            return True

    def raw_awb_pending_for_current(self) -> bool:
        """Whether W is running (or waiting to) for the photo shown."""
        key = self._current_image_path_key()
        if key is None:
            return False
        with self._raw_awb_lock:
            running = key in self._raw_awb_operations
        return running or key in self._raw_awb_waiting

    def _raw_awb_editor_holds(self, jpg_path: Path) -> bool:
        editor_path = getattr(self.image_editor, "current_filepath", None)
        if editor_path is None:
            return False
        try:
            return self._key(Path(editor_path)) == self._key(jpg_path)
        except (OSError, TypeError, ValueError):
            return False

    def _resolve_raw_awb_executable(self) -> Optional[str]:
        rt_exe = self._configured_rawtherapee_executable()
        if not rt_exe or not os.path.isfile(rt_exe):
            found = shutil.which("rawtherapee-cli") or shutil.which(
                "rawtherapee-cli.exe"
            )
            if not found:
                self.update_status_message(
                    "rawtherapee-cli not found. Install RawTherapee or set its "
                    "path in Settings."
                )
                log.error("RawTherapee CLI not configured or missing: %r", rt_exe)
                return None
            rt_exe = found
        if Path(rt_exe).stem.lower() == "rawtherapee":
            # The GUI binary is configured; the batch converter sits next to it.
            cli = Path(rt_exe).with_name(f"rawtherapee-cli{Path(rt_exe).suffix}")
            if cli.is_file():
                rt_exe = str(cli)
        valid, error = validate_executable_path(
            rt_exe, app_type="rawtherapee", allow_custom_paths=True
        )
        if not valid:
            self.update_status_message(f"RawTherapee validation failed: {error}")
            log.error("RawTherapee executable rejected: %s", error)
            return None
        return rt_exe

    # -- Saves made while W runs -------------------------------------------------

    def _raw_awb_park_save(self, request: dict, saving_status: Optional[str]) -> bool:
        """Hold a save of a JPG that W is re-rendering (called first thing in
        _submit_save_request_async). Returns True when it was held.

        The request renders the old pixels; it is submitted again once W is
        done, rendered from the new ones (or unchanged if W fails). Only the
        newest one per JPG is kept: it carries every edit made so far.
        """
        operations = getattr(self, "_raw_awb_operations", None)
        if not operations:
            return False
        context = request.get("context", {}) if isinstance(request, dict) else {}
        # Matched by the file the save writes: a save of the photo's
        # -developed variant carries the main JPG's image key but does not
        # touch the JPG W replaces.
        keys = set()
        target = context.get("target")
        if target:
            try:
                keys.add(self._key(Path(target)))
            except (OSError, TypeError, ValueError):
                return False
        elif context.get("save_image_key"):
            keys.add(context["save_image_key"])
        with self._raw_awb_lock:
            operation = next(
                (
                    op
                    for key, op in operations.items()
                    if key in keys and not op.cancel_requested
                ),
                None,
            )
            if operation is None:
                return False
            operation.parked = {"request": request, "saving_status": saving_status}
        # Bookkeeping a submitted save would do: the session is no longer
        # unsaved, and returning to the photo restores these edits.
        self._mark_current_live_edit_session_submitted(
            context.get("save_revision", 0),
            bool(context.get("save_has_meaningful_edits", True)),
        )
        self._remember_pending_edit_save_request(request)
        log.info(
            "Holding the save of %s until RAW white balance finishes",
            operation.jpg_path.name,
        )
        self._raw_awb_meter_flag(operation.operation_id, edits=True)
        return True

    def _raw_awb_release_parked(
        self, operation: RawWhiteBalanceOperation, *, onto_new_pixels: bool
    ) -> bool:
        """Submit the held save, rendered from the new JPG when
        ``onto_new_pixels``. Returns True when it was submitted."""
        parked, operation.parked = operation.parked, None
        if not parked:
            return False
        request = parked["request"]
        if onto_new_pixels:
            try:
                self._raw_awb_rebase_request(request, operation.jpg_path)
            except Exception:
                log.exception(
                    "Could not move the edits of %s onto the new JPG",
                    operation.jpg_path,
                )
                self._raw_awb_forget_request(request)
                return False
        return bool(
            self._submit_save_request_async(
                request, saving_status=parked.get("saving_status")
            )
        )

    def _raw_awb_forget_request(self, request: dict) -> None:
        pending = getattr(self, "_pending_edit_save_requests", None)
        if not pending:
            return
        for key, value in list(pending.items()):
            if value is request:
                pending.pop(key, None)

    def _raw_awb_fresh_editor(self, jpg_path: Path) -> ImageEditor:
        editor = ImageEditor()
        box = getattr(self.image_editor, "preview_master_box", None)
        if isinstance(box, tuple) and len(box) == 2:
            editor.preview_master_box = box
        if not editor.load_image(str(jpg_path)):
            raise RuntimeError(f"could not load {jpg_path.name}")
        return editor

    def _raw_awb_rebase_request(self, request: dict, jpg_path: Path) -> None:
        """Point a captured save request at the new JPG's pixels, keeping its
        edits, masks and layers."""
        fresh = self._raw_awb_fresh_editor(jpg_path)
        new = fresh.snapshot_for_export(save_target_path=jpg_path)
        snapshot = request["snapshot"]
        for key in (
            "source_arr",
            "source_shape",
            "filepath_snapshot",
            "source_filepath",
            "current_mtime",
            "bit_depth",
            "main_exif",
            "source_exif",
            "source_icc_bytes",
        ):
            snapshot[key] = new[key]
        # Results worked out on the old pixels do not fit the new ones, even
        # at the same size (the trimmed render usually is); the save works
        # them out again.
        snapshot["halo_results"] = {}
        snapshot["depth_map"] = None
        snapshot["export_cache"] = LayerDensityCache()
        # The saved edit state must name the new JPG (then its backup) as the
        # source, or reopening the editor would bring the camera JPG back.
        request["context"]["edit_source_path"] = str(jpg_path)

    def _raw_awb_carry_live_session(self, jpg_path: Path) -> bool:
        """Put the new JPG's pixels under the open editor session's edits.

        The session stays unsaved, so it is saved like any live edit (on
        navigation, Ctrl+S, quit).
        """
        try:
            fresh = self._raw_awb_fresh_editor(jpg_path)
            fresh._ensure_float_image()
        except Exception:
            log.exception("Could not load the new %s into the editor", jpg_path)
            return False
        editor = self.image_editor
        with editor._lock:
            editor.original_image = fresh.original_image
            editor.float_image = fresh.float_image
            editor.float_preview = fresh.float_preview
            editor.bit_depth = fresh.bit_depth
            editor.current_mtime = fresh.current_mtime
            editor.source_filepath = jpg_path
            editor._source_exif_bytes = fresh._source_exif_bytes
            # Halo and depth results belong to the old pixels; they are
            # worked out again (below) for the layers that need them.
            editor._halo_results = {}
            editor._depth_map = None
            editor._local_density_cache.clear()
            for name in (
                "_cached_preview",
                "_cached_highlight_analysis",
                "_cached_detail_bands",
                "_cached_u8_lut",
                "_cached_u8_wb_lut",
            ):
                if hasattr(editor, name):
                    setattr(editor, name, None)
            editor._cached_rev = -1
            # New pixels are a new revision: the session reads unsaved.
            editor._edits_rev += 1
        # Auto-adjust steps (-/+) were measured on the old pixels.
        self._active_auto_adjust_state = None
        # Halo and depth work in flight is for the old pixels too: drop it
        # and start again on the new ones.
        self._halo_reset_for_navigation()
        self._depth_reset_for_navigation()
        try:
            self._schedule_halo_jobs()
            self._schedule_depth_job()
        except Exception:
            log.exception("Could not restart halo/depth work on the new JPG")
        return True

    # -- Worker ----------------------------------------------------------------

    def _raw_awb_worker(
        self,
        operation: RawWhiteBalanceOperation,
        rt_exe: str,
        method: str,
        quality: int,
        highlights: Optional[int],
    ) -> None:
        result = RawWhiteBalanceResult(
            operation.operation_id,
            operation.jpg_key,
            False,
            "RAW white balance did not complete.",
        )
        tmp_dir: Optional[Path] = None
        staged: Optional[Path] = None
        handed_over = False
        try:
            tmp_dir = Path(tempfile.mkdtemp(prefix="faststack-raw-awb-"))
            forced = highlights if highlights else 0
            output_path = self._raw_awb_run_pass(
                operation, rt_exe, tmp_dir, "develop", method, forced, quality
            )
            detail = ""
            if highlights is None:
                clipped = clipped_fraction(output_path)
                compression = highlight_compression_for(clipped)
                log.info(
                    "RAW white balance: %.2f%% clipped in %s -> highlight "
                    "compression %d",
                    clipped * 100,
                    operation.raw_path.name,
                    compression,
                )
                if compression:
                    self._rawWhiteBalanceProgress.emit(
                        {
                            "op": operation.operation_id,
                            "stage": "highlights",
                            "expected": self._raw_awb_pass_seconds,
                            "detail": f"{clipped * 100:.1f}% CLIP",
                        }
                    )
                    output_path = self._raw_awb_run_pass(
                        operation,
                        rt_exe,
                        tmp_dir,
                        "highlights",
                        method,
                        compression,
                        quality,
                    )
                    detail = "highlights pulled back"
            self._rawWhiteBalanceProgress.emit(
                {
                    "op": operation.operation_id,
                    "stage": "finish",
                    "expected": _FINISH_SECONDS,
                }
            )
            finished = finalize_output(operation.jpg_path, output_path, quality=quality)
            if finished["angle"]:
                log.info(
                    "Turned RawTherapee's output %d degrees to match %s",
                    finished["angle"],
                    operation.jpg_path.name,
                )
            if finished["crop"]:
                log.info("Trimmed RawTherapee's output to %s", finished["crop"])

            # Stage next to the JPG so the swap is one atomic rename. A hidden
            # *.tmp name is ignored by the folder watcher and the indexer.
            staged = operation.jpg_path.with_name(
                f".{operation.jpg_path.name}.{uuid.uuid4().hex[:8]}.raw-awb.tmp"
            )
            shutil.copyfile(output_path, staged)
            with self._raw_awb_lock:
                cancelled = operation.cancel_requested
                if not cancelled:
                    operation.staged_path = staged
            if cancelled:
                raise _Cancelled()
            result = RawWhiteBalanceResult(
                operation.operation_id,
                operation.jpg_key,
                True,
                None,
                staged,
                detail,
            )
            handed_over = True
        except _Cancelled:
            result.error = "RAW white balance was cancelled."
        except Exception as exc:
            log.exception("RAW auto white balance failed for %s", operation.raw_path)
            result.error = str(exc) or exc.__class__.__name__
        finally:
            if staged is not None and not handed_over:
                # A failed or partial copy must not stay beside the JPG.
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    log.warning("Could not remove %s", staged, exc_info=True)
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            self._rawWhiteBalanceFinished.emit(result)

    def _raw_awb_run_pass(
        self,
        operation: RawWhiteBalanceOperation,
        rt_exe: str,
        tmp_dir: Path,
        name: str,
        method: str,
        highlight_compression: int,
        quality: int,
    ) -> Path:
        """One rawtherapee-cli run; returns the JPG it wrote."""
        profile_path = tmp_dir / f"{name}.pp3"
        profile_path.write_text(
            build_raw_awb_profile(method, highlight_compression), encoding="utf-8"
        )
        output_path = tmp_dir / f"{name}.jpg"
        cmd = build_raw_awb_command(
            rt_exe, operation.raw_path, output_path, profile_path, quality=quality
        )
        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "env": child_environment(),
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        started = time.monotonic()
        process = subprocess.Popen(cmd, shell=False, **popen_kwargs)
        with self._raw_awb_lock:
            operation.process = process
            cancelled = operation.cancel_requested
        if cancelled:
            self._stop_raw_process(process)
            raise _Cancelled()
        try:
            stdout, stderr = process.communicate(timeout=_RAW_AWB_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self._stop_raw_process(process)
            raise RuntimeError(
                f"RawTherapee timed out after {_RAW_AWB_TIMEOUT_SECONDS} seconds."
            ) from None
        tool_name = Path(rt_exe).stem or "rawtherapee-cli"
        log_tool_output(tool_name, stdout)
        log_tool_output(tool_name, stderr)
        if operation.cancel_requested:
            raise _Cancelled()
        if process.returncode != 0:
            detail = (stderr or stdout or "").strip().splitlines()
            raise RuntimeError(
                f"RawTherapee failed (exit code {process.returncode})"
                + (f": {detail[-1]}" if detail else ".")
            )
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError("RawTherapee did not write a JPG.")
        try:
            with Image.open(output_path) as img:
                img.verify()
        except Exception as exc:
            raise RuntimeError(f"RawTherapee wrote an unreadable JPG: {exc}")
        # Running estimate for the progress meter.
        took = time.monotonic() - started
        self._raw_awb_pass_seconds = 0.6 * self._raw_awb_pass_seconds + 0.4 * took
        return output_path

    # -- Completion (Qt thread) ------------------------------------------------

    def _on_raw_white_balance_finished(self, result: RawWhiteBalanceResult) -> None:
        with self._raw_awb_lock:
            operation = self._raw_awb_operations.get(result.jpg_key)
            if operation is None or operation.operation_id != result.operation_id:
                operation = None
            else:
                self._raw_awb_operations.pop(result.jpg_key, None)
        staged = result.staged_path
        try:
            if operation is None or getattr(self, "_shutting_down", False):
                return
            if not result.success or staged is None:
                self._raw_awb_fail(
                    operation, f"RAW white balance failed: {result.error}"
                )
                return
            error = self._apply_raw_awb_result(operation, staged, result.detail)
            if error:
                self._raw_awb_fail(operation, f"RAW white balance not applied: {error}")
        finally:
            if staged is not None:
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    log.warning("Could not remove %s", staged, exc_info=True)

    def _raw_awb_fail(self, operation: RawWhiteBalanceOperation, message: str) -> None:
        """W did not replace the JPG: edits held for it go onto the old one."""
        released = self._raw_awb_release_parked(operation, onto_new_pixels=False)
        if not released:
            self._raw_awb_restore_stash(operation)
        self._raw_awb_meter_end(operation.operation_id, ok=False)
        self.update_status_message(message, timeout=8000)

    def _apply_raw_awb_result(
        self, operation: RawWhiteBalanceOperation, staged: Path, detail: str = ""
    ) -> Optional[str]:
        """Swap the developed JPG in. Returns an error message, or None."""
        jpg_path = operation.jpg_path
        parked_request = operation.parked["request"] if operation.parked else None
        if operation.directory_key != self._key(self.image_dir):
            # Its undo entry would belong to a folder that is no longer open.
            return "another folder was opened while RawTherapee was running."
        if self._file_state_fingerprint(jpg_path) != operation.jpg_fingerprint:
            return f"{jpg_path.name} changed while RawTherapee was running."
        if self._raw_awb_target_busy(
            jpg_path, operation.jpg_key, ignore_request=parked_request
        ):
            return f"{jpg_path.name} is being saved."
        on_current = self._current_image_path_key() == operation.jpg_key
        if on_current and getattr(self.ui_state, "isCropping", False) is True:
            return "a crop was in progress; apply or cancel it, then press W again."
        live = (
            on_current
            and self._raw_awb_editor_holds(jpg_path)
            and self._current_live_session_has_meaningful_edits()
        )

        try:
            original_stat = jpg_path.stat()
        except OSError as exc:
            return str(exc)
        backup_path = create_backup_file(jpg_path)
        if backup_path is None:
            return f"could not back up {jpg_path.name}."
        if self._file_state_fingerprint(jpg_path) != operation.jpg_fingerprint:
            # Written to while it was being backed up.
            try:
                backup_path.unlink(missing_ok=True)
            except OSError:
                pass
            return f"{jpg_path.name} changed while RawTherapee was running."
        self._suppress_watcher_paths(jpg_path, backup_path)
        try:
            _safe_replace(staged, jpg_path)
        except OSError as exc:
            log.exception("Could not replace %s", jpg_path)
            try:
                backup_path.unlink(missing_ok=True)
            except OSError:
                pass
            return f"could not replace {jpg_path.name}: {exc}"
        # Keep the capture-time file date, as FastStack's own saves do, so a
        # folder sorted by date does not move the photo.
        try:
            os.utime(jpg_path, (original_stat.st_atime, original_stat.st_mtime))
        except OSError:
            log.warning("Could not restore timestamps on %s", jpg_path)
        log.info(
            "RAW auto white balance replaced %s (backup %s)", jpg_path, backup_path
        )

        # Deferred saves or a crash-recovery edit state for the old pixels
        # must never land on the new JPG. (A held save is kept in
        # operation.parked and handled below.)
        try:
            self._retire_pending_saves_for_photoshop(jpg_path, jpg_path)
        except Exception:
            log.exception("Could not retire pending saves for %s", jpg_path)

        # The saved edit state belongs to the old pixels (now the backup): it
        # is cleared here, and the undo record keeps it for Ctrl+Z.
        sidecar = operation.sidecar or self.sidecar
        metadata_before = self._mark_image_edited_in_sidecar(
            sidecar,
            jpg_path,
            clear_saved_edit_state=True,
            background=True,
        )
        self._raw_awb_restore_stash(operation)
        self.undo_history.append(
            (
                RAW_WHITE_BALANCE_ACTION,
                self._build_edit_undo_data(
                    jpg_path,
                    backup_path,
                    metadata_path=jpg_path,
                    metadata_before=metadata_before,
                    sidecar=sidecar,
                ),
                time.time(),
            )
        )

        live_carried = live and self._raw_awb_carry_live_session(jpg_path)
        saved = False
        if parked_request is not None:
            # The held save is written from the new pixels even when the
            # editor stays open on the photo (Ctrl+S, then kept editing):
            # the JPG gets what was saved, and later edits stay live.
            saved = self._raw_awb_release_parked(operation, onto_new_pixels=True)

        self._refresh_after_raw_awb(
            jpg_path, on_current=on_current, keep_editor=live_carried
        )
        self._auto_add_edited_to_batch_if_enabled(jpg_path)
        self._raw_awb_meter_end(operation.operation_id, ok=True)
        parts = [f"Auto white balance from {operation.raw_path.name} applied"]
        if detail:
            parts.append(detail)
        if live_carried or saved:
            parts.append("your edits moved onto it")
        elif parked_request is not None:
            parts.append(
                "but the edits saved while it ran could not be moved onto it"
            )
        self.update_status_message(", ".join(parts) + " (Ctrl+Z to undo).")
        return None

    def _current_image_path_key(self) -> Optional[str]:
        if not (0 <= self.current_index < len(self.image_files)):
            return None
        return self._key(self.image_files[self.current_index].path)

    def _refresh_after_raw_awb(
        self, jpg_path: Path, *, on_current: bool, keep_editor: bool = False
    ) -> None:
        """Show the new pixels; mirrors the background save completion."""
        # Cleared even when the user has moved on: the swap keeps the file's
        # mtime, so a session still holding the old pixels would otherwise
        # be reused as if it were current.
        reload_editor = False
        if (
            not keep_editor
            and self.image_editor is not None
            and self._raw_awb_editor_holds(jpg_path)
        ):
            # The editor holds the old pixels; the next edit must start from
            # the new file. An editor open on the photo being shown stays
            # open and is reloaded below (edits made meanwhile went through
            # keep_editor or a held save, so nothing is lost); one left on
            # another photo is closed.
            if self.ui_state.isEditorOpen:
                if on_current:
                    reload_editor = True
                else:
                    self.ui_state.isEditorOpen = False
            self._clear_active_auto_adjust_state(
                "RAW white balance replaced the image", clear_editor=False
            )
            self.image_editor.clear()
            self._clear_live_edit_session_state()
            with self._preview_lock:
                self._clear_last_rendered_preview_locked()

        preserved_path = self._current_image_path()
        self._begin_direct_image_transition("RAW white balance replaced a JPG")
        self.refresh_image_list()
        if preserved_path is not None:
            self._reindex_after_save(preserved_path)
        self._invalidate_decoded_path(jpg_path, force=True)
        if on_current:
            if (
                reload_editor
                and self.ui_state.isEditorOpen
                and self._current_image_path_key() == self._key(jpg_path)
                and not self.load_image_for_editing()
            ):
                self.ui_state.isEditorOpen = False
            self.prefetcher.update_prefetch(self.current_index)
            self.sync_ui_state()
            if keep_editor:
                with self._preview_lock:
                    self._clear_last_rendered_preview_locked()
                self._kick_preview_worker()
            if self.ui_state.isHistogramVisible:
                self.update_histogram()
        else:
            self._do_prefetch(self.current_index)
        self._restart_quality_decode_timer()
        if self.ui_state:
            self.ui_state.variantBadgesChanged.emit()

    # -- Progress meter ------------------------------------------------------------

    def _on_raw_white_balance_progress(self, payload: dict) -> None:
        self._raw_awb_meter_stage(
            payload["op"],
            payload["stage"],
            float(payload.get("expected") or _DEFAULT_PASS_SECONDS),
            detail=payload.get("detail", ""),
        )

    def _raw_awb_meter_stage(
        self,
        operation_id: str,
        stage: str,
        expected_seconds: float,
        name: Optional[str] = None,
        *,
        detail: str = "",
    ) -> None:
        entry = self._raw_awb_meter.get(operation_id)
        if entry is None:
            entry = {
                "opId": operation_id,
                "name": name or "",
                "edits": False,
                "created": time.time(),
            }
            self._raw_awb_meter[operation_id] = entry
        elif entry.get("state") in ("done", "failed"):
            return
        start, end = _STAGE_SPANS.get(stage, (0.0, 0.97))
        entry.update(
            {
                "state": "running",
                "stage": "develop" if stage == "develop_only" else stage,
                "from": start,
                "to": end,
                "startMs": time.time() * 1000.0,
                "expectedMs": max(0.5, expected_seconds) * 1000.0,
                "detail": detail,
            }
        )
        if name:
            entry["name"] = name
        self._publish_raw_awb_meter()

    def _raw_awb_meter_flag(self, operation_id: str, *, edits: bool) -> None:
        entry = self._raw_awb_meter.get(operation_id)
        if entry is not None:
            entry["edits"] = edits
            self._publish_raw_awb_meter()

    def _raw_awb_meter_end(self, operation_id: str, *, ok: bool) -> None:
        entry = self._raw_awb_meter.get(operation_id)
        if entry is None:
            return
        entry["state"] = "done" if ok else "failed"
        entry["endMs"] = time.time() * 1000.0
        self._publish_raw_awb_meter()

        def forget():
            if self._raw_awb_meter.get(operation_id) is entry:
                self._raw_awb_meter.pop(operation_id, None)
                self._publish_raw_awb_meter()

        QTimer.singleShot(_METER_DONE_MS if ok else _METER_FAILED_MS, forget)

    def _publish_raw_awb_meter(self) -> None:
        """Show the newest running W (else the newest finished one)."""
        if getattr(self, "ui_state", None) is None:
            return
        entries = list(self._raw_awb_meter.values())
        running = [e for e in entries if e.get("state") == "running"]
        pool = running or entries
        if not pool:
            self.ui_state.rawAwbProgress = {"active": False}
            return
        shown = max(pool, key=lambda e: e["created"])
        self.ui_state.rawAwbProgress = {
            **shown,
            "active": True,
            "count": len(running),
        }

    # -- Shutdown --------------------------------------------------------------

    def _raw_awb_drop_parked_for(self, target: Optional[str]) -> None:
        """A newer save of ``target`` was written (the save made on quit):
        a held save of the same file is older and must not overwrite it."""
        operations = getattr(self, "_raw_awb_operations", None)
        if not operations or not target:
            return
        try:
            target_key = self._key(Path(target))
        except (OSError, TypeError, ValueError):
            return
        with self._raw_awb_lock:
            for operation in operations.values():
                parked = operation.parked
                if not parked:
                    continue
                context = parked["request"].get("context", {})
                try:
                    parked_key = self._key(Path(context.get("target") or ""))
                except (OSError, TypeError, ValueError):
                    continue
                if parked_key == target_key:
                    operation.parked = None

    def _flush_raw_awb_parked_saves(self) -> None:
        """At quit: write held saves as they are (onto the old pixels)."""
        operations = getattr(self, "_raw_awb_operations", None)
        if not operations:
            return
        with self._raw_awb_lock:
            held = []
            for operation in operations.values():
                if operation.parked:
                    held.append(operation.parked)
                    operation.parked = None
                operation.cancel_requested = True
        for parked in held:
            try:
                self._run_save_request_sync(parked["request"], saving_status=None)
            except Exception:
                log.exception("Could not write edits held for RAW white balance")

    def _cancel_raw_white_balance_operations(self) -> None:
        with self._raw_awb_lock:
            operations = list(self._raw_awb_operations.values())
            self._raw_awb_operations.clear()
            for operation in operations:
                operation.cancel_requested = True
        for operation in operations:
            if operation.process is not None:
                self._stop_raw_process(operation.process)
        current = threading.current_thread()
        for operation in operations:
            worker = operation.worker
            if worker is not None and worker is not current:
                worker.join(timeout=0.5)
            if operation.staged_path is not None:
                try:
                    operation.staged_path.unlink(missing_ok=True)
                except OSError:
                    pass
            self._raw_awb_restore_stash(operation)


class _Cancelled(Exception):
    pass
