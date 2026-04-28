import ast
import importlib.util
import sys
import textwrap
import types
from enum import IntFlag
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import Mock

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_PATH = REPO_ROOT / "faststack" / "app.py"
KEYSTROKES_PATH = REPO_ROOT / "faststack" / "ui" / "keystrokes.py"
APP_SOURCE = APP_PATH.read_text(encoding="utf-8")
APP_AST = ast.parse(APP_SOURCE)


def _extract_app_method(method_name: str):
    for node in APP_AST.body:
        if isinstance(node, ast.ClassDef) and node.name == "AppController":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    namespace = {
                        "Optional": Optional,
                        "Path": Path,
                    }
                    exec(
                        textwrap.dedent(ast.get_source_segment(APP_SOURCE, item)),
                        namespace,
                    )
                    return namespace[method_name]
    raise AssertionError(f"Method not found: AppController.{method_name}")


def _extract_app_method_source(method_name: str) -> str:
    for node in APP_AST.body:
        if isinstance(node, ast.ClassDef) and node.name == "AppController":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    source = ast.get_source_segment(APP_SOURCE, item)
                    assert source is not None
                    return textwrap.dedent(source)
    raise AssertionError(f"Method not found: AppController.{method_name}")


class _Qt(IntFlag):
    NoModifier = 0
    ShiftModifier = 1
    ControlModifier = 2
    AltModifier = 4
    MetaModifier = 8
    Key_L = 100
    Key_Minus = 101
    Key_Equal = 102
    Key_Escape = 103
    Key_Right = 104
    Key_Left = 105
    Key_G = 106
    Key_BracketLeft = 107
    Key_BracketRight = 108
    Key_S = 109
    Key_BraceLeft = 110
    Key_BraceRight = 111
    Key_Backslash = 112
    Key_B = 113
    Key_X = 114
    Key_U = 115
    Key_F = 116
    Key_D = 117
    Key_I = 118
    Key_Enter = 119
    Key_Return = 120
    Key_P = 121
    Key_C = 122
    Key_A = 123
    Key_O = 124
    Key_H = 125
    Key_Delete = 126
    Key_Backspace = 127
    Key_Z = 128
    Key_E = 129
    Key_0 = 130
    Key_1 = 131
    Key_2 = 132
    Key_3 = 133
    Key_4 = 134
    Key_Underscore = 135


