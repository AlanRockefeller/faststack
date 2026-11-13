# faststack/ai/inpaint.py
"""LaMa inpainting, downloaded on first use.

Nothing here is bundled with FastStack.  The first time a feature needs
inpainting, the owner is asked to download:

* **ONNX Runtime** (Microsoft, MIT licence): the CPU inference engine, as the
  official PyPI wheel built for the running Python and platform.  When
  ``onnxruntime`` is already importable (a source install that has it) it is
  used as is and nothing is downloaded for it.
* **LaMa** (Samsung AI Center, Apache-2.0 licence; "Resolution-robust Large
  Mask Inpainting with Fourier Convolutions", 2021), the ONNX export
  published as ``Carve/LaMa-ONNX`` on Hugging Face, pinned to one revision.

Both are verified against a SHA-256 before they are used (the wheel against
the digest PyPI publishes for it, the model against the digest pinned here)
and installed atomically under ``<app-data>/ai``.  After that everything runs
offline.  Deleting that folder removes them; FastStack offers the download
again the next time it is needed.

The model takes a fixed 512x512 input.  ``LamaInpainter.inpaint`` crops a
square around the hole, scales it to 512, fills it and scales the result back,
so the hole is filled at roughly half to a third of full resolution: right for
the blurred backgrounds behind focus-stacked subjects, which carry no finer
detail than that.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import platform
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Callable, Optional

import numpy as np

from faststack.logging_setup import get_app_data_dir

log = logging.getLogger(__name__)

ORT_VERSION = "1.31.1"
ORT_PYPI_JSON = f"https://pypi.org/pypi/onnxruntime/{ORT_VERSION}/json"
# Download size of the CPU wheel, for the prompt before the exact wheel is
# looked up (the per-platform wheels are 14-23 MB).
ORT_APPROX_BYTES = 23_000_000
ORT_APPROX_INSTALLED_BYTES = 65_000_000

LAMA_REVISION = "c3c0c9e468934d62e79c329e35d82dd09ff8c444"
LAMA_URL = (
    f"https://huggingface.co/Carve/LaMa-ONNX/resolve/{LAMA_REVISION}/lama_fp32.onnx"
)
LAMA_SHA256 = "1faef5301d78db7dda502fe59966957ec4b79dd64e16f03ed96913c7a4eb68d6"
LAMA_BYTES = 208_044_816
LAMA_FILENAME = "lama_fp32.onnx"
LAMA_INPUT = 512

_USER_AGENT = "FastStack (inpainting model download)"
_CHUNK = 1 << 20

ProgressFn = Callable[[int, int, str], None]  # (done_bytes, total_bytes, what)


class InpaintUnavailable(RuntimeError):
    """The runtime or model is not installed, or cannot run here."""


class DownloadCancelled(RuntimeError):
    """The owner cancelled the download."""


def ai_dir() -> Path:
    return get_app_data_dir() / "ai"


def model_path() -> Path:
    return ai_dir() / "models" / LAMA_FILENAME


def _python_tag() -> str:
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def _runtime_dir() -> Path:
    machine = platform.machine().lower() or "unknown"
    return ai_dir() / f"onnxruntime-{ORT_VERSION}-{_python_tag()}-{sys.platform}-{machine}"


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

_ort_module: Optional[ModuleType] = None
_ort_lock = threading.Lock()


def _system_onnxruntime_available() -> bool:
    """``onnxruntime`` importable without our download (a source install)."""
    try:
        return importlib.util.find_spec("onnxruntime") is not None
    except (ImportError, ValueError):
        return False


def runtime_installed() -> bool:
    if _ort_module is not None:
        return True
    if (_runtime_dir() / "onnxruntime" / "__init__.py").is_file():
        return True
    return _system_onnxruntime_available()


def model_installed() -> bool:
    path = model_path()
    try:
        return path.is_file() and path.stat().st_size == LAMA_BYTES
    except OSError:
        return False


@dataclasses.dataclass(frozen=True)
class InstallStatus:
    runtime_ready: bool
    model_ready: bool
    download_bytes: int
    disk_bytes: int
    folder: str

    @property
    def ready(self) -> bool:
        return self.runtime_ready and self.model_ready


def install_status() -> InstallStatus:
    runtime_ready = runtime_installed()
    model_ready = model_installed()
    download = (0 if runtime_ready else ORT_APPROX_BYTES) + (
        0 if model_ready else LAMA_BYTES
    )
    disk = (0 if runtime_ready else ORT_APPROX_INSTALLED_BYTES) + (
        0 if model_ready else LAMA_BYTES
    )
    return InstallStatus(runtime_ready, model_ready, download, disk, str(ai_dir()))


# ---------------------------------------------------------------------------
# Download and install
# ---------------------------------------------------------------------------


def _wheel_platform() -> Optional[tuple[str, str]]:
    """(platform-tag prefix, suffix) of wheels that run here, or None."""
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if sys.platform.startswith("win"):
        return ("win_arm64" if arm else "win_amd64", ".whl")
    if sys.platform == "darwin":
        return ("macosx_", "_arm64.whl" if arm else "_x86_64.whl")
    if sys.platform.startswith("linux"):
        return ("manylinux_", "_aarch64.whl" if arm else "_x86_64.whl")
    return None


def _pick_wheel(release_files: list) -> dict:
    plat = _wheel_platform()
    if plat is None:
        raise InpaintUnavailable(
            f"ONNX Runtime has no build for {sys.platform} {platform.machine()}"
        )
    prefix, suffix = plat
    abi = f"-{_python_tag()}-{_python_tag()}-"  # excludes free-threaded "cp314t"
    candidates = [
        entry
        for entry in release_files
        if entry.get("packagetype") == "bdist_wheel"
        and abi in entry.get("filename", "")
        and entry["filename"].split(abi, 1)[1].startswith(prefix)
        and entry["filename"].endswith(suffix)
    ]
    if not candidates:
        raise InpaintUnavailable(
            f"ONNX Runtime {ORT_VERSION} has no wheel for Python "
            f"{sys.version_info.major}.{sys.version_info.minor} on "
            f"{sys.platform} {platform.machine()}"
        )
    return candidates[0]


def _download(
    url: str,
    dest: Path,
    expected_sha256: str,
    expected_size: Optional[int],
    what: str,
    progress: Optional[ProgressFn],
    cancelled: Optional[Callable[[], bool]],
    done_before: int,
    total: int,
) -> int:
    """Stream *url* into *dest*, verifying size and SHA-256.  Returns bytes."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    digest = hashlib.sha256()
    written = 0
    with urllib.request.urlopen(request, timeout=60) as response, open(
        dest, "wb"
    ) as out:
        while True:
            if cancelled is not None and cancelled():
                raise DownloadCancelled()
            chunk = response.read(_CHUNK)
            if not chunk:
                break
            out.write(chunk)
            digest.update(chunk)
            written += len(chunk)
            if progress is not None:
                progress(done_before + written, total, what)
    if expected_size is not None and written != expected_size:
        raise OSError(f"{what}: got {written} bytes, expected {expected_size}")
    if digest.hexdigest() != expected_sha256.lower():
        raise OSError(f"{what}: checksum mismatch, download discarded")
    return written


