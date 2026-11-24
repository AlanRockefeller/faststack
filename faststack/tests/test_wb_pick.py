"""White balance picker: click a white or grey spot to set white balance.

The solve (faststack.imaging.wb_pick) is checked against the editor's own
white balance gains, and the whole pick (click on the displayed photo ->
spot on the unedited image -> Temp / Tint -> render) on a rotated,
straightened and cropped photo, where the spot must come out neutral.
"""

from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from faststack.imaging.editor import (
    ImageEditor,
    _normalized_wb_gains,
    geometry_normalized_affine,
)
from faststack.imaging.math_utils import _linear_to_srgb
from faststack.imaging.wb_pick import (
    PICK_CLIPPED,
    PICK_DARK,
    PICK_OUTSIDE,
    PICK_RADIUS_MAX,
    PICK_RADIUS_MIN,
    read_disc,
    solve_white_balance,
)


def _srgb_patch(linear_rgb, n=64):
    """``n`` sRGB pixels of one linear colour."""
    srgb = _linear_to_srgb(np.asarray(linear_rgb, dtype=np.float64))
    return np.tile(srgb.astype(np.float32), (n, 1))


def _corrected(linear_rgb, pick):
    gains = np.asarray(_normalized_wb_gains(pick.by * 0.5, pick.mg * 0.5))
    return np.asarray(linear_rgb) * gains


# ---------------------------------------------------------------------------
# Solve
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "linear",
    [
        (0.20, 0.18, 0.12),  # warm (tungsten-ish)
        (0.10, 0.15, 0.22),  # cool (shade)
        (0.30, 0.22, 0.31),  # magenta
        (0.12, 0.20, 0.13),  # green (fluorescent)
        (0.60, 0.55, 0.40),  # bright warm white
    ],
)
def test_solve_makes_the_spot_neutral_through_the_editor_gains(linear):
    pick = solve_white_balance(_srgb_patch(linear))
    assert pick.error is None and not pick.clamped
    out = _corrected(pick.mean_linear, pick)
    assert np.ptp(out) < 1e-6 * out.max()


def test_neutral_spot_gives_zero():
    pick = solve_white_balance(_srgb_patch((0.3, 0.3, 0.3)))
    assert pick.error is None
    assert abs(pick.by) < 1e-6 and abs(pick.mg) < 1e-6


def test_blown_out_spot_is_refused():
    blown = np.tile(np.array([1.0, 0.99, 0.95], np.float32), (100, 1))
    pick = solve_white_balance(blown)
    assert pick.error == PICK_CLIPPED


def test_mostly_blown_spot_is_refused_but_a_few_clipped_pixels_are_ignored():
    grey = _srgb_patch((0.2, 0.18, 0.12), n=100)
    blown = np.ones((100, 3), np.float32)
    mostly = np.concatenate([grey[:20], blown[:80]])
    assert solve_white_balance(mostly).error == PICK_CLIPPED

    some = np.concatenate([grey[:70], blown[:30]])
    pick = solve_white_balance(some)
    clean = solve_white_balance(grey)
    assert pick.error is None
    assert pick.by == pytest.approx(clean.by) and pick.mg == pytest.approx(clean.mg)


def test_black_spot_is_refused():
    pick = solve_white_balance(np.full((50, 3), 1.0 / 255.0, np.float32))
    assert pick.error == PICK_DARK


def test_empty_spot_is_outside():
    assert solve_white_balance(np.zeros((0, 3), np.float32)).error == PICK_OUTSIDE


def test_cast_stronger_than_the_sliders_clamps_and_says_so():
    pick = solve_white_balance(_srgb_patch((0.02, 0.2, 0.5)))
    assert pick.error is None and pick.clamped
    assert pick.by == 1.0
    assert -1.0 <= pick.mg <= 1.0
    # Still as close as Temp allows: red and blue nearer each other than before.
    out = _corrected(pick.mean_linear, pick)
    assert out[2] / out[0] < 0.5 / 0.02


