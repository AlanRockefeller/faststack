import pytest
import math
import numpy as np
from PIL import Image
from faststack.imaging.editor import (
    _rotated_rect_with_max_area,
    rotate_autocrop_rgb,
    ImageEditor,
)


def test_rotated_rect_edge_cases():
    """Test fundamental edge cases for the rectangle calculation."""
    # Zero dimensions
    assert _rotated_rect_with_max_area(0, 100, 0.5) == (0, 0)
    assert _rotated_rect_with_max_area(100, 0, 0.5) == (0, 0)
    assert _rotated_rect_with_max_area(-10, 100, 0.5) == (0, 0)

    # Near zero angle (should be close to original dimensions)
    w, h = 100, 50
    cw, ch = _rotated_rect_with_max_area(w, h, 0.0000001)
    assert w - 1 <= cw <= w
    assert h - 1 <= ch <= h

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


@pytest.mark.parametrize(
    "w,h,angle_deg",
    [
        (100, 100, 0),  # Unrotated
        (200, 100, 45),  # Diagonal Square (Fully constrained case often)
        (1000, 500, 15),  # Half constrained case likely
        (500, 1000, 15),  # Tall half constrained
    ],
)
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
    img = Image.new("RGB", (w, h), color=(255, 0, 0))  # Red

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
    # Allow small tolerance for interpolation/quantization (254 instead of 255)
    def assert_red(p):
        assert p[0] >= 254 and p[1] < 2 and p[2] < 2

    assert_red(res.getpixel((0, 0)))
    assert_red(res.getpixel((res.width - 1, res.height - 1)))


