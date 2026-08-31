"""Tests for the reusable mask subsystem and background darkening tool."""

import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

from faststack.imaging.mask import DarkenSettings, MaskData, MaskStroke
from faststack.imaging.mask_engine import (
    MaskRasterCache,
    forward_transform,
    inverse_transform,
    rasterize_strokes,
    resolve_mask,
)
from faststack.imaging.masked_ops import apply_masked_darken


class TestMaskStroke(unittest.TestCase):
    def test_create_stroke(self):
        s = MaskStroke(
            points=[(0.1, 0.2), (0.3, 0.4)],
            radius=0.05,
            stroke_type="add",
        )
        self.assertEqual(len(s.points), 2)
        self.assertEqual(s.stroke_type, "add")
        self.assertIsNone(s.pressure)

    def test_serialise_round_trip(self):
        s = MaskStroke(
            points=[(0.5, 0.5)],
            radius=0.1,
            stroke_type="protect",
            pressure=[0.8],
        )
        d = s.to_dict()
        s2 = MaskStroke.from_dict(d)
        self.assertEqual(s2.stroke_type, "protect")
        self.assertAlmostEqual(s2.radius, 0.1)
        self.assertEqual(s2.pressure, [0.8])


class TestMaskData(unittest.TestCase):
    def test_revision_tracking(self):
        md = MaskData()
        self.assertEqual(md.revision, 0)
        self.assertFalse(md.has_strokes())

        md.add_stroke(MaskStroke([(0.5, 0.5)], 0.05, "add"))
        self.assertEqual(md.revision, 1)
        self.assertTrue(md.has_strokes())

        md.add_stroke(MaskStroke([(0.2, 0.2)], 0.05, "protect"))
        self.assertEqual(md.revision, 2)

        removed = md.undo_last_stroke()
        self.assertIsNotNone(removed)
        self.assertEqual(removed.stroke_type, "protect")
        self.assertEqual(md.revision, 3)
        self.assertEqual(len(md.strokes), 1)

        md.clear_strokes()
        self.assertEqual(md.revision, 4)
        self.assertFalse(md.has_strokes())

    def test_serialise_round_trip(self):
        md = MaskData()
        md.add_stroke(MaskStroke([(0.1, 0.2)], 0.03, "add"))
        md.overlay_color = (255, 0, 0)
        md.overlay_opacity = 0.6

        d = md.to_dict()
        md2 = MaskData.from_dict(d)
        self.assertEqual(len(md2.strokes), 1)
        self.assertEqual(md2.overlay_color, (255, 0, 0))
        self.assertAlmostEqual(md2.overlay_opacity, 0.6)

    def test_default_overlay(self):
        md = MaskData()
        self.assertEqual(md.overlay_color, (80, 120, 255))
        self.assertAlmostEqual(md.overlay_opacity, 0.4)


class TestDarkenSettings(unittest.TestCase):
    def test_separation_from_mask_data(self):
        """DarkenSettings and MaskData are fully independent."""
        md = MaskData()
        ds = DarkenSettings(mask_id="darken", enabled=True)
        # MaskData has no reference to DarkenSettings fields
        self.assertFalse(hasattr(md, "darken_amount"))
        # DarkenSettings has no strokes
        self.assertFalse(hasattr(ds, "strokes"))

    def test_params_tuple_excludes_darken_amount(self):
        """darken_amount is applied after resolution, so it is not a mask key.

        Dragging the strength slider must not force the expensive edge/prior
        pipeline to re-resolve a mask that cannot have changed.
        """
        ds = DarkenSettings()
        t = ds.params_tuple()
        self.assertIsInstance(t, tuple)
        self.assertEqual(len(t), 8)
        self.assertEqual(
            DarkenSettings(darken_amount=0.1).params_tuple(),
            DarkenSettings(darken_amount=0.9).params_tuple(),
        )
        self.assertNotEqual(
            DarkenSettings(feather=0.1).params_tuple(),
            DarkenSettings(feather=0.9).params_tuple(),
        )

    def test_serialise_round_trip(self):
        ds = DarkenSettings(darken_amount=0.7, mode="border_auto", enabled=True)
        d = ds.to_dict()
        ds2 = DarkenSettings.from_dict(d)
        self.assertAlmostEqual(ds2.darken_amount, 0.7)
        self.assertEqual(ds2.mode, "border_auto")
        self.assertTrue(ds2.enabled)


class TestCoordinateTransforms(unittest.TestCase):
    def test_identity_no_geometry(self):
        """No rotation, no crop → coords pass through."""
        edits = {"rotation": 0, "straighten_angle": 0.0, "crop_box": None}
        px, py = forward_transform(0.5, 0.5, edits, (100, 200))
        self.assertAlmostEqual(px, 100.0, places=1)
        self.assertAlmostEqual(py, 50.0, places=1)

    def test_round_trip_no_geometry(self):
        edits = {"rotation": 0, "straighten_angle": 0.0, "crop_box": None}
        xn, yn = 0.3, 0.7
        shape = (100, 200)  # (H, W)
        px, py = forward_transform(xn, yn, edits, shape)
        # Normalise pixel coords back to [0,1] for inverse_transform
        xr, yr = inverse_transform(px / shape[1], py / shape[0], edits, shape)
        self.assertAlmostEqual(xr, xn, places=5)
        self.assertAlmostEqual(yr, yn, places=5)

    def test_round_trip_with_crop(self):
        edits = {
            "rotation": 0,
            "straighten_angle": 0.0,
            "crop_box": (250, 250, 750, 750),  # center 50%
        }
        # A point at (0.5, 0.5) in base image should map to (0.5, 0.5) in display
        # because crop is centred
        display_x, display_y = 0.5, 0.5
        base_x, base_y = inverse_transform(display_x, display_y, edits, (100, 100))
        self.assertAlmostEqual(base_x, 0.5, places=3)
        self.assertAlmostEqual(base_y, 0.5, places=3)

    def test_round_trip_with_straighten(self):
        edits = {
            "rotation": 0,
            "straighten_angle": 5.0,
            "crop_box": None,
        }
        # Round-trip: base → display → base
        xn, yn = 0.3, 0.7
        # Forward to display coords (normalised)
        px, py = forward_transform(xn, yn, edits, (100, 100))
        # Normalise back
        disp_x, disp_y = px / 100, py / 100
        # Inverse
        xr, yr = inverse_transform(disp_x, disp_y, edits, (100, 100))
        self.assertAlmostEqual(xr, xn, places=3)
        self.assertAlmostEqual(yr, yn, places=3)

    def test_round_trip_with_rotation_90(self):
        edits = {"rotation": 90, "straighten_angle": 0.0, "crop_box": None}
        xn, yn = 0.3, 0.7
        # After 90 CCW rotation, target array has swapped dimensions
        px, py = forward_transform(xn, yn, edits, (200, 100))
        disp_x, disp_y = px / 100, py / 200
        xr, yr = inverse_transform(disp_x, disp_y, edits, (200, 100))
        self.assertAlmostEqual(xr, xn, places=3)
        self.assertAlmostEqual(yr, yn, places=3)

    def test_round_trip_with_rotation_180(self):
        edits = {"rotation": 180, "straighten_angle": 0.0, "crop_box": None}
        xn, yn = 0.3, 0.7
        px, py = forward_transform(xn, yn, edits, (100, 100))
        disp_x, disp_y = px / 100, py / 100
        xr, yr = inverse_transform(disp_x, disp_y, edits, (100, 100))
        self.assertAlmostEqual(xr, xn, places=3)
        self.assertAlmostEqual(yr, yn, places=3)

    def test_round_trip_with_rotation_270(self):
        edits = {"rotation": 270, "straighten_angle": 0.0, "crop_box": None}
        xn, yn = 0.3, 0.7
        px, py = forward_transform(xn, yn, edits, (200, 100))
        disp_x, disp_y = px / 100, py / 200
        xr, yr = inverse_transform(disp_x, disp_y, edits, (200, 100))
        self.assertAlmostEqual(xr, xn, places=3)
        self.assertAlmostEqual(yr, yn, places=3)

    def test_round_trip_rotation_plus_crop(self):
        """Combined 90-degree rotation + crop — the most realistic scenario."""
        edits = {
            "rotation": 90,
            "straighten_angle": 0.0,
            "crop_box": (250, 250, 750, 750),  # centre 50%
        }
        # Centre point should survive the round trip
        xn, yn = 0.5, 0.5
        px, py = forward_transform(xn, yn, edits, (100, 100))
        disp_x, disp_y = px / 100, py / 100
        xr, yr = inverse_transform(disp_x, disp_y, edits, (100, 100))
        self.assertAlmostEqual(xr, xn, places=3)
        self.assertAlmostEqual(yr, yn, places=3)

        # Off-centre point
        xn, yn = 0.4, 0.6
        px, py = forward_transform(xn, yn, edits, (100, 100))
        disp_x, disp_y = px / 100, py / 100
        xr, yr = inverse_transform(disp_x, disp_y, edits, (100, 100))
        self.assertAlmostEqual(xr, xn, places=3)
        self.assertAlmostEqual(yr, yn, places=3)