# ---------------------------------------------------------------------------
# Reading the spot
# ---------------------------------------------------------------------------


def _gradient(h=80, w=120):
    yy, xx = np.mgrid[0:h, 0:w]
    arr = np.stack([xx / w, yy / h, np.full((h, w), 0.5)], axis=2)
    return (arr * 255).round().astype(np.uint8)


def test_read_disc_same_pixels_from_pil_and_float():
    u8 = _gradient()
    from_pil = read_disc(Image.fromarray(u8), 0.4, 0.6, 0.05)
    from_float = read_disc(u8.astype(np.float32) / 255.0, 0.4, 0.6, 0.05)
    np.testing.assert_allclose(from_pil, from_float, atol=1e-7)


def test_read_disc_reads_a_disc_of_the_right_size():
    arr = np.zeros((200, 300, 3), np.float32)
    radius = 0.05
    r_px = radius * np.hypot(200, 300)
    pixels = read_disc(arr, 0.5, 0.5, radius)
    assert len(pixels) == pytest.approx(np.pi * r_px**2, rel=0.05)


def test_read_disc_is_cut_off_at_the_edge_and_none_off_the_image():
    arr = np.zeros((100, 100, 3), np.float32)
    full = len(read_disc(arr, 0.5, 0.5, 0.1))
    corner = len(read_disc(arr, 0.0, 0.0, 0.1))
    assert corner == pytest.approx(full / 4, rel=0.1)
    assert read_disc(arr, -0.01, 0.5, 0.1) is None
    assert read_disc(arr, 0.5, 1.2, 0.1) is None


def test_read_disc_tiny_radius_reads_the_pixel_under_the_centre():
    u8 = _gradient()
    pixels = read_disc(u8, 0.25, 0.5, 0.0)
    assert pixels.shape == (1, 3)
    np.testing.assert_allclose(pixels[0], u8[40, 30] / 255.0, atol=1e-7)


def test_tiny_disc_on_a_pixel_edge_reads_the_pixel_clicked_not_its_neighbour():
    # A click exactly on the edge between columns 49 and 50 (and rows 49 and
    # 50) is in pixel (50, 50); the nearest centres tie, and the first of
    # them would be (49, 49), on the other side of this colour edge.
    arr = np.zeros((100, 100, 3), np.float32)
    arr[50:, 50:] = 0.8
    pixels = read_disc(arr, 0.5, 0.5, 0.001)
    assert pixels.shape == (1, 3)
    np.testing.assert_allclose(pixels[0], 0.8)


def test_read_disc_scales_16_bit_data_of_either_byte_order():
    for dtype in ("<u2", ">u2"):
        arr = np.full((20, 20, 3), 32768, dtype=dtype)
        pixels = read_disc(arr, 0.5, 0.5, 0.05)
        np.testing.assert_allclose(pixels, 32768 / 65535, rtol=1e-6)


def test_read_disc_samples_a_huge_spot_sparsely():
    arr = np.zeros((3000, 3000, 3), np.float32)
    pixels = read_disc(arr, 0.5, 0.5, 0.25)
    assert 50_000 < len(pixels) <= 250_000


# ---------------------------------------------------------------------------
# The whole pick, through the controller
# ---------------------------------------------------------------------------


