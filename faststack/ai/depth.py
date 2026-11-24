# faststack/ai/depth.py
"""Depth Anything V2 (Small) monocular depth estimation, downloaded on first use.

Like the inpainting model (``faststack.ai.inpaint``, which this shares the
ONNX Runtime download and install code with), nothing here is bundled with
FastStack.  The first time a feature needs a depth map, the owner is asked to
download:

* **ONNX Runtime**, unless halo removal already fetched it.
* **Depth Anything V2 Small** (Yang et al., "Depth Anything V2", 2024;
  Apache-2.0 licence — only the Small model is, the larger ones are
  non-commercial), the ONNX export published as
  ``onnx-community/depth-anything-v2-small`` on Hugging Face, pinned to one
  revision and verified against its SHA-256.

The model estimates *relative inverse depth* from a single photo: larger
values are nearer the camera, and the scale is arbitrary.  It runs on a copy
of the photo about 518 pixels long (a second or so on the CPU);
``faststack.imaging.depth_select`` normalises the result and refines its
edges against the photo's colours.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

import numpy as np

from faststack.ai import inpaint as _ai

log = logging.getLogger(__name__)

DEPTH_REVISION = "4472b7362082ad9968fee890ca0f1e5aca36b93d"
DEPTH_MODEL = _ai.register_model(
    _ai.ModelSpec(
        label="Depth Anything model",
        url=(
            "https://huggingface.co/onnx-community/depth-anything-v2-small/"
            f"resolve/{DEPTH_REVISION}/onnx/model.onnx"
        ),
        sha256="afb6a5c28f3b6bf1618c6e43f02073ef9dfdc70e937502d51603e57b0a1df10c",
        size=99_060_839,
        filename="depth_anything_v2_small_fp32.onnx",
        part_prefix=".depth-",
    )
)

# Long edge the photo is scaled to for the model, and the patch size its
# sides must be a multiple of.
DEPTH_INPUT = 518
_PATCH = 14
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

DepthUnavailable = _ai.InpaintUnavailable
DownloadCancelled = _ai.DownloadCancelled


def install_status() -> _ai.InstallStatus:
    return _ai.install_status(DEPTH_MODEL)


def install(progress=None, cancelled=None) -> None:
    """Download whatever depth estimation is missing (worker thread)."""
    _ai.install(progress, cancelled, DEPTH_MODEL)


def model_input_size(height: int, width: int) -> tuple[int, int]:
    """(height, width) the model is run at for a photo of this shape."""
    scale = DEPTH_INPUT / max(height, width, 1)
    return (
        max(_PATCH, int(round(height * scale / _PATCH)) * _PATCH),
        max(_PATCH, int(round(width * scale / _PATCH)) * _PATCH),
    )


class DepthEstimator:
    """Loads the model once; ``estimate`` may be called from any thread."""

    def __init__(self) -> None:
        if not DEPTH_MODEL.installed():
            raise DepthUnavailable("the depth model is not installed")
        ort = _ai.load_onnxruntime()
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, (os.cpu_count() or 2) - 1)
        try:
            self._session = ort.InferenceSession(
                str(DEPTH_MODEL.path),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
        except Exception as exc:  # corrupt file, unsupported opset
            raise DepthUnavailable(f"the depth model could not be loaded: {exc}") from exc
        self._input = self._session.get_inputs()[0].name
        self._lock = threading.Lock()

    def estimate(self, image: np.ndarray) -> np.ndarray:
        """Relative inverse depth of *image* (float32 sRGB 0..1, HxWx3).

        Returns a float32 2-D array at the model's resolution (same aspect
        ratio as *image*, about ``DEPTH_INPUT`` long); larger is nearer.
        """
        from faststack.imaging.optional_deps import get_cv2

        cv2 = get_cv2()
        if cv2 is None:
            raise DepthUnavailable("OpenCV is not available")
        h, w = image.shape[:2]
        mh, mw = model_input_size(h, w)
        small = cv2.resize(
            np.ascontiguousarray(image[..., :3], dtype=np.float32),
            (mw, mh),
            interpolation=cv2.INTER_AREA if max(h, w) > max(mh, mw) else cv2.INTER_CUBIC,
        )
        tensor = (np.clip(small, 0.0, 1.0) - _MEAN) / _STD
        tensor = np.ascontiguousarray(tensor.transpose(2, 0, 1)[None], dtype=np.float32)
        with self._lock:
            out = self._session.run(None, {self._input: tensor})[0]
        return np.asarray(out, dtype=np.float32).reshape(out.shape[-2], out.shape[-1])


_estimator: Optional[DepthEstimator] = None
_estimator_lock = threading.Lock()


def get_estimator() -> DepthEstimator:
    """The shared estimator (loads the model on first call, under a second)."""
    global _estimator
    with _estimator_lock:
        if _estimator is None:
            _estimator = DepthEstimator()
        return _estimator
