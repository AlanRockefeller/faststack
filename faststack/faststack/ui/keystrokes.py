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
            # Navigation
            Qt.Key_J: "next_image",
            Qt.Key_Right: "next_image",
            Qt.Key_K: "prev_image",
            Qt.Key_Left: "prev_image",

            # View Mode
            Qt.Key_G: "toggle_grid_view",

            # Metadata
            Qt.Key_Space: "toggle_current_flag",
            Qt.Key_X: "toggle_current_reject",

            # Stacking
            Qt.Key_BracketLeft: "begin_new_stack",
            Qt.Key_BracketRight: "end_current_stack",

            # Actions
            Qt.Key_S: "toggle_selection",
            Qt.Key_Enter: "launch_helicon",
            Qt.Key_Return: "launch_helicon",
            Qt.Key_E: "edit_in_photoshop",
            Qt.Key_C: "clear_all_stacks", # Keep C for clear_all_stacks
            Qt.Key_Delete: "delete_current_image",
            Qt.Key_Backspace: "delete_current_image",
        }

        self.modifier_key_map = {
            (Qt.Key_C, Qt.ControlModifier): "copy_path_to_clipboard",
            (Qt.Key_0, Qt.ControlModifier): "reset_zoom_pan",
            (Qt.Key_Z, Qt.ControlModifier): "undo_delete",
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

        log.warning(f"Keybinder: neither main_window nor controller has '{method_name}'")

    def handle_key_press(self, event):
        key = event.key()
        text = event.text()
        log.info(f"Key pressed: {key} ({text!r}) with modifiers {event.modifiers()}")

        # Check for modifier + key combinations
        for (mapped_key, mapped_modifier), method_name in self.modifier_key_map.items():
            if key == mapped_key and event.modifiers() & mapped_modifier:
                self._call(method_name)
                return True

        # Check for single key presses
        method_name = self.key_map.get(key)
        if method_name:
            self._call(method_name)
            return True

        # extra safety for layouts where bracket keycodes are odd
        if text == "[":
            self._call("begin_new_stack")
            return True
        if text == "]":
            self._call("end_current_stack")
            return True

        return False
