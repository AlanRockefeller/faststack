"""Centralized optional dependencies to avoid circular imports and heavy initialization."""

try:
    import cv2

    HAS_OPENCV = True
except ImportError:
    cv2 = None
    HAS_OPENCV = False