def _picker_stub(editor):
    from faststack.app import AppController
    from faststack.controller.white_balance_picker import WhiteBalancePickerMixin

    class _Stub(WhiteBalancePickerMixin):
        _brush_point = AppController._brush_point
        _stack_geometry = AppController._stack_geometry

        def __init__(self):
            self.image_editor = editor
            self.image_files = ["a.jpg"]
            self._is_grid_view_active = False
            self.ui_state = SimpleNamespace(
                isWbPicking=False,
                isCropping=False,
                isBrushMode=False,
                isEditorOpen=True,
                isEditorExpanded=False,
                isHistogramVisible=False,
                brushPanHeld=False,
                wbPickRadius=0.02,
                white_balance_by=0.0,
                white_balance_mg=0.0,
                statusMessage="",
            )
            self.messages = []
            self.renders = 0
            self.batch_adds = 0

        def update_status_message(self, message, timeout=3000, color=""):
            self.messages.append(message)
            self.ui_state.statusMessage = message

        def _prepare_local_brush_image_state(self):
            return True

        def _refresh_brush_crop_scale(self):
            pass

        def exit_local_brush_mode(self):
            self.ui_state.isBrushMode = False

        def _clear_active_auto_adjust_state(self, reason="", *, clear_editor=False):
            pass

        def _kick_preview_worker(self):
            self.renders += 1

        def update_histogram(self):
            pass

        def _auto_add_edited_to_batch_if_enabled(self, path):
            self.batch_adds += 1
            return True

        def _can_show_original_compare(self, event):
            return False

    return _Stub()


# A tinted grey card on a colourful background, in base (unedited) pixels.
_CARD_LINEAR = (0.22, 0.18, 0.11)  # grey under warm light
_CARD_ROWS = slice(20, 56)
_CARD_COLS = slice(110, 150)


def _photo():
    h, w = 120, 180
    base = np.empty((h, w, 3), np.float32)
    base[:] = np.array([0.15, 0.55, 0.25], np.float32)  # foliage
    card = _linear_to_srgb(np.asarray(_CARD_LINEAR)).astype(np.float32)
    base[_CARD_ROWS, _CARD_COLS] = card
    return base


def _editor(edits):
    editor = ImageEditor()
    editor.float_image = _photo()
    editor.bit_depth = 8
    editor.current_filepath = "/photos/a.jpg"
    editor.current_edits = editor._initial_edits()
    editor.current_edits.update(edits)
    return editor


def _card_centre_displayed(editor):
    """Where the card's centre is on the displayed (edited) photo, [0, 1]."""
    h, w = editor.float_image.shape[:2]
    u = (_CARD_COLS.start + _CARD_COLS.stop) / 2 / w
    v = (_CARD_ROWS.start + _CARD_ROWS.stop) / 2 / h
    x, y, _ = geometry_normalized_affine(editor.current_edits, (h, w)) @ [u, v, 1.0]
    return float(x), float(y)


def _rendered_spread_at(editor, x, y, half=3):
    out = editor._apply_edits(editor.float_image.copy())
    oh, ow = out.shape[:2]
    cy, cx = int(y * oh), int(x * ow)
    spot = out[cy - half : cy + half + 1, cx - half : cx + half + 1].reshape(-1, 3)
    return float(np.ptp(spot.mean(axis=0)))


@pytest.mark.parametrize(
    "geometry",
    [
        {},
        {"rotation": 90, "crop_box": (100, 50, 900, 950)},
        {"rotation": 270, "straighten_angle": 4.0},
        {"rotation": 180, "straighten_angle": -3.0, "crop_box": (50, 100, 950, 900)},
    ],
    ids=["plain", "rotated+cropped", "rotated+straightened", "everything"],
)
def test_picking_the_card_makes_it_neutral_on_the_rendered_photo(geometry):
    pytest.importorskip("cv2")
    edits = {"exposure": 0.3, "contrast": 0.2, "vibrance": 0.4, **geometry}
    editor = _editor(edits)
    stub = _picker_stub(editor)
    x, y = _card_centre_displayed(editor)
    assert 0.0 < x < 1.0 and 0.0 < y < 1.0
    assert _rendered_spread_at(editor, x, y) > 0.05  # visibly warm before

    assert stub.start_white_balance_pick()
    assert stub.ui_state.isWbPicking
    stub.pick_white_balance_at(x, y)

    assert not stub.ui_state.isWbPicking  # one click, then done
    assert stub.renders == 1 and stub.batch_adds == 1
    assert "Temp" in stub.messages[-1]
    by = editor.current_edits["white_balance_by"]
    mg = editor.current_edits["white_balance_mg"]
    assert by < 0 and stub.ui_state.white_balance_by == by  # cooled the warm cast
    assert stub.ui_state.white_balance_mg == mg
    assert _rendered_spread_at(editor, x, y) < 0.004


