"""Tests for the dodge & burn stroke masks and their transforms."""

import unittest

from faststack.imaging.mask import MaskData, MaskStroke
from faststack.imaging.mask_engine import (
    forward_transform,
    inverse_transform,
)


class TestMaskStroke(unittest.TestCase):
    def test_create_stroke(self):
        s = MaskStroke(points=[(0.1, 0.2), (0.3, 0.4)], radius=0.05)
        self.assertEqual(len(s.points), 2)
        self.assertEqual(s.mode, "paint")
        self.assertAlmostEqual(s.hardness, 0.5)

    def test_serialise_round_trip(self):
        s = MaskStroke(points=[(0.5, 0.5)], radius=0.1, hardness=0.8, mode="erase")
        s2 = MaskStroke.from_dict(s.to_dict())
        self.assertEqual(s2.mode, "erase")
        self.assertAlmostEqual(s2.radius, 0.1)
        self.assertAlmostEqual(s2.hardness, 0.8)
        self.assertEqual(s2.points, [(0.5, 0.5)])


class TestMaskData(unittest.TestCase):
    def test_revision_tracking(self):
        md = MaskData()
        self.assertEqual(md.revision, 0)
        self.assertFalse(md.has_strokes())

        md.add_stroke(MaskStroke([(0.5, 0.5)], 0.05))
        self.assertEqual(md.revision, 1)
        self.assertTrue(md.has_strokes())

        md.add_stroke(MaskStroke([(0.2, 0.2)], 0.05, mode="erase"))
        self.assertEqual(md.revision, 2)

        removed = md.undo_last_stroke()
        self.assertIsNotNone(removed)
        self.assertEqual(removed.mode, "erase")
        self.assertEqual(md.revision, 3)
        self.assertEqual(len(md.strokes), 1)

        md.clear_strokes()
        self.assertEqual(md.revision, 4)
        self.assertFalse(md.has_strokes())

    def test_serialise_round_trip(self):
        md = MaskData()
        md.add_stroke(MaskStroke([(0.1, 0.2)], 0.03))
        md.live_stroke = MaskStroke([(0.9, 0.9)], 0.03)

        md2 = MaskData.from_dict(md.to_dict())
        self.assertEqual(len(md2.strokes), 1)
        self.assertIsNone(md2.live_stroke)  # never persisted
        self.assertNotEqual(md2.uid, md.uid)


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


class TestExportSnapshot(unittest.TestCase):
    def _editor_with_layer(self):
        import tempfile

        from PIL import Image as PILImage

        from faststack.imaging.editor import ImageEditor

        editor = ImageEditor()
        f = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        f.close()
        PILImage.new("RGB", (50, 50), color=(128, 128, 128)).save(f.name)
        editor.load_image(f.name)
        self.addCleanup(__import__("os").unlink, f.name)

        md = MaskData()
        md.add_stroke(MaskStroke([(0.5, 0.5)], 0.3))
        editor._mask_assets["L1"] = md
        layers = [{"id": "L1", "visible": True, "recipe": {"exposure": 0.5}}]
        editor.set_local_layers(layers)
        return editor, md, layers

    def test_snapshot_captures_immutable_mask_state(self):
        """snapshot_for_export deep-copies stroke masks and uses a fresh cache."""
        editor, md, layers = self._editor_with_layer()
        md.live_stroke = MaskStroke([(0.1, 0.1)], 0.3)

        snapshot = editor.snapshot_for_export()

        snap_mask = snapshot["mask_override"]["L1"]
        self.assertIsNot(snap_mask, md, "MaskData should be deep-copied")
        self.assertIsNone(snap_mask.live_stroke, "half-painted stroke excluded")
        self.assertIs(snapshot["edits"]["local_layers"], layers)
        self.assertIsNot(
            snapshot["export_cache"],
            editor._local_density_cache,
            "Export should use a fresh cache, not the shared preview cache",
        )
        self.assertIn("main_exif", snapshot)
        self.assertIsNotNone(snapshot["filepath_snapshot"])

    def test_snapshot_without_masks_never_uses_live_masks(self):
        editor, _md, _layers = self._editor_with_layer()
        editor._mask_assets.clear()
        snapshot = editor.snapshot_for_export()
        self.assertEqual(snapshot["mask_override"], {})
        self.assertIsNotNone(snapshot["export_cache"])

    def test_mutation_after_snapshot_does_not_affect_export(self):
        """Modifying editor state after snapshot must not change saved output."""
        editor, md, _layers = self._editor_with_layer()
        snapshot = editor.snapshot_for_export()

        editor.set_local_layers(
            [{"id": "L1", "visible": True, "recipe": {"exposure": 1.0}}]
        )
        md.add_stroke(MaskStroke([(0.1, 0.1)], 0.5))
        editor._mask_assets.clear()

        snap_layers = snapshot["edits"]["local_layers"]
        self.assertAlmostEqual(snap_layers[0]["recipe"]["exposure"], 0.5)
        self.assertEqual(len(snapshot["mask_override"]["L1"].strokes), 1)

    def test_navigation_after_snapshot_does_not_affect_export(self):
        """Clearing editor state (simulating navigation) after snapshot must
        not prevent save_from_snapshot from working."""
        import tempfile

        from PIL import Image as PILImage

        editor, _md, _layers = self._editor_with_layer()
        snapshot = editor.snapshot_for_export()

        editor.clear()
        self.assertIsNone(editor.float_image)
        self.assertEqual(len(editor._mask_assets), 0)

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f2:
            PILImage.new("RGB", (50, 50), color=(150, 150, 150)).save(f2.name)
        self.addCleanup(__import__("os").unlink, f2.name)
        editor.load_image(f2.name)

        result = editor.save_from_snapshot(snapshot)
        self.assertIsNotNone(result)


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
        mock_controller.ui_state._local_overlay_image = None

        provider = ImageProvider(mock_controller)
        transparent = provider.requestImage("mask_overlay/test", None, None)

        # Verify it has zero alpha (i.e. fully transparent)
        pixel = transparent.pixelColor(0, 0)
        self.assertEqual(pixel.alpha(), 0)

        # Verify it is NOT RGB888 format (the old placeholder was)
        self.assertEqual(transparent.format(), QImage.Format.Format_ARGB32)



if __name__ == "__main__":
    unittest.main()
