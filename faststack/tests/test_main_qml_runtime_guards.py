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
    assert "root.stringOrEmpty(root.uiStateRef.exifBrief)" in qml_text
