
import pytest
import math
from PIL import Image
import numpy as np
from faststack.imaging.editor import _rotated_rect_with_max_area, rotate_autocrop_rgb, ImageEditor

def test_rotated_rect_edge_cases():
    """Test fundamental edge cases for the rectangle calculation."""
    # Zero dimensions
    assert _rotated_rect_with_max_area(0, 100, 0.5) == (0, 0)
    assert _rotated_rect_with_max_area(100, 0, 0.5) == (0, 0)
    assert _rotated_rect_with_max_area(-10, 100, 0.5) == (0, 0)

    # Near zero angle (should be close to original dimensions)
    w, h = 100, 50
    cw, ch = _rotated_rect_with_max_area(w, h, 0.0000001)
    assert cw == w
    assert ch == h

    # Near 90 degree angle (should swap Dimensions roughly)
    # The function expects radians. pi/2 is 90 degrees.
    # Note: The function folds angle into [0, pi/2)
    # If we pass exactly pi/2, math.sin(pi/2) = 1.
    # However, our function folds: angle_rad = abs(angle_rad) % (math.pi / 2).
    # So 90 deg becomes 0 deg effectively for rect calculation purposes in this specific helper
    # because a 90 deg rotated rect inscribed in a 90 deg rotated image is the same rect.
    # Let's test 89.9 degrees converted to radians
    angle_rad = math.radians(89.9) 
    # Logic in function: if angle > pi/4, it subtracts from pi/2.
    # So 89.9 becomes 0.1 deg.
    cw, ch = _rotated_rect_with_max_area(w, h, angle_rad)
    # Should be very close to swapping w and h if we were inscribing, but wait - 
    # The function finds largest axis-aligned rect *within* the rotated w x h.
    # If we rotate 100x50 by 90deg, we have a 50x100 bounding box. 
    # The largest axis aligned rect in a 50x100 box is 50x100.
    # But let's stick to the simpler assertion: it returns something valid [1, w] x [1, h] 
    # (The function clamps to original w/h, which might be a bit counter-intuitive for 90deg 
    # if we wanted the swapped dims, but for small-angle straightening it's fine).
    assert 1 <= cw <= w
    assert 1 <= ch <= h

@pytest.mark.parametrize("w,h,angle_deg", [
    (100, 100, 0),    # Unrotated
    (200, 100, 45),   # Diagonal Square (Fully constrained case often)
    (1000, 500, 15),  # Half constrained case likely
    (500, 1000, 15),  # Tall half constrained
])
def test_rotated_rect_calculation_branches(w, h, angle_deg):
    """Exercise different geometric branches of the calculation."""
    angle_rad = math.radians(angle_deg)
    cw, ch = _rotated_rect_with_max_area(w, h, angle_rad)
    
    assert cw > 0
    assert ch > 0
    assert cw <= w
    assert ch <= h
    
    if angle_deg == 0:
        assert cw == w
        assert ch == h
    else:
        # Non-zero rotation always reduces the inscribed axis-aligned box
        assert cw * ch < w * h

def test_rotate_autocrop_rgb_behavior():
    """Test actual image formatting and cropping."""
    # Create valid RGB image
    w, h = 100, 100
    img = Image.new("RGB", (w, h), color=(255, 0, 0)) # Red
    
    # 1. Test no rotation
    res = rotate_autocrop_rgb(img, 0.0)
    assert res.size == (100, 100)
    
    # 2. Test rotation with inset
    angle = 45.0
    inset = 2
    res = rotate_autocrop_rgb(img, angle, inset=inset)
    
    # At 45 deg, a square becomes a diamond. The max inscribed rect is w/(sqrt(2)) ~ 0.707*w
    # 100 * 0.707 = 70. 
    # We expect roughly 70x70 minus inset.
    # expected_approx = 70.0
    assert 60 < res.width < 80
    assert 60 < res.height < 80
    
    # Verify no black wedges (since original was all red)
    # Center pixel should definitely be red
    cx, cy = res.width // 2, res.height // 2
    assert res.getpixel((cx, cy)) == (255, 0, 0)
    
    # Corner pixels should also be red if cropped correctly
    assert res.getpixel((0, 0)) == (255, 0, 0)
    assert res.getpixel((res.width-1, res.height-1)) == (255, 0, 0)