def test_the_pick_replaces_temp_and_tint_instead_of_adding_to_them():
    # Two sessions that start from different sliders get the same values
    # from the same card: the spot is read from the unedited pixels.
    results = []
    for start in ({}, {"white_balance_by": 0.6, "white_balance_mg": -0.4}):
        editor = _editor(start)
        stub = _picker_stub(editor)
        stub.start_white_balance_pick()
        stub.pick_white_balance_at(*_card_centre_displayed(editor))
        results.append(
            (
                editor.current_edits["white_balance_by"],
                editor.current_edits["white_balance_mg"],
            )
        )
    assert results[0] == results[1]
    assert results[0] != (0.6, -0.4)


def test_picking_a_blown_spot_changes_nothing_and_stays_in_the_picker():
    editor = _editor({})
    editor.float_image[_CARD_ROWS, _CARD_COLS] = 1.0
    editor.current_edits["white_balance_by"] = 0.25
    stub = _picker_stub(editor)
    stub.start_white_balance_pick()
    stub.pick_white_balance_at(*_card_centre_displayed(editor))
    assert stub.ui_state.isWbPicking
    assert editor.current_edits["white_balance_by"] == 0.25
    assert stub.renders == 0
    assert "blown out" in stub.messages[-1]


def test_picker_refuses_to_start_while_cropping():
    stub = _picker_stub(_editor({}))
    stub.ui_state.isCropping = True
    assert not stub.start_white_balance_pick()
    assert not stub.ui_state.isWbPicking


def test_starting_the_picker_leaves_brush_mode():
    stub = _picker_stub(_editor({}))
    stub.ui_state.isBrushMode = True
    stub.start_white_balance_pick()
    assert stub.ui_state.isWbPicking and not stub.ui_state.isBrushMode


def test_toggle_and_cancel_clear_the_hint():
    stub = _picker_stub(_editor({}))
    stub.toggle_white_balance_pick()
    assert stub.ui_state.isWbPicking and stub.ui_state.statusMessage
    stub.toggle_white_balance_pick()
    assert not stub.ui_state.isWbPicking
    assert stub.ui_state.statusMessage == ""


class _Key:
    def __init__(self, key, modifiers=None, auto_repeat=False, text=None):
        from PySide6.QtCore import Qt

        self._key = key
        self._mods = Qt.NoModifier if modifiers is None else modifiers
        self._repeat = auto_repeat
        if text is None:
            text = {Qt.Key_BracketLeft: "[", Qt.Key_BracketRight: "]"}.get(key, "")
        self._text = text

    def key(self):
        return self._key

    def text(self):
        return self._text

    def modifiers(self):
        return self._mods

    def isAutoRepeat(self):
        return self._repeat


def test_bracket_keys_size_the_spot_within_its_range():
    from PySide6.QtCore import Qt

    stub = _picker_stub(_editor({}))
    start = stub.ui_state.wbPickRadius
    assert stub._handle_wb_pick_key(_Key(Qt.Key_BracketRight))
    assert stub.ui_state.wbPickRadius == pytest.approx(start * 1.2)
    assert stub._handle_wb_pick_key(_Key(Qt.Key_BracketLeft))
    assert stub.ui_state.wbPickRadius == pytest.approx(start)
    for _ in range(100):
        stub._handle_wb_pick_key(_Key(Qt.Key_BracketLeft))
    assert stub.ui_state.wbPickRadius == PICK_RADIUS_MIN
    for _ in range(100):
        stub._handle_wb_pick_key(_Key(Qt.Key_BracketRight))
    assert stub.ui_state.wbPickRadius == PICK_RADIUS_MAX
    # Ctrl+[ (no bracket typed) and Shift+[ ("{", start a batch) are not
    # the picker's.
    assert not stub._handle_wb_pick_key(
        _Key(Qt.Key_BracketLeft, Qt.ControlModifier, text="\x1b")
    )
    assert not stub._handle_wb_pick_key(
        _Key(Qt.Key_BraceLeft, Qt.ShiftModifier, text="{")
    )


