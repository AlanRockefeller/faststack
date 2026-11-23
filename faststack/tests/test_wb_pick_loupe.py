"""The white balance picker's clicks on the loupe (Components.qml).

While picking, the loupe shows the brush ring at the picker's own size, a
left click asks the controller to pick at that point of the displayed photo,
the rest of that press does not pan (the pick has already ended the picker by
then), and a right click cancels instead of starting a crop.

The loupe is loaded for real (offscreen) in a child process, so a Qt GUI
application never shares a process with the other tests' Qt objects.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

QML_DIR = Path(__file__).resolve().parents[1] / "qml"

_PROBE = textwrap.dedent(
    r"""
    import json, sys
    from PIL import Image
    from PySide6.QtCore import QCoreApplication, QObject, QPoint, QPointF, Qt, QUrl, Slot
    from PySide6.QtGui import QGuiApplication, QWheelEvent
    from PySide6.QtQml import QQmlApplicationEngine, QQmlPropertyMap
    from PySide6.QtQuick import QQuickItem, QQuickWindow  # noqa: F401 (registers types)
    from PySide6.QtTest import QTest

    app = QGuiApplication(sys.argv)
    qml_dir, img_path = sys.argv[1], sys.argv[2]
    Image.new("RGB", (400, 300), (128, 120, 100)).save(img_path)

    ui = QQmlPropertyMap()
    for key, value in {
        "isFolderLoaded": True, "imageCount": 1,
        "currentImageSource": QUrl.fromLocalFile(img_path).toString(),
        "isWbPicking": True, "isCropping": False, "isBrushMode": False,
        "brushPanHeld": False, "wbPickRadius": 0.05, "brushRadius": 0.2,
        "brushHardness": 0.5, "brushCropScale": [1, 1],
    }.items():
        ui.insert(key, value)

    class Controller(QObject):
        calls = []

        @Slot(float, float)
        def pick_white_balance_at(self, x, y):
            self.calls.append(["pick", round(x, 3), round(y, 3)])
            ui.insert("isWbPicking", False)  # a good pick ends the picker

        @Slot()
        def cancel_white_balance_pick(self):
            self.calls.append(["cancel"])
            ui.insert("isWbPicking", False)

        @Slot()
        def toggle_crop_mode(self):
            self.calls.append(["crop"])

        @Slot(float, float, bool)
        def start_local_stroke(self, x, y, erase):
            self.calls.append(["stroke"])

    controller = Controller()
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("uiState", ui)
    engine.rootContext().setContextProperty("controller", controller)
    loupe = QUrl.fromLocalFile(qml_dir + "/Components.qml").toString()
    engine.loadData(
        ("import QtQuick\nimport QtQuick.Window\n"
         "Window { width: 800; height: 600; visible: true\n"
         "  Loader { anchors.fill: parent; source: '" + loupe + "' } }").encode(),
        QUrl.fromLocalFile(qml_dir + "/probe.qml"),
    )
    window = engine.rootObjects()[0]

    def settle(n=20):
        for _ in range(n):
            app.processEvents()
            QTest.qWait(10)

    def walk(item):
        for child in item.childItems():
            yield child
            yield from walk(child)

    settle(40)
    items = list(walk(window.contentItem()))
    main_image = next(i for i in items
                      if i.metaObject().className().startswith("QQuickImage")
                      and "png" in i.property("source").toString())
    result = {"imageSize": [main_image.width(), main_image.height()]}

    def scene(fx, fy):
        p = main_image.mapToScene(QPointF(main_image.width() * fx, main_image.height() * fy))
        return QPoint(int(p.x()), int(p.y()))

    centre = scene(0.5, 0.5)
    QTest.mouseMove(window, centre)
    settle(5)
    ring = next(i for i in items
                if i.property("lineWidth") is not None and i.property("radius") is not None)
    result["ringVisible"] = ring.isVisible()
    result["ringRadius"] = ring.property("radius")

    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, scene(0.25, 0.75))
    settle(5)
    ui.insert("isWbPicking", True)
    settle(5)

    # Zoom in, so that a drag could pan.
    for _ in range(6):
        QCoreApplication.sendEvent(window, QWheelEvent(
            QPointF(centre), QPointF(centre), QPoint(0, 0), QPoint(0, 120),
            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False))
        settle(5)
    settle(60)

    def drag(button):
        before = main_image.mapToScene(QPointF(0, 0))
        QTest.mousePress(window, button, Qt.NoModifier, centre)
        for k in range(1, 8):
            QTest.mouseMove(window, QPoint(centre.x() + 15 * k, centre.y() + 10 * k))
            settle(2)
        QTest.mouseRelease(window, button, Qt.NoModifier,
                           QPoint(centre.x() + 105, centre.y() + 70))
        settle(10)
        after = main_image.mapToScene(QPointF(0, 0))
        return [after.x() - before.x(), after.y() - before.y()]

    result["dragAfterPick"] = drag(Qt.LeftButton)
    result["pickingAfterPick"] = bool(ui.value("isWbPicking"))
    result["plainDrag"] = drag(Qt.LeftButton)  # picker off: an ordinary pan
    ui.insert("isWbPicking", True)
    settle(5)
    result["rightDragWhilePicking"] = drag(Qt.RightButton)
    result["pickingAfterRightClick"] = bool(ui.value("isWbPicking"))
    result["dragAfterCancel"] = drag(Qt.LeftButton)  # picker off: pans again

    # Pick with the left button, then press and let go of the right while
    # the left is still down: still the picker's gesture, so no crop and no
    # pan, although the pick has already ended the picker.
    ui.insert("isWbPicking", True)
    settle(5)
    before = main_image.mapToScene(QPointF(0, 0))
    QTest.mousePress(window, Qt.LeftButton, Qt.NoModifier, centre)
    settle(2)
    QTest.mousePress(window, Qt.RightButton, Qt.NoModifier, centre)
    settle(2)
    QTest.mouseRelease(window, Qt.RightButton, Qt.NoModifier, centre)
    for k in range(1, 8):
        QTest.mouseMove(window, QPoint(centre.x() + 15 * k, centre.y() + 10 * k))
        settle(2)
    QTest.mouseRelease(window, Qt.LeftButton, Qt.NoModifier,
                       QPoint(centre.x() + 105, centre.y() + 70))
    settle(10)
    after = main_image.mapToScene(QPointF(0, 0))
    result["chordAfterPick"] = [after.x() - before.x(), after.y() - before.y()]
    result["calls"] = controller.calls
    print("RESULT " + json.dumps(result))
    """
)


@pytest.fixture(scope="module")
def probe(tmp_path_factory) -> dict:
    tmp_path = tmp_path_factory.mktemp("wb_pick_loupe")
    script = tmp_path / "probe.py"
    script.write_text(_PROBE, encoding="utf-8")
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["HOME"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(script), str(QML_DIR), str(tmp_path / "photo.png")],
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


def test_ring_shows_at_the_pickers_size(probe):
    assert probe["imageSize"] == [400.0, 300.0]
    assert probe["ringVisible"]
    # 5% of the 400x300 photo's 500px diagonal, not the 20% brush size.
    assert probe["ringRadius"] == pytest.approx(25.0)


def test_left_click_picks_at_the_clicked_point(probe):
    picks = [c for c in probe["calls"] if c[0] == "pick"]
    assert picks[0] == [
        "pick",
        pytest.approx(0.25, abs=0.01),
        pytest.approx(0.75, abs=0.01),
    ]
    assert picks[1] == [
        "pick",
        pytest.approx(0.5, abs=0.01),
        pytest.approx(0.5, abs=0.01),
    ]


def test_the_press_that_picked_does_not_pan(probe):
    assert not probe["pickingAfterPick"]
    assert probe["dragAfterPick"] == [0.0, 0.0]
    # The same drag with the picker off does pan, so the zoom let it.
    assert abs(probe["plainDrag"][0]) > 20 and abs(probe["plainDrag"][1]) > 20


def test_right_click_cancels_instead_of_cropping(probe):
    assert [c[0] for c in probe["calls"]] == ["pick", "pick", "cancel", "pick"]
    assert ["crop"] not in probe["calls"]
    assert ["stroke"] not in probe["calls"]
    assert probe["rightDragWhilePicking"] == [0.0, 0.0]
    # Once cancelled, a drag pans as usual.
    assert not probe["pickingAfterRightClick"]
    assert abs(probe["dragAfterCancel"][0]) > 20


def test_second_button_during_a_pick_neither_crops_nor_pans(probe):
    # The calls check above covers the crop; this covers the pan.
    assert probe["chordAfterPick"] == [0.0, 0.0]
