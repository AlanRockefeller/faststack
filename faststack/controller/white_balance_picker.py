"""White balance picker: click a white or grey spot to set white balance.

The compact editor's "Pick" button (next to COLOR's Auto) turns the cursor
into a ring like the brushes' ring. The next left click on the photo reads the
spot under the ring from the unedited image and sets Temp and Tint so that
spot comes out neutral (the math is in faststack/imaging/wb_pick.py).
[ and ] size the ring, Esc or a right click cancels, and a successful pick
ends the picker. A spot that is blown out (or black) has lost its colour, so
it is refused with a message and the picker stays on for another try.

Mixed into AppController (faststack/app.py); every method runs on the
controller instance.
"""

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Slot

from faststack.imaging.wb_pick import (
    PICK_CLIPPED,
    PICK_DARK,
    PICK_RADIUS_MAX,
    PICK_RADIUS_MIN,
    WhiteBalancePick,
    solve_white_balance,
)

log = logging.getLogger(__name__)

# [ / ] scale the spot by this much per press, as they do the brushes.
_PICK_SIZE_STEP = 1.2

_PICK_HINT = (
    "White balance: click a white or grey spot in the photo "
    "([ ] spot size, Esc cancels)"
)
_PICK_REFUSED = {
    PICK_CLIPPED: (
        "That spot is blown out, so it has no colour left to go by. "
        "Click a white or grey spot that still shows detail"
    ),
    PICK_DARK: (
        "That spot is too dark to read its colour. Click a lighter white or grey"
    ),
}
_PICK_OUTSIDE_MESSAGE = "Click on the photo to pick its white balance"
# Hints and refusals stay up a little longer than the usual 3 seconds.
_PICK_MESSAGE_MS = 6000


def _slider_text(value: float) -> str:
    """A Temp / Tint value as the compact editor shows it (-100..100)."""
    shown = round(value * 100)
    return f"+{shown}" if shown > 0 else ("0" if shown == 0 else f"−{-shown}")


