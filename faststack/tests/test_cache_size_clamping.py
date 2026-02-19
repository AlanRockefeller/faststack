"""Test for get_decoded_image_size clamping logic."""

from faststack.imaging.cache import get_decoded_image_size
from faststack.models import DecodedImage


class MockBuffer:
    pass


def test_zero_bytes_per_line_defaults_to_rgba():
    """bytes_per_line=0 is invalid metadata -> falls back to RGBA (4 bpp)."""
    item = DecodedImage(
        buffer=MockBuffer(), width=10, height=10, bytes_per_line=0, format=None
    )
    # bytes_per_line=0 fails the > 0 guard, so bytes_per_pixel defaults to 4 (RGBA)
    assert get_decoded_image_size(item) == 10 * 10 * 4


def test_small_bytes_per_line_clamps_to_1():
    """bytes_per_line < width -> integer division gives 0, clamped to 1."""
    item = DecodedImage(
        buffer=MockBuffer(), width=10, height=10, bytes_per_line=5, format=None
    )
    assert get_decoded_image_size(item) == 10 * 10 * 1


def test_rgb_3bpp_not_overcounted():
    """RGB buffers (3 bytes/pixel) must not be inflated to 4."""
    item = DecodedImage(
        buffer=MockBuffer(), width=100, height=100, bytes_per_line=300, format=None
    )
    # bytes_per_pixel = 300 // 100 = 3
    assert get_decoded_image_size(item) == 100 * 100 * 3


def test_rgba_4bpp_unchanged():
    """RGBA buffers (4 bytes/pixel) pass through unchanged."""
    item = DecodedImage(
        buffer=MockBuffer(), width=100, height=100, bytes_per_line=400, format=None
    )
    assert get_decoded_image_size(item) == 100 * 100 * 4


def test_high_bpp_clamped_to_16():
    """Absurdly large bytes_per_pixel clamped to 16."""
    item = DecodedImage(
        buffer=MockBuffer(), width=10, height=10, bytes_per_line=500, format=None
    )
    # bytes_per_pixel = 500 // 10 = 50 -> clamped to 16
    assert get_decoded_image_size(item) == 10 * 10 * 16


def test_missing_dimensions_returns_1():
    """Buffer present but no width/height -> returns 1 (not AttributeError)."""
    from types import SimpleNamespace

    item = SimpleNamespace(buffer=MockBuffer())
    assert get_decoded_image_size(item) == 1


def test_zero_dimensions_returns_1():
    """Buffer present with zero width or height -> returns 1."""
    from types import SimpleNamespace

    item = SimpleNamespace(buffer=MockBuffer(), width=0, height=100)
    assert get_decoded_image_size(item) == 1

    item = SimpleNamespace(buffer=MockBuffer(), width=100, height=0)
    assert get_decoded_image_size(item) == 1
