from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
QUALITY_PAGE = REPO_ROOT / "tools" / "quality-harness" / "page_template.html"
EDITOR_QML = REPO_ROOT / "faststack" / "qml" / "ImageEditorDialog.qml"


def test_every_visible_quality_choice_responds_to_click():
    """Execute the shipped choice wiring and click every visible answer."""
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtQml import QJSEngine

    page = QUALITY_PAGE.read_text(encoding="utf-8")
    choice_ids = re.findall(r'<button class="choice" id="([^"]+)"', page)
    expected = {
        "pick1": "1",
        "pick2": "2",
        "pickSame": "same",
        "pickDiffer": "differ",
    }
    assert set(choice_ids) == set(expected)

    wiring = "\n".join(line for line in page.splitlines() if line.startswith('$("pick'))
    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None
    engine = QJSEngine()
    result = engine.evaluate(
        """
        var calls = [];
        var elements = {};
        function $(id) {
            if (!elements[id]) {
                elements[id] = {
                    clickHandler: null,
                    addEventListener: function(kind, handler) {
                        if (kind === "click") this.clickHandler = handler;
                    },
                    click: function() {
                        if (this.clickHandler) this.clickHandler();
                    }
                };
            }
            return elements[id];
        }
        function setPick(value) { calls.push(value); }
        """
        + wiring
    )
    assert not result.isError(), result.toString()

    for choice_id in choice_ids:
        engine.evaluate("calls = [];")
        result = engine.evaluate(
            f'elements["{choice_id}"].click(); JSON.stringify(calls);'
        )
        assert result.toString() == f'["{expected[choice_id]}"]'


def _relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(first: str, second: str) -> float:
    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def _theme_colors(qml: str, property_name: str) -> tuple[str, str]:
    match = re.search(
        rf"{property_name}: darkTheme \? \"(#[0-9a-fA-F]{{6}})\""
        rf" : \"(#[0-9a-fA-F]{{6}})\"",
        qml,
    )
    assert match, f"missing dark/light colors for {property_name}"
    return match.group(1), match.group(2)


def test_editor_slider_values_keep_accessible_theme_contrast():
    qml = EDITOR_QML.read_text(encoding="utf-8")
    backgrounds = ("#000000", "#ffffff")

    for property_name in ("sliderValueColor", "sliderZeroValueColor"):
        colors = _theme_colors(qml, property_name)
        for color, background in zip(colors, backgrounds):
            assert _contrast_ratio(color, background) >= 4.5

    handle_colors = _theme_colors(qml, "sliderHandleColor")
    for color, background in zip(handle_colors, backgrounds):
        assert _contrast_ratio(color, background) >= 3.0