def _load_keybinder_class():
    qtcore = types.ModuleType("PySide6.QtCore")
    qtcore.Qt = _Qt
    pyside6 = types.ModuleType("PySide6")
    pyside6.QtCore = qtcore
    sys.modules["PySide6"] = pyside6
    sys.modules["PySide6.QtCore"] = qtcore

    spec = importlib.util.spec_from_file_location(
        "faststack_test_keystrokes",
        KEYSTROKES_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.Keybinder


class _Event:
    def __init__(self, key, text, modifiers):
        self._key = key
        self._text = text
        self._modifiers = modifiers

    def key(self):
        return self._key

    def text(self):
        return self._text

    def modifiers(self):
        return self._modifiers


class _Controller:
    def __init__(self):
        self.calls = []
        self.main_window = None

    def quick_auto_levels(self):
        self.calls.append("quick_auto_levels")

    def quick_auto_adjust(self):
        self.calls.append("quick_auto_adjust")

    def quick_auto_white_balance(self):
        self.calls.append("quick_auto_white_balance")

    def toggle_batch_membership(self):
        self.calls.append("toggle_batch_membership")

    def reduce_auto_adjust_highlights(self):
        self.calls.append("reduce_auto_adjust_highlights")

    def raise_auto_adjust_whites(self):
        self.calls.append("raise_auto_adjust_whites")

    def deepen_auto_adjust_blacks(self):
        self.calls.append("deepen_auto_adjust_blacks")


def test_clear_active_auto_adjust_state_clears_editor_even_if_editor_ui_is_open():
    clear_active_auto_adjust_state = _extract_app_method(
        "_clear_active_auto_adjust_state"
    )
    controller = SimpleNamespace(
        _cancel_pending_auto_adjust_save=Mock(),
        _active_auto_adjust_state=object(),
        ui_state=SimpleNamespace(isEditorOpen=True),
        image_editor=SimpleNamespace(clear=Mock()),
    )

    clear_active_auto_adjust_state(controller, clear_editor=True)

    controller._cancel_pending_auto_adjust_save.assert_called_once_with()
    controller.image_editor.clear.assert_called_once_with()
    assert controller._active_auto_adjust_state is None


def test_undo_flushes_pending_auto_adjust_save_before_reporting_nothing_to_undo():
    undo_delete = _extract_app_method("undo_delete")
    controller = SimpleNamespace(
        _auto_adjust_save_pending_action="auto_adjust",
        undo_history=[],
        update_status_message=Mock(),
        _parse_edit_undo_data=lambda data: data,
        _restore_backup_safe=lambda saved_path, backup_path: True,
        _clear_active_auto_adjust_state=Mock(),
        _restore_metadata_snapshot=Mock(),
        _post_undo_refresh_and_select=Mock(),
        _is_current_live_edit_session_dirty=Mock(return_value=False),
        sidecar=object(),
    )

    def flush_pending():
        controller.undo_history.append(
            (
                "auto_adjust",
                ("saved.jpg", "backup.jpg", "saved.jpg", None, None),
                123.0,
            )
        )

    controller._flush_pending_auto_adjust_save = Mock(side_effect=flush_pending)

    undo_delete(controller)

    controller._flush_pending_auto_adjust_save.assert_called_once_with()
    messages = [
        call.args[0] for call in controller.update_status_message.call_args_list
    ]
    assert "Nothing to undo." not in messages
    assert messages[-1] == "Undid auto adjust"


def test_quick_auto_levels_stays_preview_only():
    quick_auto_levels = _extract_app_method("quick_auto_levels")
    controller = SimpleNamespace(
        image_files=[object()],
        update_status_message=Mock(),
        _clear_active_auto_adjust_state=Mock(),
        _ensure_active_image_loaded_for_auto_adjust=Mock(
            return_value=Path("image.jpg")
        ),
        _seed_active_auto_adjust_state=Mock(return_value=object()),
        _apply_auto_adjust_preview=Mock(),
    )

    quick_auto_levels(controller)

    controller._clear_active_auto_adjust_state.assert_called_once_with(
        "quick auto levels starts a fresh active auto-adjust state",
        clear_editor=False,
    )
    controller._apply_auto_adjust_preview.assert_called_once()


def test_quick_auto_adjust_stays_preview_only():
    quick_auto_adjust = _extract_app_method("quick_auto_adjust")
    controller = SimpleNamespace(
        image_files=[object()],
        _last_auto_levels_msg="",
        update_status_message=Mock(),
        _clear_active_auto_adjust_state=Mock(),
        _ensure_active_image_loaded_for_auto_adjust=Mock(
            return_value=Path("image.jpg")
        ),
        auto_white_balance=Mock(return_value="awb"),
        _seed_active_auto_adjust_state=Mock(return_value=object()),
    )

    def apply_preview(_state):
        controller._last_auto_levels_msg = "levels"

    controller._apply_auto_adjust_preview = Mock(side_effect=apply_preview)

    quick_auto_adjust(controller)

    controller._clear_active_auto_adjust_state.assert_called_once_with(
        "combined auto-adjust starts a fresh active auto-adjust state",
        clear_editor=False,
    )
    controller.auto_white_balance.assert_called_once_with()
    controller._apply_auto_adjust_preview.assert_called_once()
    controller.update_status_message.assert_called_with("awb; levels", timeout=9000)


def test_quick_auto_white_balance_stays_preview_only():
    quick_auto_white_balance = _extract_app_method("quick_auto_white_balance")
    controller = SimpleNamespace(
        image_files=[object()],
        update_status_message=Mock(),
        _clear_active_auto_adjust_state=Mock(),
        _ensure_active_image_loaded_for_auto_adjust=Mock(
            return_value=Path("image.jpg")
        ),
        auto_white_balance=Mock(return_value="awb"),
    )

    quick_auto_white_balance(controller)

    controller._clear_active_auto_adjust_state.assert_called_once_with(
        "quick auto white balance starts a fresh live baseline",
        clear_editor=False,
    )
    controller._ensure_active_image_loaded_for_auto_adjust.assert_called_once_with()
    controller.auto_white_balance.assert_called_once_with()


def test_execute_crop_source_no_longer_saves_or_pushes_undo():
    source = _extract_app_method_source("execute_crop")

    assert ".save_image(" not in source
    assert "undo_history.append" not in source
    assert "_build_edit_undo_data" not in source


def test_navigation_flushes_live_session_before_switching_index():
    source = _extract_app_method_source("_set_current_index")

    assert "_flush_current_live_edit_session_for_navigation()" in source
    assert source.index(
        "_flush_current_live_edit_session_for_navigation()"
    ) < source.index("self.current_index = index")


def test_drag_path_flushes_live_session_before_drag():
    source = _extract_app_method_source("start_drag_current_image")

    assert "_flush_current_live_edit_session_for_drag()" in source


def test_caps_lock_style_uppercase_l_without_shift_still_runs_quick_auto_levels():
    keybinder_cls = _load_keybinder_class()
    controller = _Controller()
    keybinder = keybinder_cls(controller)

    handled = keybinder.handle_key_press(_Event(_Qt.Key_L, "L", _Qt.NoModifier))

    assert handled is True
    assert controller.calls == ["quick_auto_levels"]


def test_shift_l_runs_combined_auto_adjust():
    keybinder_cls = _load_keybinder_class()
    controller = _Controller()
    keybinder = keybinder_cls(controller)

    handled = keybinder.handle_key_press(_Event(_Qt.Key_L, "L", _Qt.ShiftModifier))

    assert handled is True
    assert controller.calls == ["quick_auto_adjust"]


def test_ctrl_shift_b_runs_quick_auto_white_balance():
    keybinder_cls = _load_keybinder_class()
    controller = _Controller()
    keybinder = keybinder_cls(controller)

    handled = keybinder.handle_key_press(
        _Event(_Qt.Key_B, "B", _Qt.ControlModifier | _Qt.ShiftModifier)
    )

    assert handled is True
    assert controller.calls == ["quick_auto_white_balance"]


def test_ctrl_b_does_not_match_ctrl_shift_b_shortcut():
    keybinder_cls = _load_keybinder_class()
    controller = _Controller()
    keybinder = keybinder_cls(controller)

    handled = keybinder.handle_key_press(_Event(_Qt.Key_B, "b", _Qt.ControlModifier))

    assert handled is True
    assert controller.calls == ["toggle_batch_membership"]


def test_shift_b_does_not_match_ctrl_shift_b_shortcut():
    keybinder_cls = _load_keybinder_class()
    controller = _Controller()
    keybinder = keybinder_cls(controller)

    handled = keybinder.handle_key_press(_Event(_Qt.Key_B, "B", _Qt.ShiftModifier))

    assert handled is True
    assert controller.calls == ["toggle_batch_membership"]


def test_shift_equals_character_still_triggers_shadow_adjust():
    keybinder_cls = _load_keybinder_class()
    controller = _Controller()
    keybinder = keybinder_cls(controller)

    handled = keybinder.handle_key_press(_Event(_Qt.Key_Equal, "=", _Qt.ShiftModifier))

    assert handled is True
    assert controller.calls == ["deepen_auto_adjust_blacks"]


def test_underscore_character_still_triggers_raise_whites():
    keybinder_cls = _load_keybinder_class()
    controller = _Controller()
    keybinder = keybinder_cls(controller)

    handled = keybinder.handle_key_press(
        _Event(_Qt.Key_Minus, "_", _Qt.ShiftModifier)
    )

    assert handled is True
    assert controller.calls == ["raise_auto_adjust_whites"]


def test_shift_minus_character_still_triggers_highlight_adjust():
    keybinder_cls = _load_keybinder_class()
    controller = _Controller()
    keybinder = keybinder_cls(controller)

    handled = keybinder.handle_key_press(_Event(_Qt.Key_Minus, "-", _Qt.ShiftModifier))

    assert handled is True
    assert controller.calls == ["reduce_auto_adjust_highlights"]