def _install_runtime(
    progress: Optional[ProgressFn],
    cancelled: Optional[Callable[[], bool]],
    done_before: int,
    total: int,
) -> int:
    request = urllib.request.Request(ORT_PYPI_JSON, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        meta = json.loads(response.read().decode("utf-8"))
    wheel = _pick_wheel(meta.get("urls") or [])
    url = wheel["url"]
    if not url.startswith("https://files.pythonhosted.org/"):
        raise OSError(f"unexpected ONNX Runtime download location: {url}")
    target = _runtime_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".ort-", dir=target.parent) as tmp:
        tmp_path = Path(tmp)
        whl = tmp_path / wheel["filename"]
        got = _download(
            url,
            whl,
            wheel["digests"]["sha256"],
            int(wheel["size"]) if wheel.get("size") else None,
            "ONNX Runtime",
            progress,
            cancelled,
            done_before,
            total,
        )
        staging = tmp_path / "pkg"
        with zipfile.ZipFile(whl) as zf:
            for member in zf.namelist():
                resolved = (staging / member).resolve()
                if staging.resolve() not in resolved.parents and resolved != staging.resolve():
                    raise OSError(f"unsafe path in wheel: {member}")
            zf.extractall(staging)
        if target.exists():
            shutil.rmtree(target)
        staging.replace(target)
    log.info("Installed ONNX Runtime %s into %s", ORT_VERSION, target)
    return got


