"""Tests for permanent delete logic in faststack.io.deletion."""

import pytest
from pathlib import Path
from unittest.mock import Mock, patch

# Import the standalone module, avoiding heavy app imports
from faststack.io.deletion import (
    ensure_recycle_bin_dir,
    confirm_permanent_delete,
    permanently_delete_image_files
)

class MockImageFile:
    """Simple mock for ImageFile."""
    def __init__(self, jpg_path: Path, raw_path: Path = None):
        self.path = jpg_path
        self.raw_pair = raw_path
        self.is_video = False

class TestEnsureRecycleBinDir:
    def test_creation_success(self, tmp_path):
        """Should return True and create directory when successful."""
        recycle_bin = tmp_path / "RecycleBin"
        assert not recycle_bin.exists()
        
        result = ensure_recycle_bin_dir(recycle_bin)
        
        assert result is True
        assert recycle_bin.exists()
        assert recycle_bin.is_dir()

    def test_creation_failure(self, tmp_path):
        """Should return False when creation raises PermissionError."""
        recycle_bin = tmp_path / "RecycleBin"
        
        with patch.object(Path, "mkdir", side_effect=PermissionError("Mock perm error")):
            result = ensure_recycle_bin_dir(recycle_bin)
            assert result is False

class TestConfirmPermanentDelete:
    def test_confirm_yes(self):
        """Should return True when user accepts dialog."""
        mock_img = MockImageFile(Path("test.jpg"))
        
        with patch("faststack.io.deletion.QMessageBox") as MockMSG:
            instance = MockMSG.return_value
            instance.exec.return_value = 0 
            
            mock_delete_btn = Mock(name="DeleteButton")
            mock_cancel_btn = Mock(name="CancelButton")
            
            instance.addButton.side_effect = [mock_delete_btn, mock_cancel_btn]
            instance.clickedButton.return_value = mock_delete_btn
            
            result = confirm_permanent_delete(mock_img)
            assert result is True

    def test_confirm_no(self):
        """Should return False when user cancels."""
        mock_img = MockImageFile(Path("test.jpg"))
        
        with patch("faststack.io.deletion.QMessageBox") as MockMSG:
            instance = MockMSG.return_value
            instance.exec.return_value = 0
            
            mock_delete_btn = Mock(name="DeleteButton")
            mock_cancel_btn = Mock(name="CancelButton")
            
            instance.addButton.side_effect = [mock_delete_btn, mock_cancel_btn]
            instance.clickedButton.return_value = mock_cancel_btn
            
            result = confirm_permanent_delete(mock_img)
            assert result is False

class TestPermanentlyDeleteImageFiles:
    def test_delete_success(self, tmp_path):
        """Should delete files and return True."""
        jpg = tmp_path / "img.jpg"
        raw = tmp_path / "img.orf"
        jpg.touch()
        raw.touch()
        
        img = MockImageFile(jpg, raw)
        
        result = permanently_delete_image_files(img)
        
        assert result is True
        assert not jpg.exists()
        assert not raw.exists()

    def test_delete_jpg_only(self, tmp_path):
        """Should delete JPG if no RAW pair."""
        jpg = tmp_path / "img.jpg"
        jpg.touch()
        img = MockImageFile(jpg, None)
        
        result = permanently_delete_image_files(img)
        
        assert result is True
        assert not jpg.exists()

    def test_delete_handles_missing_files(self, tmp_path):
        """Should return False if files don't exist."""
        jpg = tmp_path / "missing.jpg"
        img = MockImageFile(jpg, None)
        
        result = permanently_delete_image_files(img)
        
        assert result is False

    def test_delete_failure_logging(self, tmp_path):
        """Should log errors and return False if deletion fails."""
        jpg = tmp_path / "protected.jpg"
        jpg.touch()
        img = MockImageFile(jpg, None)
        
        with patch.object(Path, "unlink", side_effect=OSError("Protected")):
            with patch("faststack.io.deletion.log") as mock_log:
                result = permanently_delete_image_files(img)
                
                assert result is False
                assert jpg.exists()
                mock_log.error.assert_called()