def test_bracket_typed_with_altgr_sizes_the_spot():
    # Layouts that type [ with AltGr report Ctrl+Alt with it; the Keybinder
    # matches the typed "[" whatever the modifiers and would start a stack.
    from PySide6.QtCore import Qt

    stub = _picker_stub(_editor({}))
    start = stub.ui_state.wbPickRadius
    altgr = Qt.ControlModifier | Qt.AltModifier
    assert stub._handle_wb_pick_key(_Key(Qt.Key_BracketRight, altgr))
    assert stub.ui_state.wbPickRadius == pytest.approx(start * 1.2)


def test_escape_cancels_the_picker_unless_the_histogram_is_up():
    from PySide6.QtCore import Qt

    stub = _picker_stub(_editor({}))
    stub.start_white_balance_pick()
    stub.ui_state.isHistogramVisible = True
    assert not stub._handle_wb_pick_key(_Key(Qt.Key_Escape))
    assert stub.ui_state.isWbPicking
    stub.ui_state.isHistogramVisible = False
    assert stub._handle_wb_pick_key(_Key(Qt.Key_Escape))
    assert not stub.ui_state.isWbPicking


# ---------------------------------------------------------------------------
# The real controller: what ends the picker, and its keys
# ---------------------------------------------------------------------------


@pytest.fixture
def controller(tmp_path):
    from unittest.mock import Mock, patch

    from PySide6.QtWidgets import QApplication

    from faststack.app import AppController

    _app = QApplication.instance() or QApplication([])
    with (
        patch("faststack.app.Watcher"),
        patch("faststack.app.SidecarManager"),
        patch("faststack.app.setup_logging"),
        patch("faststack.app.QQmlApplicationEngine"),
    ):
        ctrl = AppController(tmp_path, Mock())
        ctrl._thumbnail_model = Mock()
        ctrl._thumbnail_model.rowCount.return_value = 0
        ctrl._path_resolver = Mock()
        yield ctrl


def test_grid_view_crop_and_navigation_end_the_picker(controller):
    ui = controller.ui_state
    ui.isWbPicking = True
    controller._is_grid_view_active = False
    controller._set_grid_view_active(True)
    assert not ui.isWbPicking

    # exit_local_brush_mode: crop mode, closing the editor, leaving a brush.
    ui.isWbPicking = True
    controller.exit_local_brush_mode()
    assert not ui.isWbPicking

    ui.isWbPicking = True
    controller._reset_local_brush_on_navigation()
    assert not ui.isWbPicking


def test_bracket_keys_size_the_spot_instead_of_marking_a_stack(controller):
    from unittest.mock import Mock

    from PySide6.QtCore import QEvent, QObject, Qt
    from PySide6.QtGui import QKeyEvent

    controller.main_window = QObject()
    controller.keybinder = Mock()
    controller._is_grid_view_active = False
    ui = controller.ui_state
    ui.isWbPicking = True
    start = ui.wbPickRadius
    press = QKeyEvent(QEvent.Type.KeyPress, Qt.Key_BracketRight, Qt.NoModifier, "]")
    assert controller.eventFilter(controller.main_window, press)
    assert ui.wbPickRadius == pytest.approx(start * 1.2)
    controller.keybinder.handle_key_press.assert_not_called()

    # Not picking: [ / ] go back to the loupe bindings (stack start / end).
    ui.isWbPicking = False
    controller.keybinder.handle_key_press.return_value = True
    controller.eventFilter(controller.main_window, press)
    controller.keybinder.handle_key_press.assert_called_once()
    assert ui.wbPickRadius == pytest.approx(start * 1.2)
