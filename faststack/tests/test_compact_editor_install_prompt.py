"""Esc dismisses the compact editor's model download prompt (Bkgd / Halo).

A Popup closes on Esc only while it has the keyboard focus. Pressing Bkgd
(or Halo) the first time opens the prompt, and the button's click handler
used to hand the focus straight back to the editor's key handler. The prompt
then ignored Esc. Its ``opened`` stays false until the popup's enter
transition ends, so the handler has to test ``visible`` instead.

The window is loaded for real (offscreen) in a child process, so a Qt GUI
application never shares a process with the other tests' Qt objects.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

QML = Path(__file__).resolve().parents[1] / "qml" / "CompactEditorWindow.qml"

_PROBE = textwrap.dedent(
    r"""
    import json, sys
    from PySide6.QtCore import QCoreApplication, QObject, QPointF, Qt, QUrl, Slot
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine, QQmlPropertyMap
    from PySide6.QtQuick import QQuickItem, QQuickWindow  # noqa: F401 (registers types)
    from PySide6.QtTest import QTest

    QCoreApplication.setOrganizationName("faststack-prompt-test")
    QCoreApplication.setApplicationName("faststack-prompt-test")
    app = QGuiApplication(sys.argv)

    ui = QQmlPropertyMap()
    for key, value in {
        "isEditorOpen": True, "isEditorExpanded": False, "isBrushMode": False,
        "brushTool": "", "localLayers": [], "stackInputs": [], "haloState": {},
        "depthState": {"promptVisible": False}, "isWbPicking": False,
    }.items():
        ui.insert(key, value)

    class Controller(QObject):
        calls = []

        @Slot()
        def select_background(self):
            self.calls.append("select_background")
            ui.insert("depthState", {"promptVisible": True, "modelNeeded": True,
                                     "runtimeNeeded": False, "downloadMB": 100})

        @Slot()
        def depth_install_decline(self):
            self.calls.append("decline")
            ui.insert("depthState", {"promptVisible": False})

        @Slot(QObject)
        def release_auxiliary_navigation_hold(self, window):
            pass

        @Slot(int, int, str)
        def handle_key_from_compact_editor(self, key, modifiers, text):
            self.calls.append("forwarded key")

    controller = Controller()
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("uiState", ui)
    # The window's start-up asks the controller for screen geometry and the
    # like; with no controller it skips that, and the stub is wired after.
    engine.rootContext().setContextProperty("controller", None)
    engine.load(QUrl.fromLocalFile(sys.argv[1]))
    window = engine.rootObjects()[0]
    window.setProperty("visible", True)
    window.setProperty("height", 900)
    window.setProperty("controllerRef", controller)

    def settle(n=20):
        for _ in range(n):
            app.processEvents()
            QTest.qWait(5)

    def walk(item):
        for child in item.childItems():
            yield child
            yield from walk(child)

    settle(40)
    window.requestActivate()
    settle(10)
    button = next(i for i in walk(window.contentItem())
                  if i.metaObject().className().startswith("Button")
                  and i.property("contentItem") is not None
                  and "Bkgd" in str(i.property("contentItem").property("text")))
    centre = button.mapToScene(QPointF(button.width() / 2, button.height() / 2))
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, centre.toPoint())
    settle(100)  # past the popup's enter transition

    prompt = next(o for o in window.findChildren(QObject)
                  if "Popup" in o.metaObject().className()
                  and o.property("title") == "Download background selection?")
    result = {"openedAfterClick": bool(prompt.property("opened")),
              "promptHasFocus": bool(prompt.property("activeFocus"))}
    QTest.keyClick(window, Qt.Key_Escape)
    settle(100)  # past the exit transition, when it reports the decline
    result["openedAfterEsc"] = bool(prompt.property("opened"))
    result["calls"] = controller.calls
    result["editorOpen"] = bool(ui.value("isEditorOpen"))
    print("RESULT " + json.dumps(result))
    """
)


@pytest.fixture(scope="module")
def probe(tmp_path_factory) -> dict:
    tmp_path = tmp_path_factory.mktemp("install_prompt")
    script = tmp_path / "probe.py"
    script.write_text(_PROBE, encoding="utf-8")
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["QT_QUICK_CONTROLS_STYLE"] = "Material"
    # The window's Settings must not touch the developer's real config.
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["HOME"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(script), str(QML)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT ") :])
    pytest.fail(
        f"probe produced no result\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr[-4000:]}"
    )


def test_bkgd_prompt_takes_the_keyboard_focus(probe):
    assert probe["openedAfterClick"]
    assert probe["promptHasFocus"]


def test_esc_declines_the_prompt_and_leaves_the_editor_open(probe):
    assert not probe["openedAfterEsc"]
    assert probe["calls"] == ["select_background", "decline"]
    assert probe["editorOpen"]
