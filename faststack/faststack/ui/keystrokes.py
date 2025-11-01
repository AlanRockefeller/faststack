"""Maps Qt Key events to application actions."""

from PySide6.QtCore import Qt

class Keybinder:
    def __init__(self, main_window):
        self.main_window = main_window
        self.key_map = {
            # Navigation
            Qt.Key.Key_J: self.main_window.next_image,
            Qt.Key.Key_Right: self.main_window.next_image,
            Qt.Key.Key_K: self.main_window.prev_image,
            Qt.Key.Key_Left: self.main_window.prev_image,

            # View Mode
            Qt.Key.Key_G: self.main_window.toggle_grid_view,

            # Metadata
            Qt.Key.Key_Space: self.main_window.toggle_current_flag,
            Qt.Key.Key_X: self.main_window.toggle_current_reject,

            # Stacking
            Qt.Key.Key_S: self.main_window.toggle_selection,
            Qt.Key.Key_BracketLeft: self.main_window.begin_new_stack,
            Qt.Key.Key_BracketRight: self.main_window.end_current_stack,

            # Actions
            Qt.Key.Key_Return: self.main_window.launch_helicon,
            Qt.Key.Key_Enter: self.main_window.launch_helicon,
        }

    def handle_key_press(self, event):
        """Handles a key press event from the main window."""
        action = self.key_map.get(event.key())
        if action:
            action()
            return True
        return False
