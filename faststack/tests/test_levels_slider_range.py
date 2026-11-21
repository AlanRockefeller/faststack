"""The editors' Whites/Blacks sliders must reach what auto levels can set.

The sliders used to stop at +/-100, while auto levels (L) routinely set
Whites well past that: the readout pinned at +100, and nudging the slider
off and back silently threw most of the edit away.
"""

import re
from pathlib import Path

import pytest

from faststack.app import _AUTO_LEVELS_MAX_STRETCH

QML_DIR = Path(__file__).resolve().parents[1] / "qml"
# Slider units per edit unit, and the per-key edit scale (sliderEditScale).
_EDIT_SCALE = {"whites": 2.0, "blacks": 1.0}


def _slider_range(qml_name: str, key: str) -> tuple[float, float]:
    text = (QML_DIR / qml_name).read_text(encoding="utf-8")
    match = re.search(
        r'ListElement \{[^}]*key: "%s";[^}]*min: (-?\d+); max: (-?\d+)' % key, text
    )
    assert match, f"{key} slider not found in {qml_name}"
    return float(match.group(1)), float(match.group(2))


def _compact_function_range(key: str) -> tuple[float, float]:
    text = (QML_DIR / "CompactEditorWindow.qml").read_text(encoding="utf-8")
    match = re.search(
        r'if \(key === "%s"\) return \{ min: (-?\d+), max: (-?\d+) \}' % key, text
    )
    assert match, f"sliderRange({key}) not found"
    return float(match.group(1)), float(match.group(2))


@pytest.mark.parametrize("qml", ["CompactEditorWindow.qml", "ImageEditorDialog.qml"])
def test_whites_slider_covers_auto_levels(qml):
    # Largest auto whites: black point at 0, white point at the stretch cap.
    max_whites = (255.0 - 255.0 / _AUTO_LEVELS_MAX_STRETCH) / 40.0
    _lo, hi = _slider_range(qml, "whites")
    assert hi >= max_whites / _EDIT_SCALE["whites"] * 100
    # ...but stays below the point where the white point reaches 0.
    assert hi / 100 * _EDIT_SCALE["whites"] * 0.15 < 1.0


@pytest.mark.parametrize("qml", ["CompactEditorWindow.qml", "ImageEditorDialog.qml"])
def test_blacks_slider_extends_below_minus_100(qml):
    lo, _hi = _slider_range(qml, "blacks")
    assert lo <= -300


@pytest.mark.parametrize("key", ["whites", "blacks"])
def test_compact_keyboard_range_matches_slider(key):
    assert _compact_function_range(key) == _slider_range("CompactEditorWindow.qml", key)
