from pathlib import Path


def test_main_qml_coerces_runtime_values_before_array_operations():
    """Keep startup bindings from calling JS array APIs on raw backend values."""
    qml_path = Path(__file__).resolve().parents[1] / "qml" / "Main.qml"
    qml_text = qml_path.read_text(encoding="utf-8")

    assert "function toArray(value)" in qml_text
    assert "function stringOrEmpty(value)" in qml_text
    assert "function itemsWithStatus(items, status)" in qml_text
    assert "value === null || value === undefined" in qml_text
    assert "if (!value)" not in qml_text

    assert "root.uiStateRef.variantBadges.length" not in qml_text
    assert "recycleBinCleanupDialog.binInfo.filter(" not in qml_text
    assert "recycleBinCleanupDialog.binInfo.length" not in qml_text
    # exifBrief is no longer coerced at each use site; it now goes through the
    # exifBrief* helpers, which coerce with stringOrEmpty internally. Assert
    # that indirection instead of the old literal call.
    import re

    for match in re.findall(r"root\.\w+\(root\.uiStateRef\.exifBrief\)", qml_text):
        helper = match[len("root.") : match.index("(")]
        body_start = qml_text.index(f"function {helper}(")
        body = qml_text[body_start : body_start + 400]
        assert "root.stringOrEmpty(" in body, f"{helper} does not coerce its input"

    assert "root.uiStateRef.exifBrief." not in qml_text
