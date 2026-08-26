"""Regression tests for FS-P1-001: every user-facing exit honours the veto.

The window-close path (``ApplicationWindow.onClosing``) is the only place that
consults ``controller.prepare_for_app_close()``. ``Qt.quit()`` bypasses it, and
``aboutToQuit`` can retry a failed save but cannot cancel the quit -- so any
exit action that called ``Qt.quit()`` could drop unsaved edits.

These are runtime tests: the *actual* handler bodies are lifted out of the
shipped ``Main.qml`` and executed against a stub window, so they exercise the
code that ships rather than a paraphrase of it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

QML_PATH = Path(__file__).resolve().parents[1] / "qml" / "Main.qml"


# --------------------------------------------------------------------------
# Extraction of the real handler bodies from Main.qml
# --------------------------------------------------------------------------


def _match_braces(text: str, open_index: int) -> str:
    """Return the contents of the {...} block whose '{' is at ``open_index``."""
    assert text[open_index] == "{", text[open_index : open_index + 20]
    depth = 0
    i = open_index
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : i]
        elif ch in "\"'":
            quote = ch
            i += 1
            while i < len(text) and text[i] != quote:
                i += 2 if text[i] == "\\" else 1
        elif text.startswith("//", i):
            i = text.find("\n", i)
            if i == -1:
                break
        i += 1
    raise AssertionError("unbalanced braces in Main.qml")


def _on_closing_body(qml: str) -> str:
    anchor = "onClosing: function(close) {"
    start = qml.index(anchor)
    return _match_braces(qml, start + len(anchor) - 1)


def _on_clicked_blocks(qml: str) -> list[str]:
    """Every ``onClicked: { ... }`` block body in the file."""
    bodies = []
    for m in re.finditer(r"onClicked:\s*\{", qml):
        bodies.append(_match_braces(qml, m.end() - 1))
    return bodies


def _exit_menu_action(qml: str) -> str:
    """The File -> Exit menu item's onClicked expression."""
    idx = qml.index('text: "Exit"')
    tail = qml[idx : idx + 800]
    m = re.search(r"onClicked:[ \t]*(.+)", tail)
    assert m, "no onClicked after the Exit menu item"
    return m.group(1).strip()


def _recycle_bin_quit_actions(qml: str) -> tuple[str, str]:
    """The ("Keep and Quit", "Delete and Quit") handler bodies."""
    blocks = [b for b in _on_clicked_blocks(qml) if "allowCloseWithRecycleBins" in b]
    keep = [b for b in blocks if "cleanupRecycleBins" not in b]
    delete = [b for b in blocks if "cleanupRecycleBins" in b]
    assert len(keep) == 1, f"expected one Keep-and-Quit handler, got {len(keep)}"
    assert len(delete) == 1, f"expected one Delete-and-Quit handler, got {len(delete)}"
    return keep[0], delete[0]


# --------------------------------------------------------------------------
# Runtime harness
# --------------------------------------------------------------------------

HARNESS = """
import QtQuick
import QtQuick.Controls

ApplicationWindow {
    id: root
    visible: true
    width: 200
    height: 200

    property bool allowCloseWithRecycleBins: false
    property bool prepareResult: true
    property int prepareCalls: 0
    property int cleanupCalls: 0
    property int refreshCalls: 0
    property int dialogOpenCalls: 0
    property int dialogCloseCalls: 0
    property int placementSaves: 0
    property bool hasBins: false

    property QtObject uiStateRef: QtObject {
        property bool hasRecycleBinItems: root.hasBins
        function refreshRecycleBinStats() { root.refreshCalls += 1 }
        function cleanupRecycleBins() { root.cleanupCalls += 1 }
    }

    property QtObject controllerRef: QtObject {
        function prepare_for_app_close() {
            root.prepareCalls += 1
            return root.prepareResult
        }
    }

    QtObject {
        id: recycleBinCleanupDialog
        function close() { root.dialogCloseCalls += 1 }
    }

    function openDialogSafely(dlg) { root.dialogOpenCalls += 1 }
    function saveWindowPlacement(force) { root.placementSaves += 1 }
    function queueWindowPlacementSave() {}

    onClosing: function(close) {__ON_CLOSING__}

    function fileExit() {__FILE_EXIT__}
    function keepAndQuit() {__KEEP__}
    function deleteAndQuit() {__DELETE__}
}
"""


@pytest.fixture(scope="module")
def qml_source() -> str:
    return QML_PATH.read_text(encoding="utf-8")