def _install_model(
    progress: Optional[ProgressFn],
    cancelled: Optional[Callable[[], bool]],
    done_before: int,
    total: int,
) -> int:
    dest = model_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".lama-", suffix=".part", dir=dest.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        got = _download(
            LAMA_URL,
            tmp,
            LAMA_SHA256,
            LAMA_BYTES,
            "LaMa model",
            progress,
            cancelled,
            done_before,
            total,
        )
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    log.info("Installed LaMa model into %s", dest)
    return got


# A download that is still being written touches its file at least every
# few seconds; anything left alone this long belongs to an interrupted one.
_LEFTOVER_AGE_S = 15 * 60


def _newest_mtime(path: Path) -> float:
    newest = path.lstat().st_mtime
    if path.is_dir() and not path.is_symlink():
        for root, dirs, files in os.walk(path):
            for name in dirs + files:
                try:
                    newest = max(newest, (Path(root) / name).lstat().st_mtime)
                except OSError:
                    pass
    return newest


def sweep_leftovers(max_age_s: float = _LEFTOVER_AGE_S) -> None:
    """Delete what an interrupted download left behind.

    The temporary model file and the wheel's staging folder are normally
    removed by the download itself, but not when FastStack exits in the
    middle of it (the worker is a daemon thread).  Only entries untouched for
    *max_age_s* go, so a download another FastStack window is running right
    now is left alone.
    """
    root = ai_dir()
    candidates = list(root.glob(".ort-*")) + list((root / "models").glob(".lama-*.part"))
    now = time.time()
    for path in candidates:
        try:
            if now - _newest_mtime(path) < max_age_s:
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            log.info("Removed an interrupted download: %s", path)
        except OSError as exc:
            log.debug("Could not remove %s: %s", path, exc)


