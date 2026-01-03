
import pytest
import numpy as np
from PIL import Image
from faststack.imaging.editor import ImageEditor


def test_auto_levels_pins_highlights_if_clipped():
    editor = ImageEditor()
    # 10x10 image
    w, h = 10, 10
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:] = 100
    
    # Clip Blue: Set last pixel to 255
    arr[9, 9, 2] = 255
    
    img = Image.fromarray(arr, 'RGB')
    editor.original_image = img
    editor._preview_image = img
    
    # Use threshold 0.0 to make p_low deterministic (min value)
    # This prevents fragility with per-channel percentiles on small arrays
    blacks, whites, p_low, p_high = editor.auto_levels(threshold_percent=0.0)
    
    # 1 pixel of 255 in 100 is 1%. Eps (from threshold 0.0) would be 0.0.
    # Actually logic is eps = min(threshold, 0.01). If threshold 0.0, eps=0.0.
    # 1% > 0.0% -> Pins.
    
    assert p_high == 255.0
    assert whites == 0.0
    
    # p_low should be the strict minimum (100)
    assert p_low == 100.0

def test_auto_levels_pins_shadows_if_clipped():
    editor = ImageEditor()
    w, h = 10, 10
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:] = 100
    
    # Clip Red shadow: 1 pixel at 0.
    arr[0, 0, 0] = 0
    
    img = Image.fromarray(arr, 'RGB')
    editor.original_image = img
    editor._preview_image = img
    
    # Threshold 0.0 -> eps=0.0. 1% detected > 0.0 -> Pins.
    blacks, whites, p_low, p_high = editor.auto_levels(threshold_percent=0.0)
    
    assert p_low == 0.0 
    assert blacks == 0.0
    
    # Whites should be normal (max is 100)
    assert p_high == 100.0
    assert whites == (255.0 - 100.0) / 40.0

def test_auto_levels_tiny_hot_pixel_ignored():
    """
    Verify that a very small number of clipped pixels (below eps check)
    does NOT trigger pinning, and does NOT get picked up by percentile 
    if strictly below the threshold.
    """
    editor = ImageEditor()
    # 200x200 = 40,000 pixels
    w, h = 200, 200
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    
    # Base: 150
    arr[:] = 150
    
    # Set top ~2.5% pixels to 200 (1000 pixels)
    # This ensures the 99.9th percentile lands on 200, not 150.
    # Flattening for easier assignment
    flat = arr.reshape(-1, 3)
    flat[0:1000, :] = 200
    arr = flat.reshape(h, w, 3)
    
    # Add ONE hot pixel at 255 in Red channel
    arr[0, 0, 0] = 255
    
    img = Image.fromarray(arr, 'RGB')
    editor.original_image = img
    editor._preview_image = img
    
    # Threshold 0.1%. Eps = 0.01%.
    # 1 pixel / 40000 = 0.0025%.
    # 0.0025% < 0.01%. Should NOT pin.
    
    blacks, whites, p_low, p_high = editor.auto_levels(threshold_percent=0.1)
    
    # p_high should be 200 (from the 200-level plateau), ignoring the 255.
    assert p_high == 200.0
    assert p_high != 255.0 # Check explicitly not pinned
    assert whites > 0.0

def test_auto_levels_degenerate_image():
    editor = ImageEditor()
    w, h = 10, 10
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:] = 128
    
    img = Image.fromarray(arr, 'RGB')
    editor.original_image = img
    editor._preview_image = img
    
    blacks, whites, p_low, p_high = editor.auto_levels(threshold_percent=0.1)
    
    assert p_high == 128.0
    assert p_low == 128.0
    assert blacks == 0.0
    assert whites == 0.0

def test_auto_levels_normal_range():
    editor = ImageEditor()
    w, h = 10, 10
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:] = 128
    arr[0, 0, :] = 50  # Low
    arr[9, 9, :] = 200 # High
    
    img = Image.fromarray(arr, 'RGB')
    editor.original_image = img
    editor._preview_image = img
    
    blacks, whites, p_low, p_high = editor.auto_levels(threshold_percent=0.0)
    
    assert p_high == 200.0
    assert p_low == 50.0
    
    assert p_high != 255.0 # Not pinned
    assert p_low != 0.0    # Not pinned
    
    assert whites == (255.0 - 200.0) / 40.0
    assert blacks == -50.0 / 40.0

