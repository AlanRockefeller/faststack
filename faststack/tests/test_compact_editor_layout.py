"""The compact editor can be made thin, and its Auto buttons take every click.

Two problems this guards against:

* The panel's minimum width was measured from the widest row (the header's
  buttons), about 350px. It can now go to about half that: the header wraps,
  labels elide, and nothing pushes past the right edge at the minimum.
* With nothing to scroll the scroll bar was faded out but still covered the
  right 16px of the content, so clicks on the right part of the LIGHT / COLOR
  "Auto" buttons went to the invisible bar.

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
    import json, os, sys
    from PySide6.QtCore import QCoreApplication, QPoint, QPointF, Qt, QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickItem, QQuickWindow  # noqa: F401 (registers types)
    from PySide6.QtTest import QTest

    QCoreApplication.setOrganizationName("faststack-layout-test")
    QCoreApplication.setApplicationName("faststack-layout-test")
    app = QGuiApplication(sys.argv)
    qml_path, mode, height = sys.argv[1], sys.argv[2], int(sys.argv[3])

    ui = {
        "isEditorOpen": True, "isEditorExpanded": False,
        "stackInputs": [{"name": "a.jpg", "label": "Frame 1 a.jpg", "short": "1"}],
        "stackInputName": "a.jpg", "haloState": {}, "depthState": {},
        "isBrushMode": mode != "global", "brushTool": "dodge" if mode == "global" else mode,
        "localLayers": [], "localBrushSummary": "", "localEditingLayerId": "",
        "brushRadius": 0.05, "brushHardness": 0.5, "localOverlayVisible": False,
    }
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("uiState", ui)
    engine.rootContext().setContextProperty("controller", None)
    engine.load(QUrl.fromLocalFile(qml_path))
    window = engine.rootObjects()[0]
    window.setProperty("visible", True)
    window.setProperty("minimumHeight", 100)
    window.setProperty("height", height)

    def settle():
        for _ in range(20):
            app.processEvents()

    def walk(item):
        for child in item.childItems():
            yield child
            yield from walk(child)

    def by_class(prefix):
        return next(i for i in walk(window.contentItem())
                    if i.metaObject().className().startswith(prefix))

    def by_text(text):
        return next(i for i in walk(window.contentItem())
                    if i.metaObject().className().startswith("Button")
                    and i.property("contentItem") is not None
                    and i.property("contentItem").property("text") == text)

    settle()
    result = {"defaultWidth": window.width(), "minimumWidth": window.property("minimumWidth")}
    header = next((i for i in walk(window.contentItem()) if i.property("toolsLineWidth") is not None), None)
    result["header"] = None
    if header is not None:
        tools = next(i for i in header.childItems() if i.metaObject().className() == "QQuickFlow")
        corner = next(i for i in header.childItems() if i.metaObject().className() == "QQuickRow")
        result["header"] = {
            "oneLine": bool(header.property("oneLine")),
            "toolsHeight": tools.height(), "toolsY": tools.y(),
            "buttonHeight": corner.height(),
        }

    # Auto buttons: click a grid across each; count the clicks that land.
    scrollbar = by_class("ScrollBar")
    result["canScroll"] = scrollbar.property("size") < 1.0
    flick = by_class("QQuickFlickable")
    result["autoClicks"] = {}
    for label_row in ("LIGHT", "COLOR"):
        heading = next(i for i in walk(window.contentItem())
                       if i.metaObject().className() == "QQuickText" and i.property("text") == label_row)
        button = next(i for i in heading.parentItem().childItems()
                      if i.metaObject().className().startswith("Button"))
        if result["canScroll"]:
            rel = button.mapToItem(flick, QPointF(0, 0)).y()
            flick.setProperty("contentY", max(0.0, flick.property("contentY") + rel - 40))
            settle()
        hits = []
        button.clicked.connect(lambda: hits.append(1))
        origin = button.mapToScene(QPointF(0, 0))
        tries = 0
        for fx in (0.1, 0.3, 0.5, 0.7, 0.9):
            for fy in (0.25, 0.5, 0.75):
                tries += 1
                QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                                 QPoint(int(origin.x() + button.width() * fx),
                                        int(origin.y() + button.height() * fy)), 300)
                for _ in range(3):
                    app.processEvents()
        result["autoClicks"][label_row] = [len(hits), tries]

    # At the minimum width nothing but (elided) text reaches past the column.
    window.setProperty("width", result["minimumWidth"])
    settle()
    column = next(i for i in walk(window.contentItem())
                  if i.metaObject().className() == "QQuickColumnLayout" and i.property("spacing") == 6)
    overflow = []
    for item in walk(column):
        if not item.isVisible() or item.width() <= 0 or item.metaObject().className() == "QQuickText":
            continue
        right = item.mapToItem(column, QPointF(item.width(), 0)).x()
        if right > column.width() + 0.5:
            overflow.append([item.metaObject().className(), round(right), round(column.width())])
    result["overflowAtMinimum"] = overflow
    save = by_text("Save").property("contentItem")
    result["saveLabelHeight"] = save.height()
    print("RESULT " + json.dumps(result))
    """
)


def _probe(tmp_path, mode: str, height: int) -> dict:
    script = tmp_path / "probe.py"
    script.write_text(_PROBE, encoding="utf-8")
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["QT_QUICK_CONTROLS_STYLE"] = "Material"
    # The window's Settings must not touch the developer's real config.
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["HOME"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(script), str(QML), mode, str(height)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT ") :])
    pytest.fail(f"probe produced no result\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr[-4000:]}")


@pytest.mark.parametrize("mode", ["global", "dodge", "stack", "halo"])
def test_compact_editor_goes_to_half_width_without_overflow(tmp_path, mode):
    result = _probe(tmp_path, mode, 680)
    # About half the previous ~350px floor.
    assert result["minimumWidth"] <= 180
    assert result["overflowAtMinimum"] == []
    # The label box of a 28px Material button is not squeezed to nothing
    # (an elided Text in a zero-height box draws no text at all).
    assert result["saveLabelHeight"] > 0


def test_compact_editor_header_wraps_consistently(tmp_path):
    # Whether the default width fits the header on one line depends on the
    # fonts, so check the two layouts are consistent rather than which one.
    header = _probe(tmp_path, "global", 680)["header"]
    assert header is not None, "header tools are not in a wrapping Flow"
    if header["oneLine"]:
        # One line really is one row (no rounding onto a second).
        assert header["toolsHeight"] <= header["buttonHeight"] + 0.5
        assert header["toolsY"] == 0
    else:
        # Wrapped: the tools sit below the rotate / expand / close buttons.
        assert header["toolsY"] >= header["buttonHeight"]


@pytest.mark.parametrize("height", [900, 300], ids=["nothing-to-scroll", "scrolling"])
def test_auto_buttons_take_every_click(tmp_path, height):
    result = _probe(tmp_path, "global", height)
    assert result["canScroll"] == (height == 300)
    for row, (hits, tries) in result["autoClicks"].items():
        assert hits == tries, f"{row} Auto: {hits}/{tries} clicks reached the button"
