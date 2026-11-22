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


def _curve_engine():
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtQml import QJSEngine

    # QJSEngine aborts without an application object.
    if QCoreApplication.instance() is None:
        _curve_engine.app = QCoreApplication([])
    engine = QJSEngine()
    source = (QML_DIR / "SliderCurve.js").read_text(encoding="utf-8")
    result = engine.evaluate(source.replace(".pragma library", "", 1))
    assert not result.isError(), result.toString()
    return engine


def _call(engine, fn, *args):
    result = engine.globalObject().property(fn).call(list(args))
    assert not result.isError(), result.toString()
    return result.toNumber()


@pytest.mark.parametrize("lo,hi", [(-100, 300), (-300, 100), (-100, 100)])
def test_slider_curve_centres_zero_and_reaches_extents(lo, hi):
    eng = _curve_engine()
    val = lambda p: _call(eng, "valueForPosition", p, lo, hi)  # noqa: E731
    pos = lambda v: _call(eng, "positionForValue", v, lo, hi)  # noqa: E731
    assert val(0) == 0 and pos(0) == 0
    assert val(100) == pytest.approx(hi)
    assert val(-100) == pytest.approx(lo)
    # One to one near the middle: everyday edits feel as before.
    for p in (-50, -20, 1, 37, 50):
        assert val(p) == pytest.approx(p)
    # Monotonic and invertible across the whole travel.
    prev = None
    for p in range(-100, 101):
        v = val(p)
        assert pos(v) == pytest.approx(p, abs=1e-6)
        if prev is not None:
            assert v > prev
        prev = v


def test_slider_curve_puts_plus_100_at_three_quarters():
    eng = _curve_engine()
    assert _call(eng, "positionForValue", 100, -100, 300) == pytest.approx(75)
    assert _call(eng, "positionForValue", -100, -300, 100) == pytest.approx(-75)


@pytest.mark.parametrize("qml", ["CompactEditorWindow.qml", "ImageEditorDialog.qml"])
def test_slider_handle_runs_minus_to_plus_100(qml):
    text = (QML_DIR / qml).read_text(encoding="utf-8")
    assert 'import "SliderCurve.js" as SliderCurve' in text
    # Centred travel for bipolar controls; a one-sided control (Vignette)
    # keeps 0..100 so it can never send a negative value.
    assert re.search(r"from: sliderRow\.min(Val)? < 0 \? -100 : 0", text)
    assert re.search(r"to: sliderRow\.max(Val)? > 0 \? 100 : 0", text)
