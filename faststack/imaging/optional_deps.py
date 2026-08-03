"""Centralized, lazy access to optional dependencies."""

import importlib
import logging
import threading
from types import ModuleType
from typing import Optional, cast

log = logging.getLogger(__name__)

_UNLOADED = object()
_cv2: ModuleType | None | object = _UNLOADED
_cv2_lock = threading.Lock()


def get_cv2() -> Optional[ModuleType]:
    """Return OpenCV, importing it only when an OpenCV operation is needed."""
    global _cv2

    if _cv2 is _UNLOADED:
        with _cv2_lock:
            if _cv2 is _UNLOADED:
                try:
                    _cv2 = importlib.import_module("cv2")
                except ImportError:
                    _cv2 = None
                    log.debug("OpenCV is not available", exc_info=True)
    return cast(Optional[ModuleType], _cv2)


def has_opencv() -> bool:
    """Return whether OpenCV can be imported, loading it on this first check."""
    return get_cv2() is not None