class TestStrokeRasterisation(unittest.TestCase):
    def test_basic_rasterisation(self):
        md = MaskData()
        md.add_stroke(MaskStroke([(0.5, 0.5)], 0.1, "add"))

        edits = {"rotation": 0, "straighten_angle": 0.0, "crop_box": None}
        add_map, protect_map = rasterize_strokes(md, (100, 100), edits)

        self.assertEqual(add_map.shape, (100, 100))
        self.assertEqual(protect_map.shape, (100, 100))
        # Centre should be painted
        self.assertGreater(add_map[50, 50], 0.5)
        # Protect map should be empty
        self.assertAlmostEqual(protect_map.max(), 0.0)

    def test_protect_stroke(self):
        md = MaskData()
        md.add_stroke(MaskStroke([(0.5, 0.5)], 0.1, "protect"))

        edits = {"rotation": 0, "straighten_angle": 0.0, "crop_box": None}
        add_map, protect_map = rasterize_strokes(md, (100, 100), edits)

        self.assertAlmostEqual(add_map.max(), 0.0)
        self.assertGreater(protect_map[50, 50], 0.5)

    def test_different_resolutions(self):
        """Same strokes rasterised at different sizes produce different arrays."""
        md = MaskData()
        md.add_stroke(MaskStroke([(0.5, 0.5)], 0.05, "add"))
        edits = {"rotation": 0, "straighten_angle": 0.0, "crop_box": None}

        add_small, _ = rasterize_strokes(md, (50, 50), edits)
        add_large, _ = rasterize_strokes(md, (200, 200), edits)

        self.assertEqual(add_small.shape, (50, 50))
        self.assertEqual(add_large.shape, (200, 200))
        # Both should have paint near centre
        self.assertGreater(add_small[25, 25], 0.0)
        self.assertGreater(add_large[100, 100], 0.0)

    def test_rasterisation_with_rotation_90(self):
        """A stroke at (0.8, 0.5) should move after 90 CCW rotation."""
        md = MaskData()
        md.add_stroke(MaskStroke([(0.8, 0.5)], 0.1, "add"))
        edits = {"rotation": 90, "straighten_angle": 0.0, "crop_box": None}
        # 90 CCW: (0.8, 0.5) → (0.5, 0.2) in rotated space
        add_map, _ = rasterize_strokes(md, (100, 100), edits)
        self.assertGreater(add_map[20, 50], 0.3)
        # Original position (50, 80) should have low/no paint
        self.assertLess(add_map[50, 80], 0.1)


class TestMaskResolution(unittest.TestCase):
    def test_resolve_produces_valid_mask(self):
        md = MaskData()
        md.add_stroke(MaskStroke([(0.5, 0.5)], 0.15, "add"))

        ds = DarkenSettings(enabled=True, mode="paint_only")
        img = np.full((100, 100, 3), 0.5, dtype=np.float32)
        edits = {"rotation": 0, "straighten_angle": 0.0, "crop_box": None}

        mask = resolve_mask(md, ds, img, (100, 100), edits)
        self.assertEqual(mask.shape, (100, 100))
        self.assertTrue(np.all(mask >= 0.0))
        self.assertTrue(np.all(mask <= 1.0))
        # Centre should have high mask value
        self.assertGreater(mask[50, 50], 0.3)

    def test_protect_resists_masking(self):
        """Protected areas should have lower mask values."""
        md = MaskData()
        # Paint entire image as background
        md.add_stroke(MaskStroke([(0.5, 0.5)], 0.5, "add"))
        # Protect the centre
        md.add_stroke(MaskStroke([(0.5, 0.5)], 0.1, "protect"))

        ds = DarkenSettings(enabled=True, mode="paint_only", subject_protection=1.0)
        img = np.full((100, 100, 3), 0.5, dtype=np.float32)
        edits = {"rotation": 0, "straighten_angle": 0.0, "crop_box": None}

        mask = resolve_mask(md, ds, img, (100, 100), edits)
        # Centre (protected) should be lower than edges (unprotected)
        centre = mask[50, 50]
        edge = mask[5, 5]
        self.assertLess(centre, edge)


