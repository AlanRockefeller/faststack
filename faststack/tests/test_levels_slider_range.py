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


def _curve_engine(script: str = "SliderCurve.js"):
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtQml import QJSEngine

    # QJSEngine aborts without an application object.
    if QCoreApplication.instance() is None:
        _curve_engine.app = QCoreApplication([])
    engine = QJSEngine()
    source = (QML_DIR / script).read_text(encoding="utf-8")
    result = engine.evaluate(source.replace(".pragma library", "", 1))
    assert not result.isError(), result.toString()
    return engine


def _call(engine, fn, *args):
    result = engine.globalObject().property(fn).call(list(args))
    assert not result.isError(), result.toString()
    if result.isBool():
        return result.toBool()
    if result.isString():
        return result.toString()
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


# ---------------------------------------------------------------------------
# SliderInput.js: clicks, drags and arrow keys on the editors' sliders
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("qml", ["CompactEditorWindow.qml", "ImageEditorDialog.qml"])
def test_editors_use_slider_input(qml):
    text = (QML_DIR / qml).read_text(encoding="utf-8")
    assert 'import "SliderInput.js" as SliderInput' in text
    assert (
        "SliderInput.pressMove(slider._pressValue, slider._grabbed, "
        "slider._grabValue, value," in text
    )
    # A release sends the handle's value only after a drag.
    assert re.search(r"\} else if \(dragged\) \{", text)


def _px(px, handle=10, available=240):
    """``px`` pixels of pointer travel in handle-position units."""
    return px / (available - handle) * 200


def _press(eng, press_value, *values, handle=10, available=240, unmoved=False):
    """Feed one press's moved values through pressMove: True once it drags.

    ``unmoved``: Qt emitted no moved for the press itself (as reported for
    6.5, on a press on the handle's centre), so the editors' grabIfUnmoved
    grabbed the handle where it was before the first of *values*.
    """
    grabbed, grab = (True, press_value) if unmoved else (False, 0.0)
    geometry = (handle, available, -100, 100)
    for value in values:
        step = _call(eng, "pressMove", press_value, grabbed, grab, value, *geometry)
        if step == "grab":
            grabbed, grab = True, value
        elif step == "drag":
            return True
        else:
            assert step == ""
    return False


def test_click_on_the_handle_is_not_a_drag():
    eng = _curve_engine("SliderInput.js")
    # Qt puts the handle at the press point: anywhere on a 10 px handle.
    for px in (0, 2, 5, -5):
        assert not _press(eng, -100, -100 + _px(px))  # pinned at the end
        assert not _press(eng, 20, 20 + _px(px))
    # A pointer that wobbles by a pixel or two is still a click.
    assert not _press(eng, 20, 20 + _px(3), 20 + _px(4), 20 + _px(2))


def test_press_beside_the_handle_jumps_there():
    eng = _curve_engine("SliderInput.js")
    assert _press(eng, -100, 85)
    assert _press(eng, 20, 20 + _px(8))


@pytest.mark.parametrize(
    "grab_px, moves",
    [(0, [1, 2, 3]), (0, [-1, -2, -3]), (5, [-2, -4, -8]), (-5, [2, 4, 8])],
)
def test_short_drag_from_the_handle_is_a_drag(grab_px, moves):
    # A few pixels from where the handle was grabbed, wherever on it that was.
    eng = _curve_engine("SliderInput.js")
    grab = 20 + _px(grab_px)
    assert _press(eng, 20, grab, *(grab + _px(m) for m in moves))


@pytest.mark.parametrize("move_px", [3, -3, 6])
def test_short_drag_from_an_unmoved_press_is_a_drag(move_px):
    # Where Qt emits no moved for a press on the handle's centre, the first
    # moved is the pointer's own travel. Taken as the grab point, a quick
    # 3 px drag was swallowed; the handle is grabbed at the press instead.
    eng = _curve_engine("SliderInput.js")
    assert _press(eng, 20, 20 + _px(move_px), unmoved=True)
    assert not _press(eng, 20, unmoved=True)  # a click: no moved at all
    assert not _press(eng, 20, 20 + _px(1), 20 - _px(1), unmoved=True)  # a wobble


@pytest.mark.parametrize("qml", ["CompactEditorWindow.qml", "ImageEditorDialog.qml"])
def test_editors_grab_a_press_no_moved_has_classified(qml):
    text = (QML_DIR / qml).read_text(encoding="utf-8")
    press = text[text.index("onPressedChanged: {", text.index("property bool _dragging")) :]
    press = press[: press.index("} else {")]
    assert "Qt.callLater(slider.grabIfUnmoved)" in press
    grab = text[text.index("function grabIfUnmoved() {") :]
    grab = grab[: grab.index("\n                }\n")]
    assert "if (slider.pressed && !slider._grabbed && !slider._dragging) {" in grab
    assert "slider._grabbed = true" in grab
    assert "slider._grabValue = slider.value" in grab


def test_press_on_handle_allowance_never_below_three_pixels():
    eng = _curve_engine("SliderInput.js")
    per_px = _call(eng, "unitsPerPixel", 0, 200, -100, 100)
    assert per_px == pytest.approx(1.0)
    assert _call(eng, "pressOnHandle", 0, 3, 0, per_px)
    assert not _call(eng, "pressOnHandle", 0, 3.5, 0, per_px)
    # A one-sided slider (0..100) has half the units per pixel.
    assert _call(eng, "unitsPerPixel", 10, 240, 0, 100) == pytest.approx(
        _call(eng, "unitsPerPixel", 10, 240, -100, 100) / 2
    )


def test_key_hold_ends_when_the_edit_changes_elsewhere():
    eng = _curve_engine("SliderInput.js")
    current = lambda sent, now: _call(eng, "keyHoldCurrent", sent, now)  # noqa: E731
    assert current(41, 41)
    assert current(41, 41.0001)  # rounding on the way through the backend
    assert not current(41, 180)  # auto levels set Whites meanwhile
    assert not current(41, 40)  # one step from elsewhere


@pytest.mark.parametrize(
    "base,step,expected",
    [
        (41, 1, 42),
        (300, 1, 300),  # at the end: stays
        (-400, 1, -399),  # past the end: steps back toward the range
        (-400, -1, -400),  # ...but never further out
        (-400, 200, -200),
    ],
)
def test_key_step_stays_in_range_without_jumping(base, step, expected):
    eng = _curve_engine("SliderInput.js")
    assert _call(eng, "keyStep", base, step, -300, 300) == expected
