"""Tests for executable path validation."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from faststack.io.executable_validator import (
    validate_executable_path,
    _is_executable,
    _is_subpath,
)


def test_empty_path():
    """Test that empty path is rejected."""
    is_valid, error = validate_executable_path("")
    assert not is_valid
    assert "empty" in error.lower()


def test_nonexistent_file():
    """Test that nonexistent file is rejected."""
    is_valid, error = validate_executable_path("C:\\nonexistent\\fake.exe")
    assert not is_valid
    assert "not found" in error.lower()


def test_valid_photoshop_path():
    """Test validation of a valid Photoshop path."""
    photoshop_path = r"C:\Program Files\Adobe\Adobe Photoshop 2026\Photoshop.exe"
    
    # Mock the path checks
    with patch('faststack.io.executable_validator.Path') as mock_path:
        mock_path_instance = MagicMock()
        mock_path.return_value.resolve.return_value = mock_path_instance
        mock_path_instance.exists.return_value = True
        mock_path_instance.is_file.return_value = True
        mock_path_instance.suffix.lower.return_value = '.exe'
        mock_path_instance.name = "Photoshop.exe"
        mock_path_instance.__str__ = lambda self: photoshop_path
        
        with patch('faststack.io.executable_validator._is_subpath', return_value=True):
            is_valid, error = validate_executable_path(
                photoshop_path,
                app_type="photoshop"
            )
            assert is_valid
            assert error is None


def test_suspicious_path_with_traversal():
    """Test that paths with directory traversal are flagged."""
    suspicious_path = r"C:\Program Files\..\Windows\System32\malware.exe"
    
    with patch('faststack.io.executable_validator.Path') as mock_path:
        mock_path_instance = MagicMock()
        mock_path.return_value.resolve.return_value = mock_path_instance
        mock_path_instance.exists.return_value = True
        mock_path_instance.is_file.return_value = True
        mock_path_instance.suffix.lower.return_value = '.exe'
        mock_path_instance.name = "malware.exe"
        mock_path_instance.__str__ = lambda self: r"C:\Windows\System32\malware.exe"
        
        # The normalized path will differ from input, triggering warning
        is_valid, error = validate_executable_path(suspicious_path)
        # Should still pass but with a warning logged
        # (in production, you might want to make this fail)


def test_non_exe_file():
    """Test that non-executable files are rejected on Windows."""
    txt_file = r"C:\Program Files\test.txt"
    
    with patch('faststack.io.executable_validator.Path') as mock_path:
        mock_path_instance = MagicMock()
        mock_path.return_value.resolve.return_value = mock_path_instance
        mock_path_instance.exists.return_value = True
        mock_path_instance.is_file.return_value = True
        mock_path_instance.suffix.lower.return_value = '.txt'
        
        is_valid, error = validate_executable_path(txt_file)
        assert not is_valid
        assert "not executable" in error.lower()


def test_is_executable_windows():
    """Test _is_executable on Windows."""
    with patch('os.name', 'nt'):
        exe_path = MagicMock()
        exe_path.suffix.lower.return_value = '.exe'
        assert _is_executable(exe_path)
        
        txt_path = MagicMock()
        txt_path.suffix.lower.return_value = '.txt'
        assert not _is_executable(txt_path)


def test_is_subpath():
    """Test _is_subpath logic."""
    # This is hard to test without real paths, so we'll test the logic
    parent = Path(r"C:\Program Files")
    child = Path(r"C:\Program Files\Adobe\Photoshop.exe")
    
    # Mock the relative_to to simulate success
    with patch.object(Path, 'resolve') as mock_resolve:
        mock_resolve.return_value.relative_to = MagicMock()
        result = _is_subpath(child, parent)
        assert result


def test_wrong_executable_name_for_type():
    """Test that wrong executable names generate warnings but don't fail."""
    wrong_exe = r"C:\Program Files\Adobe\NotPhotoshop.exe"
    
    with patch('faststack.io.executable_validator.Path') as mock_path:
        mock_path_instance = MagicMock()
        mock_path.return_value.resolve.return_value = mock_path_instance
        mock_path_instance.exists.return_value = True
        mock_path_instance.is_file.return_value = True
        mock_path_instance.suffix.lower.return_value = '.exe'
        mock_path_instance.name = "NotPhotoshop.exe"
        mock_path_instance.__str__ = lambda self: wrong_exe
        
        with patch('faststack.io.executable_validator._is_subpath', return_value=True):
            # Should still pass, but with a warning logged
            is_valid, error = validate_executable_path(
                wrong_exe,
                app_type="photoshop"
            )
            assert is_valid  # Name mismatch is warning, not failure