@pytest.fixture
def window(qml_source):
    """A stub ApplicationWindow running Main.qml's real lifecycle handlers."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtQuick")
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine

    app = QGuiApplication.instance() or QGuiApplication([])
    assert app is not None

    exit_action = _exit_menu_action(qml_source)
    keep, delete = _recycle_bin_quit_actions(qml_source)
    source = (
        HARNESS.replace("__ON_CLOSING__", _on_closing_body(qml_source))
        .replace("__FILE_EXIT__", exit_action)
        .replace("__KEEP__", keep)
        .replace("__DELETE__", delete)
    )

    engine = QQmlApplicationEngine()
    engine.loadData(source.encode("utf-8"))
    roots = engine.rootObjects()
    assert roots, "harness QML failed to load"
    win = roots[0]
    try:
        yield win
    finally:
        engine.deleteLater()
        del engine


def _closed(win) -> bool:
    """A close that was accepted hides the window; a vetoed one does not."""
    return not bool(win.property("visible"))


# --------------------------------------------------------------------------
# 1. File -> Exit reaches prepare_for_app_close()
# --------------------------------------------------------------------------


def test_file_exit_reaches_prepare_for_app_close(window):
    window.setProperty("prepareResult", True)
    window.fileExit()

    assert window.property("prepareCalls") == 1
    assert _closed(window)


# --------------------------------------------------------------------------
# 2. A refused prepare_for_app_close() cancels the exit
# --------------------------------------------------------------------------


def test_file_exit_is_vetoed_when_save_cannot_be_flushed(window):
    window.setProperty("prepareResult", False)
    window.fileExit()

    assert window.property("prepareCalls") == 1
    assert not _closed(window), "Exit must not close the window on a failed flush"
    # A vetoed close must not persist placement as if the app were exiting.
    assert window.property("placementSaves") == 0

    # Once the flush succeeds, the very same action closes.
    window.setProperty("prepareResult", True)
    window.fileExit()
    assert window.property("prepareCalls") == 2
    assert _closed(window)


# --------------------------------------------------------------------------
# 3. Both recycle-bin exit choices honour prepare_for_app_close()
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["keepAndQuit", "deleteAndQuit"])
def test_recycle_bin_exit_choices_honour_the_veto(window, action):
    window.setProperty("hasBins", True)
    window.setProperty("prepareResult", False)

    getattr(window, action)()

    assert window.property("prepareCalls") == 1, "recycle-bin exit bypassed the flush"
    assert not _closed(window)
    # The bin decision is remembered, so the prompt does not reappear.
    assert window.property("allowCloseWithRecycleBins") is True
    assert window.property("dialogCloseCalls") == 1


@pytest.mark.parametrize(
    "action,expected_cleanups", [("keepAndQuit", 0), ("deleteAndQuit", 1)]
)
def test_recycle_bin_exit_choices_close_when_flush_succeeds(
    window, action, expected_cleanups
):
    window.setProperty("hasBins", True)
    window.setProperty("prepareResult", True)

    getattr(window, action)()

    assert window.property("prepareCalls") == 1
    assert window.property("cleanupCalls") == expected_cleanups
    assert _closed(window)


# --------------------------------------------------------------------------
# 5. Existing recycle-bin close behaviour still works
# --------------------------------------------------------------------------


def test_close_with_bins_still_prompts_instead_of_exiting(window):
    window.setProperty("hasBins", True)
    window.setProperty("prepareResult", True)

    window.close()

    assert not _closed(window), "a pending recycle bin must veto the first close"
    assert window.property("dialogOpenCalls") == 1
    assert window.property("refreshCalls") == 1
    # The bin prompt runs *before* the flush, exactly as before.
    assert window.property("prepareCalls") == 0


def test_close_without_bins_is_unaffected(window):
    window.setProperty("hasBins", False)
    window.setProperty("prepareResult", True)

    window.close()

    assert window.property("dialogOpenCalls") == 0
    assert window.property("prepareCalls") == 1
    assert window.property("placementSaves") == 1
    assert _closed(window)


# --------------------------------------------------------------------------
# Supplementary source contract
# --------------------------------------------------------------------------


def test_no_lifecycle_exit_calls_qt_quit(qml_source):
    """Supplementary to the runtime tests above, not a substitute for them."""
    code = re.sub(r"//[^\n]*", "", qml_source)
    assert "Qt.quit()" not in code


# --------------------------------------------------------------------------
# 4. SIGINT uses the close-safety path once the window exists
# --------------------------------------------------------------------------


def test_sigint_closes_the_window_instead_of_quitting():
    """Ctrl-C must go through onClosing so a dirty session can veto it."""
    from unittest.mock import MagicMock

    from faststack.app import _make_sigint_close_handler

    app = MagicMock()
    window = MagicMock()
    handler = _make_sigint_close_handler(app, [window])

    handler(2, None)

    window.close.assert_called_once_with()
    app.quit.assert_not_called()


def test_sigint_before_the_window_exists_still_quits():
    """A Ctrl-C during startup must terminate, not be swallowed."""
    from unittest.mock import MagicMock

    from faststack.app import _make_sigint_close_handler

    app = MagicMock()
    holder: list = []
    handler = _make_sigint_close_handler(app, holder)

    handler(2, None)  # must not raise
    app.quit.assert_called_once_with()

    # Once QML has loaded, the same handler switches to the close path.
    window = MagicMock()
    holder.append(window)
    handler(2, None)
    window.close.assert_called_once_with()
    assert app.quit.call_count == 1


def test_sigint_falls_back_to_quit_if_closing_raises():
    from unittest.mock import MagicMock

    from faststack.app import _make_sigint_close_handler

    app = MagicMock()
    window = MagicMock()
    window.close.side_effect = RuntimeError("window already destroyed")
    handler = _make_sigint_close_handler(app, [window])

    handler(2, None)

    window.close.assert_called_once_with()
    app.quit.assert_called_once_with()


def test_main_installs_the_close_safe_sigint_handler():
    """main() must wire the handler up and populate the window holder."""
    import inspect

    from faststack import app

    source = inspect.getsource(app.main)
    assert "_make_sigint_close_handler(app, sigint_window_holder)" in source
    assert "sigint_window_holder.append(main_window)" in source
    assert "lambda *args: app.quit()" not in source


def test_sigint_veto_is_observable_end_to_end(window):
    """The handler's close() genuinely re-enters Main.qml's onClosing."""
    from unittest.mock import MagicMock

    from faststack.app import _make_sigint_close_handler

    app = MagicMock()
    handler = _make_sigint_close_handler(app, [window])

    window.setProperty("prepareResult", False)
    handler(2, None)
    assert window.property("prepareCalls") == 1
    assert not _closed(window), "Ctrl-C must not exit past a failed flush"
    app.quit.assert_not_called()

    window.setProperty("prepareResult", True)
    handler(2, None)
    assert _closed(window)