class WhiteBalancePickerMixin:
    """Pick white balance from a spot on the photo (see the module docstring)."""

    @Slot()
    def toggle_white_balance_pick(self) -> None:
        """The compact editor's Pick button: start picking, or stop."""
        if self.ui_state.isWbPicking:
            self.cancel_white_balance_pick()
            return
        self.start_white_balance_pick()

    def start_white_balance_pick(self) -> bool:
        ui = self.ui_state
        if ui.isCropping:
            self.update_status_message(
                "Apply or cancel the crop before picking white balance"
            )
            return False
        if self._is_grid_view_active or not self.image_files:
            return False
        # The ring is drawn on the loupe, which the full editor covers.
        if not ui.isEditorOpen or ui.isEditorExpanded:
            ui.isEditorExpanded = False
            ui.isEditorOpen = True
        # White balance is a global edit: leave the brushes (dodge & burn
        # sliders would otherwise be setting a recipe, not the photo).
        if ui.isBrushMode:
            self.exit_local_brush_mode()
        if not self._prepare_local_brush_image_state():
            return False
        # The ring is sized like the brushes', against the uncropped frame.
        self._refresh_brush_crop_scale()
        ui.isWbPicking = True
        self.update_status_message(_PICK_HINT, timeout=_PICK_MESSAGE_MS)
        return True

    @Slot()
    def cancel_white_balance_pick(self) -> None:
        ui = self.ui_state
        if not ui.isWbPicking:
            return
        ui.isWbPicking = False
        # Don't leave "click a white or grey spot" up once it no longer does.
        if ui.statusMessage == _PICK_HINT:
            ui.statusMessage = ""

    @Slot(int)
    def adjust_white_balance_pick_size(self, direction: int) -> None:
        factor = _PICK_SIZE_STEP if direction > 0 else 1 / _PICK_SIZE_STEP
        self.ui_state.wbPickRadius = min(
            PICK_RADIUS_MAX,
            max(PICK_RADIUS_MIN, float(self.ui_state.wbPickRadius) * factor),
        )

    @Slot(float, float)
    def pick_white_balance_at(self, x_norm: float, y_norm: float) -> None:
        """Set white balance from the spot at displayed-image (x, y) in [0, 1]."""
        ui = self.ui_state
        if not ui.isWbPicking or ui.isCropping:
            return
        if not self._prepare_local_brush_image_state():
            self.cancel_white_balance_pick()
            return
        # The same mapping the brushes paint through, so the pixels read are
        # the ones under the ring whatever the rotation, straighten or crop.
        u, v = self._brush_point(float(x_norm), float(y_norm))
        pixels = self.image_editor.read_base_disc(u, v, float(ui.wbPickRadius))
        if pixels is None:
            self.update_status_message(_PICK_OUTSIDE_MESSAGE, timeout=_PICK_MESSAGE_MS)
            return
        pick = solve_white_balance(pixels)
        if pick.error is not None:
            # Stay in the picker: the next click is another try.
            self.update_status_message(
                _PICK_REFUSED.get(pick.error, _PICK_OUTSIDE_MESSAGE),
                timeout=_PICK_MESSAGE_MS,
            )
            return
        log.debug(
            "White balance pick at (%.4f, %.4f) r=%.4f: %d px, linear "
            "RGB %.4f/%.4f/%.4f -> by=%.4f mg=%.4f%s",
            u,
            v,
            ui.wbPickRadius,
            pick.pixels,
            *pick.mean_linear,
            pick.by,
            pick.mg,
            " (clamped)" if pick.clamped else "",
        )
        self._apply_picked_white_balance(pick)
        ui.isWbPicking = False
        message = (
            f"White balance from the picked spot: Temp {_slider_text(pick.by)}, "
            f"Tint {_slider_text(pick.mg)}"
        )
        if pick.clamped:
            message += (
                ". The cast is stronger than the sliders reach, so the spot "
                "is only nearly neutral"
            )
        self.update_status_message(message)

    def _apply_picked_white_balance(self, pick: WhiteBalancePick) -> None:
        """Set Temp and Tint together, then render once.

        Two set_edit_parameter calls would each start a preview render, and
        the first would show the photo with only Temp changed.
        """
        editor = self.image_editor
        changed = False
        for key, value in (
            ("white_balance_by", pick.by),
            ("white_balance_mg", pick.mg),
        ):
            changed = editor.set_edit_param(key, value) or changed
            setattr(self.ui_state, key, float(editor.get_edit_value(key, value)))
        if not changed:
            return
        self._clear_active_auto_adjust_state(
            "white balance picked from a spot", clear_editor=False
        )
        self._kick_preview_worker()
        self.update_histogram()
        if editor.current_filepath:
            self._auto_add_edited_to_batch_if_enabled(Path(editor.current_filepath))

    def _handle_wb_pick_key(self, event) -> bool:
        """Picker keys; like the brush keys they shadow the loupe bindings
        ([ and ] start and end a stack otherwise) only while picking."""
        key = event.key()
        mods = event.modifiers() & (
            Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier | Qt.ShiftModifier
        )
        if key == Qt.Key_Escape:
            if self.ui_state.isHistogramVisible:
                return False  # Esc closes the histogram first, as elsewhere
            self.cancel_white_balance_pick()
            return True
        if key in (Qt.Key_BracketLeft, Qt.Key_BracketRight) and not mods:
            self.adjust_white_balance_pick_size(1 if key == Qt.Key_BracketRight else -1)
            return True
        if key == Qt.Key_Space and not mods:
            # As in the brushes: Space shows the original, and Space+drag
            # pans instead of picking.
            self.ui_state.brushPanHeld = True
            if not event.isAutoRepeat() and self._can_show_original_compare(event):
                self.start_original_compare_preview()
            return True
        return False