def install(
    progress: Optional[ProgressFn] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> None:
    """Download and install whatever is missing.  Runs on a worker thread.

    Raises DownloadCancelled, InpaintUnavailable or OSError (network, disk,
    checksum).  A failed or cancelled download leaves nothing half-installed;
    one cut short by quitting is cleaned up by ``sweep_leftovers``.
    """
    sweep_leftovers()
    status = install_status()
    total = status.download_bytes
    done = 0
    if not status.runtime_ready:
        done += _install_runtime(progress, cancelled, done, total)
        # The approximate size was only for the prompt; keep the bar honest.
        total = max(total, done + (0 if status.model_ready else LAMA_BYTES))
    if not status.model_ready:
        done += _install_model(progress, cancelled, done, total)
    if progress is not None:
        progress(done, max(total, done), "done")


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


def _load_onnxruntime() -> ModuleType:
    global _ort_module
    with _ort_lock:
        if _ort_module is not None:
            return _ort_module
        local = _runtime_dir()
        if (local / "onnxruntime" / "__init__.py").is_file():
            if str(local) not in sys.path:
                sys.path.insert(0, str(local))
        try:
            _ort_module = importlib.import_module("onnxruntime")
        except Exception as exc:  # broken wheel, missing system library
            raise InpaintUnavailable(f"ONNX Runtime could not be loaded: {exc}") from exc
        log.info(
            "ONNX Runtime %s loaded from %s",
            getattr(_ort_module, "__version__", "?"),
            getattr(_ort_module, "__file__", "?"),
        )
        return _ort_module


class LamaInpainter:
    """Loads the LaMa model once; ``inpaint`` is safe to call from one worker
    thread at a time."""

    def __init__(self) -> None:
        if not model_installed():
            raise InpaintUnavailable("the LaMa model is not installed")
        ort = _load_onnxruntime()
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, (os.cpu_count() or 2) - 1)
        self._session = ort.InferenceSession(
            str(model_path()), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._lock = threading.Lock()

    def _run(self, tile: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """One 512x512 inference: float32 RGB 0..1 in and out."""
        with self._lock:
            result = self._session.run(
                None,
                {
                    "image": np.ascontiguousarray(tile.transpose(2, 0, 1)[None]),
                    "mask": np.ascontiguousarray(mask[None, None]),
                },
            )[0][0]
        # This export returns 0..255 whatever the content; guessing the range
        # from the values would take a near-black tile for 0..1 data.
        return result.transpose(1, 2, 0).astype(np.float32) * np.float32(1.0 / 255.0)

    def inpaint(
        self,
        image: np.ndarray,
        hole: np.ndarray,
        *,
        scale: float = 0.5,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> np.ndarray:
        """Fill *hole* (bool HxW) in *image* (float32 RGB 0..1, HxWx3).

        Returns a float32 RGB array the size of *image*; pixels outside the
        hole are returned unchanged.  The hole is filled at *scale* of full
        resolution (0.5 suits blurred backgrounds: LaMa sees enough context
        and the result carries all the detail such a background has), in
        overlapping 512x512 tiles of ``512 / scale`` full-res pixels that are
        blended where they overlap.
        """
        from faststack.imaging.optional_deps import get_cv2

        cv2 = get_cv2()
        if cv2 is None:
            raise InpaintUnavailable("OpenCV is not available")
        hole = np.asarray(hole, dtype=bool)
        out = np.array(image, dtype=np.float32, copy=True)
        if not hole.any():
            return out
        h, w = hole.shape
        side = int(min(max(h, w), round(LAMA_INPUT / max(scale, 1e-3))))
        side = max(side, min(LAMA_INPUT, max(h, w)))
        ys, xs = np.nonzero(hole)
        stride = max(1, int(side * 0.7))

        def starts(lo: int, hi: int, extent: int) -> list:
            """Tile origins along one axis covering the hole's [lo, hi), each
            tile inside the image and with some context beyond the hole."""
            span = min(side, extent)
            last = extent - span
            margin = side // 8
            if hi - lo + 2 * margin <= span:  # one tile, centred on the hole
                return [int(np.clip((lo + hi - span) // 2, 0, last))]
            origins, pos = [], lo - margin
            while True:
                origin = int(np.clip(pos, 0, last))
                if not origins or origin != origins[-1]:
                    origins.append(origin)
                if origin + span >= min(hi + margin, extent):
                    return origins
                pos += stride

        acc = np.zeros((h, w, 3), np.float32)
        wsum = np.zeros((h, w), np.float32)
        size = (LAMA_INPUT, LAMA_INPUT)
        for ty in starts(int(ys.min()), int(ys.max()) + 1, h):
            for tx in starts(int(xs.min()), int(xs.max()) + 1, w):
                th, tw = min(side, h), min(side, w)
                hcrop = hole[ty : ty + th, tx : tx + tw]
                if not hcrop.any():
                    continue
                if cancelled is not None and cancelled():
                    raise DownloadCancelled()
                crop = np.clip(image[ty : ty + th, tx : tx + tw], 0.0, 1.0)
                small = cv2.resize(crop, size, interpolation=cv2.INTER_AREA)
                hsmall = cv2.resize(hcrop.astype(np.float32), size, interpolation=cv2.INTER_AREA)
                result = self._run(small.astype(np.float32), (hsmall > 0.01).astype(np.float32))
                filled = cv2.resize(result, (tw, th), interpolation=cv2.INTER_CUBIC)
                # Weight falls off towards the tile's edges, where LaMa saw
                # the least context.
                ry = np.minimum(np.arange(th) + 1, th - np.arange(th)).astype(np.float32)
                rx = np.minimum(np.arange(tw) + 1, tw - np.arange(tw)).astype(np.float32)
                wt = np.minimum(ry[:, None], rx[None, :]) / max(1.0, side * 0.15)
                wt = np.clip(wt, 1e-3, 1.0) * hcrop
                acc[ty : ty + th, tx : tx + tw] += filled * wt[..., None]
                wsum[ty : ty + th, tx : tx + tw] += wt
        done = hole & (wsum > 0)
        out[done] = np.clip(acc[done] / wsum[done][:, None], 0.0, 1.0)
        return out


_inpainter: Optional[LamaInpainter] = None
_inpainter_lock = threading.Lock()


def get_inpainter() -> LamaInpainter:
    """The shared inpainter (loads the model on first call, several seconds)."""
    global _inpainter
    with _inpainter_lock:
        if _inpainter is None:
            _inpainter = LamaInpainter()
        return _inpainter
