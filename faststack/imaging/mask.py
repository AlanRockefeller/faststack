# faststack/imaging/mask.py
"""Reusable soft-mask model for local adjustments.

Layer 1 of the mask subsystem.  Provides:
- MaskStroke  – a single brush stroke in image-normalised coordinates
- MaskData    – a generic, tool-agnostic mask asset (strokes + overlay metadata)
- DarkenSettings – tool-specific parameters for the background darkening tool

MaskData is intentionally free of tool-specific logic so that future local
adjustment tools (selective exposure, colour, sharpening, dust cleanup …)
can share the same mask representation.
"""

import dataclasses
import logging
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stroke
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class MaskStroke:
    """A single brush stroke stored in image-normalised coordinates [0, 1].

    Coordinates are relative to the *oriented base image* — i.e. the image
    after 90-degree rotation but **before** straighten and crop.  This keeps
    strokes stable when the user adjusts straighten/crop later.
    """

    points: List[Tuple[float, float]]  # (x_norm, y_norm) sequence
    radius: float  # brush radius in normalised coords
    stroke_type: str  # "add" (background hint) or "protect" (subject hint)
    pressure: Optional[List[float]] = None  # optional per-point pressure

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "points": self.points,
            "radius": self.radius,
            "stroke_type": self.stroke_type,
        }
        if self.pressure is not None:
            d["pressure"] = self.pressure
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MaskStroke":
        return cls(
            points=[tuple(p) for p in d["points"]],
            radius=d["radius"],
            stroke_type=d["stroke_type"],
            pressure=d.get("pressure"),
        )


# ---------------------------------------------------------------------------
# Generic mask asset (tool-agnostic)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class MaskData:
    """Reusable mask asset — strokes, revision tracking, overlay metadata.

    This class owns *no* tool-specific parameters and *no* raster caches.
    Raster products are disposable and live on the mask engine / editor.
    """

    strokes: List[MaskStroke] = dataclasses.field(default_factory=list)
    revision: int = 0

    # Overlay display metadata (generic — any tool can use these)
    overlay_color: Tuple[int, int, int] = (80, 120, 255)  # default blue
    overlay_opacity: float = 0.4

    # ---- mutation helpers (all bump revision) ----

    def add_stroke(self, stroke: MaskStroke) -> None:
        self.strokes.append(stroke)
        self.revision += 1

    def undo_last_stroke(self) -> Optional[MaskStroke]:
        if self.strokes:
            removed = self.strokes.pop()
            self.revision += 1
            return removed
        return None

    def clear_strokes(self) -> None:
        self.strokes.clear()
        self.revision += 1

    def has_strokes(self) -> bool:
        return len(self.strokes) > 0

    # ---- serialisation ----

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strokes": [s.to_dict() for s in self.strokes],
            "revision": self.revision,
            "overlay_color": list(self.overlay_color),
            "overlay_opacity": self.overlay_opacity,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MaskData":
        strokes = [MaskStroke.from_dict(s) for s in d.get("strokes", [])]
        return cls(
            strokes=strokes,
            revision=d.get("revision", len(strokes)),
            overlay_color=tuple(d.get("overlay_color", (80, 120, 255))),
            overlay_opacity=d.get("overlay_opacity", 0.4),
        )


# ---------------------------------------------------------------------------
# Background darkening tool settings (tool-specific)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class DarkenSettings:
    """Parameters for the background darkening tool.

    References a MaskData asset by *mask_id* (a key into
    ``ImageEditor._mask_assets``).  This keeps the generic mask separate
    from the tool-specific knobs.
    """

    mask_id: str = "darken"
    enabled: bool = False

    # Darkening intensity
    darken_amount: float = 0.5  # 0–1

    # Mask refinement
    edge_protection: float = 0.5  # 0–1
    subject_protection: float = 0.5  # 0–1
    feather: float = 0.5  # 0–1
    dark_range: float = 0.5  # 0–1
    neutrality_sensitivity: float = 0.5  # 0–1
    expand_contract: float = 0.0  # -1 to +1
    auto_from_edges: float = 0.0  # 0–1

    # Mode
    mode: str = "assisted"
    # Valid: "paint_only", "assisted", "strong_subject", "border_auto"

    # Brush (stored on settings so each tool can have its own default)
    brush_radius: float = 0.03  # normalised

    def params_tuple(self) -> tuple:
        """Frozen tuple of all scalar params — used as a cache key."""
        return (
            self.darken_amount,
            self.edge_protection,
            self.subject_protection,
            self.feather,
            self.dark_range,
            self.neutrality_sensitivity,
            self.expand_contract,
            self.auto_from_edges,
            self.mode,
        )

    # ---- serialisation ----

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mask_id": self.mask_id,
            "enabled": self.enabled,
            "darken_amount": self.darken_amount,
            "edge_protection": self.edge_protection,
            "subject_protection": self.subject_protection,
            "feather": self.feather,
            "dark_range": self.dark_range,
            "neutrality_sensitivity": self.neutrality_sensitivity,
            "expand_contract": self.expand_contract,
            "auto_from_edges": self.auto_from_edges,
            "mode": self.mode,
            "brush_radius": self.brush_radius,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DarkenSettings":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