class TestMaskedDarken(unittest.TestCase):
    def test_darken_only_affects_masked_areas(self):
        arr = np.full((100, 100, 3), 0.6, dtype=np.float32)
        # Mask: left half = background, right half = subject
        mask = np.zeros((100, 100), dtype=np.float32)
        mask[:, :50] = 1.0

        original_right = arr[50, 75].copy()
        result = apply_masked_darken(arr, mask, darken_amount=0.8, edge_protection=0.0)

        # Right half (unmasked) should be unchanged
        np.testing.assert_array_almost_equal(result[50, 75], original_right, decimal=3)
        # Left half (masked) should be darker
        self.assertTrue(np.all(result[50, 25] < original_right))

    def test_zero_amount_is_noop(self):
        arr = np.full((50, 50, 3), 0.5, dtype=np.float32)
        original = arr.copy()
        mask = np.ones((50, 50), dtype=np.float32)

        result = apply_masked_darken(arr, mask, darken_amount=0.0, edge_protection=0.0)
        np.testing.assert_array_equal(result, original)

    def test_output_clamped(self):
        arr = np.full((50, 50, 3), 0.1, dtype=np.float32)
        mask = np.ones((50, 50), dtype=np.float32)

        result = apply_masked_darken(arr, mask, darken_amount=1.0, edge_protection=0.0)
        self.assertTrue(np.all(result >= 0.0))
        self.assertTrue(np.all(result <= 1.0))


class TestMaskRasterCache(unittest.TestCase):
    def test_stroke_cache_hit(self):
        cache = MaskRasterCache()
        maps = (
            np.zeros((10, 10), dtype=np.float32),
            np.zeros((10, 10), dtype=np.float32),
        )
        cache.put_strokes(1, (10, 10), 42, maps)

        result = cache.get_strokes(1, (10, 10), 42)
        self.assertIsNotNone(result)

    def test_stroke_cache_miss_different_revision(self):
        cache = MaskRasterCache()
        maps = (
            np.zeros((10, 10), dtype=np.float32),
            np.zeros((10, 10), dtype=np.float32),
        )
        cache.put_strokes(1, (10, 10), 42, maps)

        result = cache.get_strokes(2, (10, 10), 42)
        self.assertIsNone(result)

    def test_stroke_cache_miss_different_shape(self):
        """Different resolution = different cache key."""
        cache = MaskRasterCache()
        maps = (
            np.zeros((10, 10), dtype=np.float32),
            np.zeros((10, 10), dtype=np.float32),
        )
        cache.put_strokes(1, (10, 10), 42, maps)

        result = cache.get_strokes(1, (200, 200), 42)
        self.assertIsNone(result)

    def test_resolved_cache(self):
        cache = MaskRasterCache()
        mask = np.zeros((10, 10), dtype=np.float32)
        params = (0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.0, 0.0, "assisted")
        img_key = 12345
        cache.put_resolved(1, (10, 10), 42, params, img_key, mask)

        result = cache.get_resolved(1, (10, 10), 42, params, img_key)
        self.assertIsNotNone(result)

        # Different params = miss
        params2 = (0.7, 0.5, 0.5, 0.5, 0.5, 0.5, 0.0, 0.0, "assisted")
        result2 = cache.get_resolved(1, (10, 10), 42, params2, img_key)
        self.assertIsNone(result2)

        # Different image content = miss
        img_key2 = 99999
        result3 = cache.get_resolved(1, (10, 10), 42, params, img_key2)
        self.assertIsNone(result3)

    def test_clear(self):
        cache = MaskRasterCache()
        maps = (
            np.zeros((10, 10), dtype=np.float32),
            np.zeros((10, 10), dtype=np.float32),
        )
        cache.put_strokes(1, (10, 10), 42, maps)
        cache.clear()
        self.assertIsNone(cache.get_strokes(1, (10, 10), 42))

    def test_resolved_cache_detects_change_outside_old_sample_grid(self):
        """FS-P6-005 fallback: standalone callers pass no image_key.

        ``resolve_mask`` still content-hashes when a caller has no trustworthy
        render provenance. The production path is covered by
        ``TestMaskRenderIdentity`` instead.
        """
        mask_data = MaskData()
        settings = DarkenSettings(
            enabled=True,
            mode="assisted",
            feather=0.0,
            neutrality_sensitivity=0.0,
            auto_from_edges=0.0,
        )
        edits = {"rotation": 0, "straighten_angle": 0.0, "crop_box": None}
        image = np.ones((12, 12, 3), dtype=np.float32)
        cache = MaskRasterCache()

        before = resolve_mask(
            mask_data, settings, image, image.shape[:2], edits, cache=cache
        )
        # (5, 5) is outside the former 4x4 sample rows/columns (0, 4, 8, 11).
        image[5, 5] = 0.0
        cached = resolve_mask(
            mask_data, settings, image, image.shape[:2], edits, cache=cache
        )
        uncached = resolve_mask(
            mask_data, settings, image, image.shape[:2], edits, cache=None
        )

        self.assertTrue(np.array_equal(cached, uncached))
        self.assertFalse(np.array_equal(before, cached))