def test_boundary_clamping():
    """Test internal clamping logic."""
    img = Image.new("RGB", (10, 10), (255, 255, 255))

    # Very small image, 45 deg rotation
    # Inscribed rect will be small.
    # high inset could theoretically reduce it to < 0.
    res = rotate_autocrop_rgb(img, 45, inset=50)  # Huge inset

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
    img = Image.new("RGB", (w, h), (0, 255, 0))  # Green

    editor = ImageEditor()
    editor.original_image = img
    editor.current_filepath = "dummy.jpg"  # Needed for save, but not here

    angle = 10.0

    # --- Scenario B: Straighten Only ---
    editor.current_edits["straighten_angle"] = angle
    editor.current_edits["crop_box"] = None

    res_b = editor._apply_edits(img.copy(), for_export=True)

    # Should define a specific size based on autocrop
    h_b, w_b = res_b.shape[:2]

    # --- Scenario A: Manual Crop ---
    # We want to simulate the logic where we replicate what autocrop would have done,
    # but manually via crop_box.
    # Instead of re-deriving the inscribed rect, we simply take the *actual*
    # dimensions that Scenario B produced (w_b, h_b) and create a manual crop
    # of that exact size, centered on the rotated canvas.

    # NOTE: The editor implementation applies 'crop_box' BEFORE 'straighten_angle'
    # (Crop-then-Rotate) if both are present. This makes it impossible to define
    # a precise axis-aligned crop on the *rotated* canvas using the standard parameters.
    # To simulate a "User cropping the rotated image" correctly in this test,
    # we feed the editor a pre-rotated image and set straighten_angle=0.
    
    # 1) Compute the rotated canvas size using PIL
    rot_temp = img.rotate(-angle, expand=True)
    rw, rh = rot_temp.size

    # Update editor to use the rotated image as 'original' for this scenario
    editor.original_image = rot_temp
    editor.current_edits["straighten_angle"] = 0.0

    # 2) Create a centered crop rectangle with width=w_b and height=h_b
    cx, cy = rw / 2.0, rh / 2.0
    left = cx - w_b / 2.0
    top = cy - h_b / 2.0
    right = left + w_b
    bottom = top + h_b

    # 3) Convert to normalized 0-1000 relative to (rw, rh)
    # 4) Use round() rather than int() to reduce systematic flooring error
    # 5) Clamp to [0, 1000]
    def clamp(val):
        return max(0, min(1000, val))

    n_left = clamp(round(left / rw * 1000))
    n_top = clamp(round(top / rh * 1000))
    n_right = clamp(round(right / rw * 1000))
    n_bottom = clamp(round(bottom / rh * 1000))

    # 6) Set editor.current_edits["crop_box"]
    editor.current_edits["crop_box"] = (n_left, n_top, n_right, n_bottom)

    # Use the pre-rotated image
    res_a = editor._apply_edits(rot_temp.copy(), for_export=True)

    # Allow for a few pixels difference due to floor/round in rotation math
    assert abs(res_a.shape[1] - w_b) < 10
    assert abs(res_a.shape[0] - h_b) < 10

    # Verify both are Green (center pixel)
    # Scale from 0-1 to 0-255 for comparison
    pixel = np.round(res_a[res_a.shape[0] // 2, res_a.shape[1] // 2] * 255).astype(int)
    assert tuple(pixel) == (0, 255, 0)


# -------------------------------------------------------------------------
# Regression Tests for Rotation Direction (CW/CCW)
# -------------------------------------------------------------------------


def create_quadrant_image(w=100, h=100):
    """
    Creates an image with 4 distinct colored quadrants.
    TL: Red (255, 0, 0)
    TR: Green (0, 255, 0)
    BL: Blue (0, 0, 255)
    BR: White (255, 255, 255)
    """
    img = Image.new("RGB", (w, h))
    pixels = img.load()

    cx, cy = w // 2, h // 2

    for y in range(h):
        for x in range(w):
            if x < cx and y < cy:
                pixels[x, y] = (255, 0, 0)  # TL Red
            elif x >= cx and y < cy:
                pixels[x, y] = (0, 255, 0)  # TR Green
            elif x < cx and y >= cy:
                pixels[x, y] = (0, 0, 255)  # BL Blue
            else:
                pixels[x, y] = (255, 255, 255)  # BR White
    return img


def test_rotate_cw():
    """Test that rotate_cw rotates 90 degrees Clockwise."""
    editor = ImageEditor()
    editor.original_image = create_quadrant_image(100, 100)
    editor.current_filepath = "dummy.jpg"

    # Initial state: 0 rotation
    assert editor.current_edits["rotation"] == 0

    # Rotate CW (Logic in app.py subtracts 90, so local state becomes 270)
    # editor.rotate_image_cw() implementation: (current - 90) % 360
    editor.rotate_image_cw()

    assert editor.current_edits["rotation"] == 270

    # Apply edits
    # PIL Transpose constants:
    # ROTATE_90: 90 CCW (Left)
    # ROTATE_270: 270 CCW (Right/CW)
    # Expected for CW: ROTATE_270 (which maps to 270 degrees CCW)

    res = editor._apply_edits(editor.original_image.copy())

    h, w = res.shape[:2]

    # Sample center of quadrants
    q_w, q_h = w // 4, h // 4

    # Helper to get pixel as 0-255 tuple
    def get_p(arr, x, y):
        return tuple(np.round(arr[y, x] * 255).astype(int))

    # Helper for tolerant comparison
    def assert_color(c1, c2, msg=""):
        assert all(abs(a - b) <= 1 for a, b in zip(c1, c2)), f"{msg}: {c1} != {c2}"

    # New TL (Should be Blue)
    assert_color(get_p(res, q_w, q_h), (0, 0, 255), "TL should be Blue (was Red)")

    # New TR (Should be Red)
    assert_color(get_p(res, w - q_w, q_h), (255, 0, 0), "TR should be Red")

    # New BL (Should be White)
    assert_color(get_p(res, q_w, h - q_h), (255, 255, 255), "BL should be White")

    # New BR (Should be Green)
    assert_color(get_p(res, w - q_w, h - q_h), (0, 255, 0), "BR should be Green")


def test_rotate_ccw():
    """Test that rotate_ccw rotates 90 degrees Counter-Clockwise."""
    editor = ImageEditor()
    editor.original_image = create_quadrant_image(100, 100)
    editor.current_filepath = "dummy.jpg"

    # Rotate CCW (Logic: current + 90) -> 90
    editor.rotate_image_ccw()

    assert editor.current_edits["rotation"] == 90

    res = editor._apply_edits(editor.original_image.copy())

    h, w = res.shape[:2]
    q_w, q_h = w // 4, h // 4

    # Helper to get pixel as 0-255 tuple
    def get_p(arr, x, y):
        return tuple(np.round(arr[y, x] * 255).astype(int))

    # CCW Rotation:
    # TL (Red) -> BL
    # TR (Green) -> TL
    # BL (Blue) -> BR
    # BR (White) -> TR

    # Helper for tolerant comparison
    def assert_color(c1, c2, msg=""):
        assert all(abs(a - b) <= 1 for a, b in zip(c1, c2)), f"{msg}: {c1} != {c2}"

    # New TL (Should be Green)
    assert_color(get_p(res, q_w, q_h), (0, 255, 0), "TL should be Green")

    # New TR (Should be White)
    assert_color(get_p(res, w - q_w, q_h), (255, 255, 255), "TR should be White")

    # New BL (Should be Red)
    assert_color(get_p(res, q_w, h - q_h), (255, 0, 0), "BL should be Red")

    # New BR (Should be Blue)
    assert_color(get_p(res, w - q_w, h - q_h), (0, 0, 255), "BR should be Blue")