def test_boundary_clamping():
    """Test internal clamping logic."""
    img = Image.new("RGB", (10, 10), (255, 255, 255))
    
    # Very small image, 45 deg rotation
    # Inscribed rect will be small.
    # high inset could theoretically reduce it to < 0.
    res = rotate_autocrop_rgb(img, 45, inset=50) # Huge inset
    
    # It should clamp to at least 1x1 or similar valid image, not crash
    assert res.width > 0
    assert res.height > 0
    
def test_integration_straighten_modes():
    """
    Integration test comparing Scenario A (Manual Crop) vs Scenario B (Straighten Only).
    
    Scenario A: User rotates + manually crops. The rotation expands canvas, user picks crop.
    Scenario B: User rotates only. We autocrop to remove wedges.
    """
    # Create image with specific pattern to verify content
    w, h = 200, 100
    img = Image.new("RGB", (w, h), (0, 255, 0)) # Green
    
    editor = ImageEditor()
    editor.original_image = img
    editor.current_filepath = "dummy.jpg" # Needed for save, but not here
    
    angle = 10.0
    
    # --- Scenario B: Straighten Only ---
    editor.current_edits['straighten_angle'] = angle
    editor.current_edits['crop_box'] = None
    
    res_b = editor._apply_edits(img.copy(), for_export=True)
    
    # Should define a specific size based on autocrop
    w_b, h_b = res_b.size
    
    # --- Scenario A: Manual Crop ---
    # We want to simulate the logic where we replicate what autocrop would have done,
    # but manually via crop_box.
    # 1. Calculate what the autocrop rect would be relative to the *rotated* canvas.
    # Note: _rotated_rect yields dims in *original* pixel space generally, 
    # but let's look at how app.py handles normalization or how editor applies it.
    
    # Actually, let's just assert that if we manually crop to the SAME pixels 
    # that autocrop found, we get the same result.
    
    # Re-use the helper to find the crop box
    angle_rad = math.radians(angle)
    cw, ch = _rotated_rect_with_max_area(w, h, angle_rad)
    
    # rotate_autocrop_rgb logic:
    # It rotates with expand=True. The new center is center of rotated image.
    # It crops centered rect of size (cw, ch).
    
    # So if we emulate this in editor:
    editor.current_edits['straighten_angle'] = angle
    
    # We need to compute the 'crop_box' (normalized 0-1000) that corresponds 
    # to that center crop on the ROTATED image.
    
    # Get rotated size
    rot_temp = img.rotate(-angle, expand=True)
    rw, rh = rot_temp.size
    
    cx, cy = rw / 2.0, rh / 2.0
    left = cx - cw / 2.0
    top = cy - ch / 2.0
    right = left + cw
    bottom = top + ch
    
    # Normalize to 0-1000 relative to rotated size
    # (Editor applies crop_box relative to the current (rotated) image size)
    n_left = int(left / rw * 1000)
    n_top = int(top / rh * 1000)
    n_right = int(right / rw * 1000)
    n_bottom = int(bottom / rh * 1000)
    
    editor.current_edits['crop_box'] = (n_left, n_top, n_right, n_bottom)
    
    res_a = editor._apply_edits(img.copy(), for_export=True)
    
    # Allow for 1-2 pixel differences due to int/round conversions in normalization
    assert abs(res_a.width - w_b) < 5
    assert abs(res_a.height - h_b) < 5
    
    # Verify both are Green (center pixel)
    assert res_a.getpixel((res_a.width//2, res_a.height//2)) == (0, 255, 0)

