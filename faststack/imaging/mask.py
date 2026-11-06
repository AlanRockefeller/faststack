# faststack/imaging/mask.py
"""Brush-stroke masks for local (dodge & burn) adjustments.

Provides:
- MaskStroke – a single brush stroke in image-normalised coordinates
- MaskData   – the strokes that make up one local-adjustment layer's mask

The layer's *recipe* (which sliders it moves, and by how much) lives in the
edit dict under ``local_layers``; see ``faststack.imaging.local_adjust``.
MaskData is keyed by the layer id in ``ImageEditor._mask_assets``.
"""

import dataclasses
import itertools
import logging
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

STROKE_PAINT = "paint"
STROKE_ERASE = "erase"

_mask_uids = itertools.count(1)


# ---------------------------------------------------------------------------
# Stroke
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class MaskStroke:
    """A single brush stroke stored in image-normalised coordinates [0, 1].

    Coordinates are relative to the *oriented base image* — i.e. the image
    before 90-degree rotation, straighten and crop.  This keeps strokes
    stable when the user adjusts geometry later.

    ``radius`` is a fraction of the oriented base image's diagonal, so a
    stroke covers the same part of the photo at every render resolution and
    crop.  ``hardness`` is the fraction of the radius painted at full
    strength before the edge falls off (1.0 = hard edge).

    A stroke is one coat: scrubbing back and forth within a stroke does not
    build up.  ``paint`` strokes add one coat of the layer's recipe; ``erase``
    strokes scale the layer's accumulated density towards zero.
    """

    points: List[Tuple[float, float]]  # (x_norm, y_norm) sequence
    radius: float
    hardness: float = 0.5
    mode: str = STROKE_PAINT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "points": [list(p) for p in self.points],
            "radius": self.radius,
            "hardness": self.hardness,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MaskStroke":
        mode = d.get("mode", STROKE_PAINT)
        if mode not in (STROKE_PAINT, STROKE_ERASE):
            raise ValueError(f"Unknown stroke mode {mode!r}")
        return cls(
            points=[(float(p[0]), float(p[1])) for p in d["points"]],
            radius=float(d["radius"]),
            hardness=min(1.0, max(0.0, float(d.get("hardness", 0.5)))),
            mode=mode,
        )


# ---------------------------------------------------------------------------
# Mask asset
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class MaskData:
    """The committed strokes of one layer, plus the stroke being painted.

    ``revision`` bumps on every committed change.  ``uid`` is a per-instance
    id (never serialised) that, with the stroke objects themselves, keys the
    raster cache.
    ``live_stroke`` is the in-progress stroke while the mouse button is down.
    It is never serialised; the GUI thread replaces it with a new object on
    every mouse move (never mutates it), so a render worker that read the
    reference once sees a consistent stroke.
    """

    strokes: List[MaskStroke] = dataclasses.field(default_factory=list)
    revision: int = 0
    live_stroke: Optional[MaskStroke] = None
    uid: int = dataclasses.field(default_factory=lambda: next(_mask_uids))

    # ---- mutation helpers (all bump revision) ----

    def add_stroke(self, stroke: MaskStroke) -> None:
        # Rebind instead of append so a worker iterating the old list is safe.
        self.strokes = [*self.strokes, stroke]
        self.revision += 1

    def undo_last_stroke(self) -> Optional[MaskStroke]:
        if self.strokes:
            removed = self.strokes[-1]
            self.strokes = self.strokes[:-1]
            self.revision += 1
            return removed
        return None

    def clear_strokes(self) -> None:
        self.strokes = []
        self.revision += 1

    def has_strokes(self) -> bool:
        return len(self.strokes) > 0

    # ---- serialisation ----

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strokes": [s.to_dict() for s in self.strokes],
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MaskData":
        strokes = [MaskStroke.from_dict(s) for s in d.get("strokes", [])]
        return cls(strokes=strokes, revision=int(d.get("revision", len(strokes))))