class TestEditorIntegration(unittest.TestCase):
    """Test that the editor pipeline integrates the darken step correctly."""

    def test_applied_mask_and_overlay_use_same_pre_darken_stage(self):
        from faststack.imaging.editor import ImageEditor

        editor = ImageEditor()
        source = np.linspace(0.05, 0.95, 48 * 32 * 3, dtype=np.float32).reshape(
            32, 48, 3
        )
        mask_data = MaskData()
        mask_data.add_stroke(MaskStroke([(0.5, 0.5)], 0.2, "add"))
        settings = DarkenSettings(enabled=True, mode="assisted")
        edits = dict(editor.current_edits)
        edits.update(
            {
                "exposure": 0.4,
                "crop_box": (100, 100, 900, 900),
                "straighten_angle": 3.0,
                "darken_settings": settings,
            }
        )
        editor._mask_assets[settings.mask_id] = mask_data

        expected_stage = editor._apply_edits(
            source.copy(),
            edits=edits,
            for_export=True,
            stop_before_darken=True,
        )
        captured = []

        def capture_resolve(
            mask, darken, image, shape, current_edits, cache=None, image_key=None
        ):
            captured.append((image.copy(), image_key))
            return np.zeros(shape, dtype=np.float32)

        with patch(
            "faststack.imaging.mask_engine.resolve_mask",
            side_effect=capture_resolve,
        ):
            editor._apply_edits(source.copy(), edits=edits, for_export=True)

        self.assertEqual(len(captured), 1)
        self.assertTrue(np.array_equal(captured[0][0], expected_stage))
        # The production path must supply a generation identity, never fall
        # back to hashing the rendered buffer.
        self.assertIsNotNone(captured[0][1])

    def test_darken_settings_in_initial_edits(self):
        from faststack.imaging.editor import ImageEditor

        editor = ImageEditor()
        self.assertIn("darken_settings", editor.current_edits)
        self.assertIsNone(editor.current_edits["darken_settings"])

    def test_mask_assets_dict_exists(self):
        from faststack.imaging.editor import ImageEditor

        editor = ImageEditor()
        self.assertIsInstance(editor._mask_assets, dict)
        self.assertEqual(len(editor._mask_assets), 0)

    def test_clear_resets_mask_state(self):
        from faststack.imaging.editor import ImageEditor

        editor = ImageEditor()
        editor._mask_assets["darken"] = MaskData()
        editor.clear()
        self.assertEqual(len(editor._mask_assets), 0)

    def test_apply_edits_with_darken(self):
        """Darken step runs when settings and strokes are present."""
        from PIL import Image as PILImage

        from faststack.imaging.editor import ImageEditor

        editor = ImageEditor()
        # Create a small test image
        img = PILImage.new("RGB", (50, 50), color=(128, 128, 128))
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            img.save(f.name)
            editor.load_image(f.name)

        # Set up darken
        md = MaskData()
        md.add_stroke(MaskStroke([(0.5, 0.5)], 0.3, "add"))
        editor._mask_assets["darken"] = md
        ds = DarkenSettings(enabled=True, darken_amount=0.8, mode="paint_only")
        editor.current_edits["darken_settings"] = ds

        # Apply edits
        arr = editor.float_preview.copy()
        result = editor._apply_edits(arr, for_export=False)

        # Result should be darker in the centre vs a version without darken
        editor.current_edits["darken_settings"] = None
        arr2 = editor.float_preview.copy()
        result_no_darken = editor._apply_edits(arr2, for_export=False)

        # The darkened version should have lower values in the masked area
        centre_dark = result[25, 25].mean()
        centre_normal = result_no_darken[25, 25].mean()
        self.assertLess(centre_dark, centre_normal)

        # Clean up
        import os

        os.unlink(f.name)

    def test_load_image_clears_mask_state(self):
        """Loading a new image must clear mask assets and raster cache."""
        from PIL import Image as PILImage

        from faststack.imaging.editor import ImageEditor

        editor = ImageEditor()

        # First load
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            PILImage.new("RGB", (50, 50), color=(128, 128, 128)).save(f.name)
            editor.load_image(f.name)

        # Add darken state
        editor._mask_assets["darken"] = MaskData()
        editor._mask_assets["darken"].add_stroke(MaskStroke([(0.5, 0.5)], 0.1, "add"))
        editor._mask_raster_cache.put_strokes(
            1,
            (50, 50),
            0,
            (
                np.zeros((50, 50), dtype=np.float32),
                np.zeros((50, 50), dtype=np.float32),
            ),
        )

        # Second load — should clear mask state
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f2:
            PILImage.new("RGB", (50, 50), color=(200, 200, 200)).save(f2.name)
            editor.load_image(f2.name)

        self.assertEqual(len(editor._mask_assets), 0)
        self.assertIsNone(editor._mask_raster_cache.get_strokes(1, (50, 50), 0))

        import os

        os.unlink(f.name)
        os.unlink(f2.name)

    def test_toggle_off_disables_darken_effect(self):
        """Turning the darken tool off must disable the effect in the render pipeline."""
        from PIL import Image as PILImage

        from faststack.imaging.editor import ImageEditor

        editor = ImageEditor()
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            PILImage.new("RGB", (50, 50), color=(128, 128, 128)).save(f.name)
            editor.load_image(f.name)

        # Set up darken with strokes
        md = MaskData()
        md.add_stroke(MaskStroke([(0.5, 0.5)], 0.3, "add"))
        editor._mask_assets["darken"] = md
        ds = DarkenSettings(enabled=True, darken_amount=0.8, mode="paint_only")
        editor.current_edits["darken_settings"] = ds

        # Render with darken ON
        arr_on = editor.float_preview.copy()
        result_on = editor._apply_edits(arr_on, for_export=False)
        centre_on = result_on[25, 25].mean()

        # Simulate toggle off: set enabled=False (what toggle_darken_mode does)
        ds.enabled = False

        # Render with darken OFF
        arr_off = editor.float_preview.copy()
        result_off = editor._apply_edits(arr_off, for_export=False)
        centre_off = result_off[25, 25].mean()

        # Effect must be gone — centre should be brighter when disabled
        self.assertGreater(centre_off, centre_on)

        import os

        os.unlink(f.name)

    def test_snapshot_captures_immutable_darken_state(self):
        """snapshot_for_export deep-copies darken state — mutations after
        snapshot do not affect the export data."""
        import tempfile

        from PIL import Image as PILImage

        from faststack.imaging.editor import ImageEditor

        editor = ImageEditor()

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            PILImage.new("RGB", (50, 50), color=(128, 128, 128)).save(f.name)
            editor.load_image(f.name)

        # Set up darken with strokes
        md = MaskData()
        md.add_stroke(MaskStroke([(0.5, 0.5)], 0.3, "add"))
        editor._mask_assets["darken"] = md
        ds = DarkenSettings(enabled=True, darken_amount=0.8, mode="paint_only")
        editor.current_edits["darken_settings"] = ds

        # Take snapshot
        snapshot = editor.snapshot_for_export()

        # Verify deep-copy: snapshot objects are NOT the live ones
        snap_ds = snapshot["edits"].get("darken_settings")
        self.assertIsNotNone(snap_ds)
        self.assertIsNot(snap_ds, ds, "DarkenSettings should be deep-copied")

        snap_mask = snapshot["mask_override"]
        self.assertIsNotNone(snap_mask)
        self.assertIsNot(
            snap_mask.get("darken"),
            md,
            "MaskData should be deep-copied",
        )

        # Verify fresh export cache
        self.assertIsNotNone(snapshot["export_cache"])
        self.assertIsNot(
            snapshot["export_cache"],
            editor._mask_raster_cache,
            "Export should use a fresh cache, not the shared preview cache",
        )

        # Verify EXIF is captured
        self.assertIn("main_exif", snapshot)
        self.assertIn("source_exif", snapshot)

        # Verify filepath is captured
        self.assertIsNotNone(snapshot["filepath_snapshot"])

        import os

        os.unlink(f.name)

    def test_snapshot_without_darken_no_override(self):
        """snapshot_for_export with no darken should not produce mask overrides."""
        import tempfile

        from PIL import Image as PILImage

        from faststack.imaging.editor import ImageEditor

        editor = ImageEditor()

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            PILImage.new("RGB", (50, 50), color=(128, 128, 128)).save(f.name)
            editor.load_image(f.name)

        snapshot = editor.snapshot_for_export()
        self.assertIsNone(snapshot["mask_override"])
        self.assertIsNone(snapshot["export_cache"])

        import os

        os.unlink(f.name)

    def test_mutation_after_snapshot_does_not_affect_export(self):
        """Modifying editor state after snapshot must not change saved output."""
        import tempfile

        from PIL import Image as PILImage

        from faststack.imaging.editor import ImageEditor

        editor = ImageEditor()

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            PILImage.new("RGB", (50, 50), color=(128, 128, 128)).save(f.name)
            editor.load_image(f.name)

        # Set up darken
        md = MaskData()
        md.add_stroke(MaskStroke([(0.5, 0.5)], 0.3, "add"))
        editor._mask_assets["darken"] = md
        ds = DarkenSettings(enabled=True, darken_amount=0.5, mode="paint_only")
        editor.current_edits["darken_settings"] = ds

        # Snapshot at darken_amount=0.5
        snapshot = editor.snapshot_for_export()

        # Mutate live state AFTER snapshot
        ds.darken_amount = 1.0
        md.add_stroke(MaskStroke([(0.1, 0.1)], 0.5, "add"))
        editor._mask_assets.clear()

        # Snapshot should still have the original values
        snap_ds = snapshot["edits"]["darken_settings"]
        self.assertAlmostEqual(snap_ds.darken_amount, 0.5)

        snap_mask = snapshot["mask_override"]["darken"]
        self.assertEqual(len(snap_mask.strokes), 1)  # only the original stroke

        import os

        os.unlink(f.name)

    def test_navigation_after_snapshot_does_not_affect_export(self):
        """Clearing editor state (simulating navigation) after snapshot must
        not prevent save_from_snapshot from working."""
        import tempfile

        from PIL import Image as PILImage

        from faststack.imaging.editor import ImageEditor

        editor = ImageEditor()

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            PILImage.new("RGB", (50, 50), color=(128, 128, 128)).save(f.name)
            editor.load_image(f.name)

        # Set up darken
        md = MaskData()
        md.add_stroke(MaskStroke([(0.5, 0.5)], 0.3, "add"))
        editor._mask_assets["darken"] = md
        ds = DarkenSettings(enabled=True, darken_amount=0.8, mode="paint_only")
        editor.current_edits["darken_settings"] = ds

        # Snapshot captures all state
        snapshot = editor.snapshot_for_export()

        # Simulate navigation clearing all editor state
        editor.clear()
        self.assertIsNone(editor.float_image)
        self.assertEqual(len(editor._mask_assets), 0)

        # Simulate loading a second temporary image which will repopulate current_filepath
        # and cached state, creating a potential cross-image race context.
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f2:
            PILImage.new("RGB", (50, 50), color=(150, 150, 150)).save(f2.name)
            editor.load_image(f2.name)

        # save_from_snapshot should still work with the snapshot
        result = editor.save_from_snapshot(snapshot)
        # save_from_snapshot uses _apply_edits which uses the passed cache_context
        # to avoid polluting or depending on live editor state.
        self.assertIsNotNone(result)

        import os

        os.unlink(f.name)
        os.unlink(f2.name)


