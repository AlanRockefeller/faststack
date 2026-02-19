import pytest
from unittest.mock import Mock
from pathlib import Path
from faststack.ui.provider import UIState


def test_recycle_bin_detailed_text_formatting():
    # Mock AppController
    mock_controller = Mock()

    # Define sample recycle bin stats
    sample_stats = [
        {
            "path": "C:/images/image recycle bin",
            "count": 2,
            "jpg_count": 1,
            "raw_count": 1,
            "other_count": 0,
            "file_paths": ["image1.jpg", "image1.ARW"],
        },
        {
            "path": "D:/other/image recycle bin",
            "count": 1,
            "jpg_count": 0,
            "raw_count": 0,
            "other_count": 1,
            "file_paths": ["doc.txt"],
        },
    ]

    mock_controller.get_recycle_bin_stats.return_value = sample_stats

    # Initialize UIState
    ui_state = UIState(mock_controller)

    # Get detailed text
    detailed_text = ui_state.recycleBinDetailedText

    # Verify content
    assert "Directory: C:/images/image recycle bin" in detailed_text
    assert "  - image1.jpg" in detailed_text
    assert "  - image1.ARW" in detailed_text
    assert "Directory: D:/other/image recycle bin" in detailed_text
    assert "  - doc.txt" in detailed_text

    # Verify separators (trailing newline)
    assert detailed_text.count("  - ") == 3
    assert detailed_text.endswith("\n") or detailed_text.count("\n\n") > 0


def test_recycle_bin_detailed_text_empty():
    mock_controller = Mock()
    mock_controller.get_recycle_bin_stats.return_value = []

    ui_state = UIState(mock_controller)
    assert ui_state.recycleBinDetailedText == ""
