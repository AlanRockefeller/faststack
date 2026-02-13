from unittest.mock import MagicMock, patch
from pathlib import Path
import pytest
from faststack.app import AppController
from faststack.models import ImageFile

@pytest.fixture
def mock_controller():
    # Mock dependencies
    engine = MagicMock()
    
    # Instantiate controller with required args
    with patch('faststack.app.Watcher'), \
         patch('faststack.app.SidecarManager'), \
         patch('faststack.app.ImageEditor'), \
         patch('faststack.app.ByteLRUCache'), \
         patch('faststack.app.Prefetcher'), \
         patch('faststack.app.ThumbnailCache'), \
         patch('faststack.app.PathResolver'), \
         patch('faststack.app.ThumbnailPrefetcher'), \
         patch('faststack.app.ThumbnailModel'), \
         patch('faststack.app.ThumbnailProvider'), \
         patch('faststack.app.concurrent.futures.ThreadPoolExecutor'):
        try:
            controller = AppController(image_dir=Path("c:/images"), engine=engine)
        except Exception as e:
            import traceback
            traceback.print_exc()
            raise e
    
    # Mock image files
    img1 = ImageFile(path=Path("c:/images/img1.jpg"), raw_pair=Path("c:/images/img1.CR2"))
    img2 = ImageFile(path=Path("c:/images/img2.jpg"), raw_pair=None) # No RAW
    controller.image_files = [img1, img2]
    
    # Define a stack covering both images
    controller.stacks = [[0, 1]]
    
    # Mock dependencies
    controller._launch_helicon_with_files = MagicMock(return_value=True)
    controller.clear_all_stacks = MagicMock()
    controller.sync_ui_state = MagicMock()
    
    return controller

def test_launch_helicon_raw_preferred(mock_controller):
    """Test launching with use_raw=True (default)"""
    mock_controller.launch_helicon(use_raw=True)
    
    # Should select RAW for img1, JPG for img2 (fallback)
    expected_files = [
        Path("c:/images/img1.CR2"),
        Path("c:/images/img2.jpg")
    ]
    
    mock_controller._launch_helicon_with_files.assert_called_once()
    call_args = mock_controller._launch_helicon_with_files.call_args[0][0]
    assert call_args == expected_files

def test_launch_helicon_jpg_only(mock_controller):
    """Test launching with use_raw=False"""
    mock_controller.launch_helicon(use_raw=False)
    
    # Should select JPG for both
    expected_files = [
        Path("c:/images/img1.jpg"),
        Path("c:/images/img2.jpg")
    ]
    
    mock_controller._launch_helicon_with_files.assert_called_once()
    call_args = mock_controller._launch_helicon_with_files.call_args[0][0]
    assert call_args == expected_files

def test_launch_helicon_no_stacks(mock_controller):
    """Test launching with no stacks defined"""
    mock_controller.stacks = []
    mock_controller.launch_helicon()
    
    mock_controller._launch_helicon_with_files.assert_not_called()

def test_uistate_delegation(mock_controller):
    """Test that UIState correctly delegates launch_helicon with the use_raw argument"""
    from faststack.ui.provider import UIState
    ui_state = UIState(mock_controller)
    
    # Test True
    ui_state.launch_helicon(True)
    mock_controller._launch_helicon_with_files.assert_called()
    assert mock_controller._launch_helicon_with_files.call_args[0][0][0].suffix == ".CR2"
    
    # Reset mock
    mock_controller._launch_helicon_with_files.reset_mock()
    
    # Test False
    ui_state.launch_helicon(False)
    mock_controller._launch_helicon_with_files.assert_called()
    assert mock_controller._launch_helicon_with_files.call_args[0][0][0].suffix == ".jpg"