class TestOverlayFallback(unittest.TestCase):
    def test_mask_overlay_returns_transparent_when_no_overlay(self):
        """Verify that requesting mask_overlay with no image returns a
        transparent QImage, not an opaque placeholder."""
        try:
            from unittest.mock import Mock

            from PySide6.QtGui import QImage

            from faststack.ui.provider import ImageProvider
        except ImportError:
            self.skipTest("PySide6 not available")

        # Mock app_controller to return no overlay image
        mock_controller = Mock()
        mock_controller.ui_state._darken_overlay_image = None

        provider = ImageProvider(mock_controller)
        transparent = provider.requestImage("mask_overlay/test", None, None)

        # Verify it has zero alpha (i.e. fully transparent)
        pixel = transparent.pixelColor(0, 0)
        self.assertEqual(pixel.alpha(), 0)

        # Verify it is NOT RGB888 format (the old placeholder was)
        self.assertEqual(transparent.format(), QImage.Format.Format_ARGB32)


class TestMaskRenderIdentity(unittest.TestCase):
    """FS-P6-005 on the production path.

    ``resolve_mask`` no longer hashes the rendered buffer when the caller has
    trustworthy provenance; ``ImageEditor`` supplies a render identity instead.
    These tests drive the real ``_apply_edits`` pipeline (no mocking of the
    identity itself) and assert the two properties FS-P6-005 needs: any input
    that can move pre-darken pixels changes the key, and nothing else does.
    """

    def _editor(self, source=None):
        from faststack.imaging.editor import ImageEditor

        editor = ImageEditor()
        editor.session_id = "session-under-test"
        if source is None:
            source = self._source()
        editor.float_preview = source
        editor.float_image = source
        mask_data = MaskData()
        mask_data.add_stroke(MaskStroke([(0.5, 0.5)], 0.3, "add"))
        settings = DarkenSettings(enabled=True, mode="assisted")
        editor._mask_assets[settings.mask_id] = mask_data
        editor.current_edits["darken_settings"] = settings
        return editor, source, mask_data, settings

    @staticmethod
    def _source():
        """Flat grey with one small saturated patch and one small bright patch.

        Both patches are far smaller than the old 4x4 sparse sample grid would
        have landed on, which is the defect FS-P6-005 was filed for.
        """
        arr = np.full((64, 96, 3), 0.45, dtype=np.float32)
        arr[7:11, 9:13] = (0.95, 0.12, 0.10)  # localized saturated patch
        arr[52:56, 80:84] = 0.99  # localized near-white patch
        return arr

    def _run(self, editor, **kwargs):
        """Render through _apply_edits, returning (image_key, resolved mask)."""
        from faststack.imaging import mask_engine

        seen = {}
        real_resolve = mask_engine.resolve_mask

        def spy(mask, darken, image, shape, edits, cache=None, image_key=None):
            resolved = real_resolve(
                mask, darken, image, shape, edits, cache=cache, image_key=image_key
            )
            seen["key"] = image_key
            seen["mask"] = resolved
            return resolved

        with patch.object(mask_engine, "resolve_mask", spy):
            editor._apply_edits(
                editor.float_preview,
                edits=dict(editor.current_edits),
                protect_input=True,
                **kwargs,
            )
        self.assertIn("key", seen, "resolve_mask was never reached")
        self.assertIsNotNone(seen["key"], "production path fell back to hashing")
        return seen["key"], seen["mask"]

    # --- invalidation ---------------------------------------------------

    def test_localized_edit_changes_key_and_mask(self):
        """An edit that only moves a handful of pixels must invalidate."""
        editor, _, _, _ = self._editor()
        editor.current_edits["color_sat_red"] = 0.0
        base_key, base_mask = self._run(editor)

        # Per-hue saturation reaches only the small red patch.
        editor.current_edits["color_sat_red"] = -1.0
        new_key, new_mask = self._run(editor)

        self.assertNotEqual(base_key, new_key)
        self.assertFalse(np.array_equal(base_mask, new_mask))

    def test_localized_source_change_changes_key(self):
        """A rebuilt master differing in one small region must invalidate."""
        editor, source, _, _ = self._editor()
        base_key, _ = self._run(editor)

        rebuilt = source.copy()
        rebuilt[7:11, 9:13] = 0.05
        editor.float_preview = rebuilt
        editor.float_image = rebuilt
        editor._preview_master_generation += 1
        new_key, _ = self._run(editor)

        self.assertNotEqual(base_key, new_key)

    def test_reload_in_place_changes_key(self):
        editor, _, _, _ = self._editor()
        base_key, _ = self._run(editor)
        editor.current_mtime += 1.0
        self.assertNotEqual(base_key, self._run(editor)[0])

    def test_geometry_edits_change_key(self):
        for key, value in (
            ("crop_box", (100, 100, 900, 900)),
            ("rotation", 90),
            ("straighten_angle", 2.5),
        ):
            with self.subTest(edit=key):
                editor, _, _, _ = self._editor()
                base_key, _ = self._run(editor)
                editor.current_edits[key] = value
                self.assertNotEqual(base_key, self._run(editor)[0])

    def test_for_export_changes_key(self):
        """Export and preview select different clipping branches."""
        editor, _, _, _ = self._editor()
        preview_key, _ = self._run(editor, for_export=False)
        export_key, _ = self._run(editor, for_export=True)
        self.assertNotEqual(preview_key, export_key)

    def test_render_scale_params_change_key(self):
        editor, _, _, _ = self._editor()
        base_key, _ = self._run(editor)
        self.assertNotEqual(base_key, self._run(editor, downscale_long_edge=48)[0])
        self.assertNotEqual(base_key, self._run(editor, detail_source_scale=0.5)[0])

    def test_levels_soft_knee_changes_key(self):
        editor, _, _, _ = self._editor()
        editor.current_edits["blacks"] = 0.4
        base_key, _ = self._run(editor)
        self.assertNotEqual(
            base_key, self._run(editor, levels_soft_knee_override=False)[0]
        )

    def test_approximate_detail_reuse_is_distinguishable(self):
        """Identical edits, different blur provenance, different key.

        The detail-band cache deliberately survives exposure changes and
        rescales its blurs, which is only an approximation once highlights
        recovery sits between the two. Two renders whose *edits* match can
        therefore still differ in pixels, so the mask key has to separate them.
        Both editors share one source array so the only difference is which
        blurs the render had available.
        """
        source = self._source()

        carried, _, _, _ = self._editor(source)
        carried.current_edits.update(
            {"clarity": 0.5, "highlights": -0.6, "exposure": 0.1}
        )
        self._run(carried)  # warms the detail cache at exposure 0.1
        carried.current_edits["exposure"] = 0.6
        carried_key, _ = self._run(carried)  # reuses those blurs, rescaled

        fresh, _, _, _ = self._editor(source)
        fresh.current_edits.update(
            {"clarity": 0.5, "highlights": -0.6, "exposure": 0.6}
        )
        fresh_key, _ = self._run(fresh)  # blurs computed at 0.6 exactly

        self.assertNotEqual(
            carried_key,
            fresh_key,
            "approximate blur reuse is indistinguishable from an exact render",
        )
        # ...and the *only* thing separating them is the detail stage stamp.
        self.assertEqual(
            [v for i, v in enumerate(carried_key) if i != 8],
            [v for i, v in enumerate(fresh_key) if i != 8],
        )

    def test_detail_stamp_is_real_cache_state(self):
        """The stamp comes from the cache _apply_edits wrote, not a mock."""
        editor, _, _, _ = self._editor()
        editor.current_edits["clarity"] = 0.5
        key, _ = self._run(editor)
        stamp = dict(key[8][1])
        self.assertEqual(stamp["Y20"], editor._cached_detail_bands["band_ids"]["Y20"])
        # A second identical render reuses the entry and keeps the same key.
        self.assertEqual(key, self._run(editor)[0])

    def test_detail_bands_absent_leaves_no_stamp(self):
        """With no detail sliders active the stage is exact and unstamped."""
        editor, _, _, _ = self._editor()
        key, _ = self._run(editor)
        self.assertEqual(key[8], (None, None))

    def test_highlight_analysis_reuse_across_render_tiers(self):
        """Analysis measured at one resolution and reused at another.

        Its cache key has no resolution term, so a drag render and a master
        render share the entry even though the strided measurement differs.
        That only reaches pixels through the highlights branch.
        """
        source = self._source()

        carried, _, _, _ = self._editor(source)
        carried.current_edits["highlights"] = -0.6
        self._run(carried, downscale_long_edge=48)  # measures the strided 48px tier
        carried_key, _ = self._run(carried)  # full render reusing that state

        fresh, _, _, _ = self._editor(source)
        fresh.current_edits["highlights"] = -0.6
        fresh_key, _ = self._run(fresh)  # full render measuring its own state

        self.assertNotEqual(carried_key[8][0], fresh_key[8][0])
        self.assertNotEqual(carried_key, fresh_key)
        self.assertEqual(
            [v for i, v in enumerate(carried_key) if i != 8],
            [v for i, v in enumerate(fresh_key) if i != 8],
        )

        # With highlights inactive the analysis cannot steer pixels at all.
        fresh.current_edits["highlights"] = 0.0
        self.assertIsNone(self._run(fresh)[0][8][0])

    def test_mask_stroke_and_settings_invalidate(self):
        from faststack.imaging import mask_engine

        editor, _, mask_data, settings = self._editor()
        _, base_mask = self._run(editor)

        mask_data.add_stroke(MaskStroke([(0.2, 0.2)], 0.1, "add"))
        _, stroked = self._run(editor)
        self.assertFalse(np.array_equal(base_mask, stroked))

        settings.feather = 0.9
        _, feathered = self._run(editor)
        self.assertFalse(np.array_equal(stroked, feathered))
        self.assertIsInstance(editor._mask_raster_cache, mask_engine.MaskRasterCache)

    # --- stability ------------------------------------------------------

    def test_identical_render_reuses_cached_mask(self):
        """No pixel of the image is read to establish that nothing changed."""
        editor, _, _, _ = self._editor()
        first_key, first_mask = self._run(editor)
        second_key, second_mask = self._run(editor)
        self.assertEqual(first_key, second_key)
        self.assertIs(first_mask, second_mask)

    def test_hashing_is_not_used_on_the_production_path(self):
        from faststack.imaging import mask_engine

        editor, _, _, _ = self._editor()
        with patch.object(
            mask_engine,
            "_image_content_key",
            side_effect=AssertionError("full-buffer hash on the production path"),
        ):
            self._run(editor)

    def test_post_mask_edits_do_not_invalidate(self):
        """Stages at or after the darken step cannot change the mask."""
        editor, _, _, settings = self._editor()
        base_key, base_mask = self._run(editor)

        editor.current_edits["vignette"] = 0.8
        self.assertEqual(base_key, self._run(editor)[0])

        settings.darken_amount = 0.9
        same_key, same_mask = self._run(editor)
        self.assertEqual(base_key, same_key)
        self.assertIs(base_mask, same_mask)


