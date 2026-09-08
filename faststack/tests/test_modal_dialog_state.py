from unittest.mock import MagicMock


def test_native_modal_dialog_balances_global_dialog_state_on_error(app_controller):
    dialog = MagicMock()
    dialog.exec.side_effect = RuntimeError("dialog failed")
    app_controller._dialog_open_count = 0
    app_controller._dialog_open = False
    states = []
    app_controller.dialogStateChanged.connect(states.append)

    try:
        app_controller._exec_modal_dialog(dialog)
    except RuntimeError:
        pass

    assert app_controller._dialog_open_count == 0
    assert app_controller._dialog_open is False
    assert states == [True, False]
