# faststack/ui/keystrokes.py
import logging

from PySide6.QtCore import Qt

log = logging.getLogger(__name__)


class Keybinder:
    def __init__(self, controller):
        """
        controller is your AppController.
        We will call controller.<method>() by default,
        but if controller.main_window has a QML method of the same name,
        we'll call that instead so the footer/UI stays in sync.
        """
        self.controller = controller

        # map keys → method names (not callables)
        self.key_map = {
            # View switching
            Qt.Key_Escape: "switch_to_grid_view",
            # Navigation
            Qt.Key_Right: "next_image",
            Qt.Key_Left: "prev_image",
            Qt.Key_G: "show_jump_to_image_dialog",
            # Stacking
            Qt.Key_BracketLeft: "begin_new_stack",
            Qt.Key_BracketRight: "end_current_stack",
            Qt.Key_S: "toggle_stack_membership",
            # Batching
            Qt.Key_BraceLeft: "begin_new_batch",
            Qt.Key_BraceRight: "end_current_batch",
            Qt.Key_B: "toggle_batch_membership",
            # Remove from batch/stack
            Qt.Key_X: "remove_from_batch_or_stack",
            # Toggle flags
            Qt.Key_U: "toggle_uploaded",
            Qt.Key_F: "toggle_favorite",
            Qt.Key_D: "toggle_todo",
            Qt.Key_I: "show_exif_dialog",
            # Actions
            Qt.Key_Enter: "launch_helicon",
            Qt.Key_Return: "launch_helicon",
            Qt.Key_P: "edit_in_photoshop",
            Qt.Key_R: "open_raw_in_rawtherapee",
            Qt.Key_C: "clear_all_stacks",
            Qt.Key_A: "quick_auto_white_balance",
            Qt.Key_O: "toggle_crop_mode",
            Qt.Key_H: "toggle_histogram",
            Qt.Key_Delete: "delete_current_image",
            Qt.Key_Backspace: "delete_current_image",
        }

        self.modifier_key_map = {
            (Qt.Key_D, Qt.ShiftModifier): "toggle_local_brush_mode",
            (Qt.Key_C, Qt.ControlModifier): "copy_path_to_clipboard",
            (Qt.Key_Right, Qt.ShiftModifier): "next_image_by_10",
            (Qt.Key_Left, Qt.ShiftModifier): "prev_image_by_10",
            (Qt.Key_0, Qt.ControlModifier): "reset_zoom_pan",
            (Qt.Key_Z, Qt.ControlModifier): "undo_delete",
            (Qt.Key_E, Qt.ControlModifier): "toggle_edited",
            (Qt.Key_S, Qt.ControlModifier): "toggle_stacked",
            (
                Qt.Key_B,
                Qt.ControlModifier | Qt.ShiftModifier,
            ): "quick_auto_white_balance",
            (Qt.Key_1, Qt.ControlModifier): "zoom_100",
            (Qt.Key_2, Qt.ControlModifier): "zoom_200",
            (Qt.Key_3, Qt.ControlModifier): "zoom_300",
            (Qt.Key_4, Qt.ControlModifier): "zoom_400",
            (Qt.Key_U, Qt.AltModifier): "jump_to_last_uploaded",
        }

    def _call(self, method_name: str):
        """
        Try QML root first (to keep footer/UI happy), then controller.
        """
        mw = getattr(self.controller, "main_window", None)
        if mw is not None and hasattr(mw, method_name):
            getattr(mw, method_name)()
            return

        if hasattr(self.controller, method_name):
            getattr(self.controller, method_name)()
            return

        log.warning(
            f"Keybinder: neither main_window nor controller has '{method_name}'"
        )

    # Modifiers that select a binding. Keypad and layout-group flags only say
    # where a key came from, so they never change which action runs.
    _BINDING_MODIFIERS = (
        Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier | Qt.ShiftModifier
    )
    _COMMAND_MODIFIERS = Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier

    # Bound by the character the active layout produced, whatever modifiers
    # it took to type it: AltGr arrives as Ctrl+Alt on Windows, so [ ] { }
    # on many European layouts would otherwise never match.
    _CHARACTER_MAP = {
        "[": "begin_new_stack",
        "]": "end_current_stack",
        "{": "begin_new_batch",
        "}": "end_current_batch",
    }

    @staticmethod
    def _is_letter_key(key) -> bool:
        return Qt.Key_A <= key <= Qt.Key_Z

    def _modifier_binding(self, key, modifiers):
        for (mapped_key, mapped_modifier), method_name in self.modifier_key_map.items():
            if key == mapped_key and modifiers == mapped_modifier:
                return method_name
        return None

    # Pressing a modifier on its own is not a command; it used to be logged
    # as one ("Key pressed: 16777248 ('')") every time Shift or Ctrl went down.
    _MODIFIER_KEYS = frozenset(
        {
            Qt.Key_Shift,
            Qt.Key_Control,
            Qt.Key_Alt,
            Qt.Key_AltGr,
            Qt.Key_Meta,
            Qt.Key_Super_L,
            Qt.Key_Super_R,
            Qt.Key_CapsLock,
            Qt.Key_NumLock,
            Qt.Key_ScrollLock,
        }
    )

    @staticmethod
    def describe_key(key, modifiers) -> str:
        """``Ctrl+Shift+B`` rather than ``66 ... ControlModifier|ShiftModifier``."""
        try:
            name = Qt.Key(key).name.removeprefix("Key_")
        except (ValueError, TypeError, AttributeError):
            name = f"key {key}"
        parts = [
            label
            for flag, label in (
                (Qt.ControlModifier, "Ctrl"),
                (Qt.AltModifier, "Alt"),
                (Qt.MetaModifier, "Meta"),
                (Qt.ShiftModifier, "Shift"),
            )
            if modifiers & flag
        ]
        return "+".join([*parts, name])

    def handle_key_press(self, event):
        key = event.key()
        if key in self._MODIFIER_KEYS:
            return False
        modifiers = event.modifiers() & self._BINDING_MODIFIERS
        method_name = self._binding_for(key, event.text(), modifiers)
        if log.isEnabledFor(logging.DEBUG):
            # One line per key: what was pressed and what it ran.
            log.debug(
                "Key %s -> %s",
                self.describe_key(key, modifiers),
                method_name or "(unbound)",
            )
        if not method_name:
            return False
        self._call(method_name)
        return True

    def _binding_for(self, key, text, modifiers):
        """The action a key press runs, or None. Never calls anything."""
        method_name = self._CHARACTER_MAP.get(text)
        if method_name:
            return method_name

        # Modifier combinations match exactly, so Ctrl+Shift+C is not Ctrl+C
        # and a future binding cannot be shadowed by a shorter one. Shift is
        # how some layouts type digits and symbols (AZERTY sends Ctrl+1 as
        # Ctrl+Shift+&), so a non-letter key is retried without it.
        candidates = [modifiers]
        if modifiers & Qt.ShiftModifier and not self._is_letter_key(key):
            candidates.append(modifiers & ~Qt.ShiftModifier)
        for candidate in candidates:
            method_name = self._modifier_binding(key, candidate)
            if method_name:
                return method_name

        command_modifiers = modifiers & self._COMMAND_MODIFIERS

        # Check for single key presses
        if key == Qt.Key_L:
            if not command_modifiers:
                return (
                    "quick_auto_adjust"
                    if modifiers & Qt.ShiftModifier
                    else "quick_auto_levels"
                )
            return None

        if command_modifiers:
            # Unbound Ctrl/Alt/Meta chords (Ctrl+P, Alt+D, ...) no longer fall
            # through to the plain-key action.
            return None

        if text == "-":
            return "reduce_auto_adjust_highlights"
        if text == "_":
            return "raise_auto_adjust_whites"
        if text == "+":
            return "raise_auto_adjust_blacks"
        if text == "=":
            return "deepen_auto_adjust_blacks"
        if key == Qt.Key_Minus and modifiers == Qt.NoModifier:
            return "reduce_auto_adjust_highlights"
        if key == Qt.Key_Underscore:
            return "raise_auto_adjust_whites"
        if key == Qt.Key_Plus:
            return "raise_auto_adjust_blacks"
        if key == Qt.Key_Equal and modifiers == Qt.NoModifier:
            return "deepen_auto_adjust_blacks"

        method_name = self.key_map.get(key)
        if method_name:
            # Letter bindings are plain keys: Shift+letter is left unbound.
            # Symbol keys such as { } _ + need Shift to be typed at all.
            if self._is_letter_key(key) and modifiers & Qt.ShiftModifier:
                return None
            return method_name

        return None