class TestPublishedOverlayMask(unittest.TestCase):
    """The overlay is built from the accepted preview's own resolved mask.

    Before this, _update_darken_overlay ran a second, master-resolution
    _apply_edits on the GUI thread for every accepted preview frame. The mask
    now rides on the DecodedImage produced by the worker render that already
    resolved it, so the pixels and the overlay above them are one result.
    """

    def setUp(self):
        try:
            from PySide6.QtGui import QImage  # noqa: F401
        except ImportError:
            self.skipTest("PySide6 not available")

    # --- fixtures ---------------------------------------------------------

    def _editor(self, source=None, strokes=((0.5, 0.5),)):
        from faststack.imaging.editor import ImageEditor

        editor = ImageEditor()
        editor.session_id = "session-under-test"
        if source is None:
            source = np.full((48, 72, 3), 0.45, dtype=np.float32)
            source[6:10, 8:12] = (0.95, 0.12, 0.10)
        editor.float_preview = source
        editor.float_image = source
        mask_data = MaskData()
        for point in strokes:
            mask_data.add_stroke(MaskStroke([point], 0.25, "add"))
        settings = DarkenSettings(enabled=True, mode="assisted")
        editor._mask_assets["darken"] = mask_data
        settings.mask_id = "darken"
        editor.current_edits["darken_settings"] = settings
        return editor, source, mask_data, settings

    def _render(self, editor, **kwargs):
        """Render a display frame the way the preview worker does."""
        return editor._render_decoded_from_float(
            editor.float_preview,
            edits=dict(editor.current_edits),
            for_export=False,
            protect_input=True,
            **kwargs,
        )

    def _controller(self, editor, mask_data):
        """Minimal stand-in carrying the real AppController methods."""
        from types import SimpleNamespace

        from faststack.app import AppController

        ui_state = SimpleNamespace(
            _darken_overlay_image=None,
            _darken_overlay_generation=0,
            darken_overlay_generation_changed=Mock(),
            _is_darkening=True,
            currentImageSourceChanged=Mock(),
            highlightStateChanged=Mock(),
        )
        ctl = SimpleNamespace(
            _shutting_down=False,
            _preview_lock=threading.RLock(),
            _preview_inflight=True,
            _preview_pending=False,
            _preview_token=1,
            _preview_full_res_token=-1,
            _live_preview_target_dims=None,
            _live_preview_target_session_key=None,
            _accepted_darken_mask=None,
            _accepted_darken_mask_session=None,
            _darken_overlay_buffer=None,
            ui_refresh_generation=0,
            current_index=0,
            _last_rendered_preview_index=-1,
            _last_rendered_preview_gen=-1,
            _original_compare_active=False,
            _original_compare_preview=None,
            image_editor=editor,
            ui_state=ui_state,
            _publish_last_rendered_preview_locked=Mock(),
            _get_current_live_preview_session_key=Mock(return_value=("img", "s")),
            _kick_preview_worker=Mock(),
            update_histogram=Mock(),
        )
        for name in (
            "_apply_preview_result",
            "_update_darken_overlay",
            "_clear_darken_overlay",
            "_accepted_darken_mask_for_display",
            "_invalidate_accepted_darken_mask",
            "_emit_preview_accepted_side_effects",
        ):
            setattr(ctl, name, getattr(AppController, name).__get__(ctl))
        return ctl

    @staticmethod
    def _payload(token, decoded, session=("img", "s"), is_final=True):
        return (token, session, decoded, is_final, True)

    # --- pairing ----------------------------------------------------------

    def test_accepted_frame_carries_the_mask_that_render_resolved(self):
        from faststack.imaging import mask_engine

        editor, _, mask_data, settings = self._editor()
        produced = []
        real = mask_engine.resolve_mask

        def spy(*a, **k):
            resolved = real(*a, **k)
            produced.append(resolved)
            return resolved

        with patch.object(mask_engine, "resolve_mask", spy):
            decoded = self._render(editor)

        self.assertEqual(len(produced), 1)
        self.assertIsNotNone(decoded.darken_mask)
        self.assertIs(decoded.darken_mask.mask, produced[0])
        self.assertEqual(
            (decoded.darken_mask.height, decoded.darken_mask.width),
            produced[0].shape,
        )
        self.assertEqual(decoded.darken_mask.mask_id, settings.mask_id)
        self.assertEqual(decoded.darken_mask.mask_revision, mask_data.revision)

    def test_published_mask_is_read_only(self):
        """Ownership: the worker shares, never mutates, and says so."""
        editor, _, _, _ = self._editor()
        decoded = self._render(editor)
        self.assertFalse(decoded.darken_mask.mask.flags["WRITEABLE"])
        with self.assertRaises(ValueError):
            decoded.darken_mask.mask[0, 0] = 0.5

    def test_no_mask_published_when_darkening_inactive(self):
        editor, _, _, settings = self._editor()
        settings.enabled = False
        self.assertIsNone(self._render(editor).darken_mask)

    def test_display_resize_keeps_the_mask_at_its_own_tier(self):
        editor, _, _, _ = self._editor()
        decoded = self._render(editor)
        resized = editor._resize_decoded_for_display(decoded, (36, 24))
        self.assertIsNotNone(resized.darken_mask)
        self.assertIs(resized.darken_mask.mask, decoded.darken_mask.mask)
        self.assertEqual(resized.width, 36)
        self.assertEqual(resized.darken_mask.width, decoded.darken_mask.width)

    # --- stale-result protection -----------------------------------------

    def test_late_old_result_cannot_replace_a_newer_mask(self):
        """A: stroke; B: stroke+1; B accepted; A lands late."""
        editor, _, mask_data, _ = self._editor()
        decoded_a = self._render(editor)
        mask_data.add_stroke(MaskStroke([(0.2, 0.2)], 0.1, "add"))
        editor._edits_rev += 1
        decoded_b = self._render(editor)
        self.assertIsNot(decoded_a.darken_mask.mask, decoded_b.darken_mask.mask)

        ctl = self._controller(editor, mask_data)
        ctl._preview_token = 1
        ctl._apply_preview_result(self._payload(1, decoded_a))
        self.assertIs(ctl._accepted_darken_mask, decoded_a.darken_mask)

        # A newer render is kicked and accepted...
        ctl._preview_token = 2
        ctl._preview_inflight = True
        ctl._apply_preview_result(self._payload(2, decoded_b))
        self.assertIs(ctl._accepted_darken_mask, decoded_b.darken_mask)

        # ...and only now does the older render finish.
        ctl._preview_inflight = True
        ctl._apply_preview_result(self._payload(1, decoded_a))
        self.assertIs(
            ctl._accepted_darken_mask,
            decoded_b.darken_mask,
            "a losing render replaced the accepted mask",
        )

    def test_pending_newer_request_rejects_the_in_flight_mask(self):
        editor, _, mask_data, _ = self._editor()
        decoded = self._render(editor)
        ctl = self._controller(editor, mask_data)
        ctl._preview_pending = True  # a newer kick is already queued
        ctl._apply_preview_result(self._payload(1, decoded))
        self.assertIsNone(ctl._accepted_darken_mask)

    def test_session_change_rejects_the_mask(self):
        """Switching image mid-render: the result belongs to the old session."""
        editor, _, mask_data, _ = self._editor()
        decoded = self._render(editor)
        ctl = self._controller(editor, mask_data)
        ctl._get_current_live_preview_session_key = Mock(return_value=("other", "s2"))
        ctl._apply_preview_result(self._payload(1, decoded))
        self.assertIsNone(ctl._accepted_darken_mask)

    def test_published_mask_is_dropped_when_the_session_moves_on(self):
        editor, _, mask_data, _ = self._editor()
        decoded = self._render(editor)
        ctl = self._controller(editor, mask_data)
        ctl._apply_preview_result(self._payload(1, decoded))
        self.assertIsNotNone(ctl._accepted_darken_mask_for_display())
        ctl._get_current_live_preview_session_key = Mock(return_value=("other", "s2"))
        self.assertIsNone(ctl._accepted_darken_mask_for_display())

    def test_invalidate_forgets_the_published_mask(self):
        editor, _, mask_data, _ = self._editor()
        ctl = self._controller(editor, mask_data)
        ctl._apply_preview_result(self._payload(1, self._render(editor)))
        ctl._invalidate_accepted_darken_mask()
        self.assertIsNone(ctl._accepted_darken_mask_for_display())

    # --- no second render -------------------------------------------------

    def test_accepting_a_preview_does_not_re_render(self):
        """The whole point: side effects run with zero extra pipeline work."""
        from faststack.imaging import mask_engine
        from faststack.imaging.editor import ImageEditor

        editor, _, mask_data, _ = self._editor()
        decoded = self._render(editor)
        ctl = self._controller(editor, mask_data)

        applies, resolves = [], []
        with (
            patch.object(
                ImageEditor,
                "_apply_edits",
                side_effect=lambda *a, **k: applies.append(1),
                autospec=True,
            ),
            patch.object(
                mask_engine,
                "resolve_mask",
                side_effect=lambda *a, **k: resolves.append(1),
            ),
        ):
            ctl._apply_preview_result(self._payload(1, decoded))
            ctl._emit_preview_accepted_side_effects()

        self.assertEqual(applies, [], "accepting a frame re-ran _apply_edits")
        self.assertEqual(resolves, [], "accepting a frame re-resolved the mask")
        self.assertIsNotNone(ctl.ui_state._darken_overlay_image)

    def test_overlay_recolour_reuses_the_same_mask(self):
        from faststack.imaging import mask_engine
        from faststack.imaging.editor import ImageEditor

        editor, _, mask_data, _ = self._editor()
        ctl = self._controller(editor, mask_data)
        ctl._apply_preview_result(self._payload(1, self._render(editor)))
        ctl._update_darken_overlay()
        first_gen = ctl.ui_state._darken_overlay_generation
        published = ctl._accepted_darken_mask.mask

        mask_data.overlay_color = (0, 255, 0)
        applies, resolves = [], []
        with (
            patch.object(
                ImageEditor,
                "_apply_edits",
                side_effect=lambda *a, **k: applies.append(1),
                autospec=True,
            ),
            patch.object(
                mask_engine,
                "resolve_mask",
                side_effect=lambda *a, **k: resolves.append(1),
            ),
        ):
            ctl._update_darken_overlay()

        self.assertEqual(applies, [])
        self.assertEqual(resolves, [])
        self.assertIs(ctl._accepted_darken_mask.mask, published)
        self.assertGreater(ctl.ui_state._darken_overlay_generation, first_gen)
        # The green channel of the ARGB32 (BGRA in memory) buffer is now set.
        argb = np.frombuffer(ctl._darken_overlay_buffer, dtype=np.uint8)
        self.assertEqual(int(argb[1]), 255)
        self.assertEqual(int(argb[2]), 0)

    def test_overlay_cleared_when_strokes_are_gone(self):
        editor, _, mask_data, _ = self._editor()
        ctl = self._controller(editor, mask_data)
        ctl._apply_preview_result(self._payload(1, self._render(editor)))
        ctl._update_darken_overlay()
        self.assertIsNotNone(ctl.ui_state._darken_overlay_image)
        mask_data.clear_strokes()
        ctl._update_darken_overlay()
        self.assertIsNone(ctl.ui_state._darken_overlay_image)

    def test_stale_stroke_revision_keeps_the_last_good_overlay(self):
        """A stroke lands before its render does; do not show the old mask."""
        editor, _, mask_data, _ = self._editor()
        ctl = self._controller(editor, mask_data)
        ctl._apply_preview_result(self._payload(1, self._render(editor)))
        ctl._update_darken_overlay()
        gen = ctl.ui_state._darken_overlay_generation

        mask_data.add_stroke(MaskStroke([(0.8, 0.8)], 0.1, "add"))
        ctl._update_darken_overlay()
        self.assertEqual(ctl.ui_state._darken_overlay_generation, gen)

    # --- resolution tiers and edits ---------------------------------------

    def test_each_render_tier_publishes_its_own_mask(self):
        editor, _, _, _ = self._editor()
        drag = self._render(editor, downscale_long_edge=36)
        editor._edits_rev += 1
        hq = self._render(editor)

        self.assertEqual((drag.darken_mask.height, drag.darken_mask.width), (24, 36))
        self.assertEqual((hq.darken_mask.height, hq.darken_mask.width), (48, 72))
        self.assertEqual((drag.height, drag.width), (24, 36))
        self.assertEqual((hq.height, hq.width), (48, 72))

    def test_stroke_change_publishes_a_new_mask(self):
        editor, _, mask_data, _ = self._editor()
        before = self._render(editor).darken_mask
        mask_data.add_stroke(MaskStroke([(0.15, 0.15)], 0.12, "add"))
        after = self._render(editor).darken_mask

        self.assertNotEqual(before.mask_revision, after.mask_revision)
        self.assertIsNot(before.mask, after.mask)
        self.assertFalse(np.array_equal(before.mask, after.mask))

    def test_darken_amount_reuses_the_resolved_mask(self):
        """The strength slider changes pixels but not the mask."""
        editor, _, _, settings = self._editor()
        before = self._render(editor).darken_mask
        settings.darken_amount = 0.9
        editor._edits_rev += 1
        after = self._render(editor).darken_mask
        self.assertIs(before.mask, after.mask)

    def test_export_resolves_its_own_mask(self):
        """Export must not consume the interactive preview's published mask."""
        editor, _, _, _ = self._editor()
        preview = self._render(editor).darken_mask

        full = np.full((96, 144, 3), 0.45, dtype=np.float32)
        full[12:20, 16:24] = (0.95, 0.12, 0.10)
        editor.float_image = full
        export_state = {}
        editor._apply_edits(
            full,
            edits=dict(editor.current_edits),
            for_export=True,
            cache_context={},
            render_state_out=export_state,
        )
        exported = export_state["resolved_darken_mask"]

        self.assertIsNot(exported.mask, preview.mask)
        self.assertEqual((exported.height, exported.width), (96, 144))
        self.assertEqual((preview.height, preview.width), (48, 72))


if __name__ == "__main__":
    unittest.main()
