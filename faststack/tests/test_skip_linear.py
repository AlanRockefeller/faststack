"""Regression tests for the skip-linear and no-copy export optimizations.

These tests are intentionally minimal and self-contained (no cv2 dependency).
"""

import unittest

import numpy as np

from faststack.imaging.editor import ImageEditor


class TestSkipLinearOptimization(unittest.TestCase):
    """Tests for the _apply_edits skip-linear fast path."""

    def setUp(self):
        np.random.seed(42)
        self.editor = ImageEditor()
        # Deterministic float32 image in [0.1, 0.9] — avoids clip boundaries
        self.arr = np.random.rand(100, 100, 3).astype(np.float32) * 0.8 + 0.1

    def test_skip_linear_output_matches_full_pipeline(self):
        """Skip-linear uint8 output must match full-pipeline output within 1/255.

        Forces the full pipeline by injecting a tiny (below-perceptual) exposure
        value that is still above the 0.001 epsilon, so _skip_linear=False.
        Compares final uint8 frames; max abs diff must be <= 1.
        """
        edits_base = self.editor._initial_edits()
        edits_base["blacks"] = 0.4
        edits_base["whites"] = 0.3

        # Skip path: exposure == 0 → _skip_linear=True
        result_skip = self.editor._apply_edits(
            self.arr.copy(),
            edits=edits_base,
            for_export=True,
        )
        u8_skip = (np.clip(result_skip, 0.0, 1.0) * 255).astype(np.uint8)

        # Full path: exposure = 0.002 → _skip_linear=False (above 0.001 threshold)
        edits_full = dict(edits_base)
        edits_full["exposure"] = 0.002
        result_full = self.editor._apply_edits(
            self.arr.copy(),
            edits=edits_full,
            for_export=True,
        )
        u8_full = (np.clip(result_full, 0.0, 1.0) * 255).astype(np.uint8)

        max_diff = int(
            np.max(np.abs(u8_skip.astype(np.int16) - u8_full.astype(np.int16)))
        )
        self.assertLessEqual(
            max_diff,
            1,
            f"Skip-linear output diverged from full pipeline by {max_diff}/255",
        )

    def test_no_copy_path_does_not_mutate_float_image(self):
        """When _edits_can_share_input is True, save_image must not alter float_image.

        We can't easily call save_image (needs a real file), so we replicate the
        exact pattern: pass float_image directly (no .copy()) to _apply_edits
        with levels-only edits, then verify the source array is unchanged.
        """
        source = self.arr.copy()
        source_hash = source.data.tobytes().__hash__()

        edits = self.editor._initial_edits()
        edits["blacks"] = 0.5
        edits["whites"] = 0.3

        # Precondition: these edits qualify for no-copy
        self.assertTrue(
            ImageEditor._edits_can_share_input(edits),
            "_edits_can_share_input should be True for levels-only edits",
        )

        # Simulate the no-copy path
        _result = self.editor._apply_edits(source, edits=edits, for_export=True)

        # source must be identical (byte-for-byte)
        self.assertEqual(
            source.data.tobytes().__hash__(),
            source_hash,
            "float_image was mutated by _apply_edits on the no-copy path",
        )

    def test_edits_skip_linear_boundary(self):
        """Verify the 0.001 epsilon boundary for _edits_skip_linear."""
        edits = self.editor._initial_edits()

        # All zeros → skip
        self.assertTrue(ImageEditor._edits_skip_linear(edits))

        # Exactly at boundary → still skip
        edits["exposure"] = 0.001
        self.assertTrue(ImageEditor._edits_skip_linear(edits))

        # Just over → no skip
        edits["exposure"] = 0.0011
        self.assertFalse(ImageEditor._edits_skip_linear(edits))

    def test_edits_can_share_input_requires_no_geometry(self):
        """Geometry ops must disqualify the no-copy path."""
        edits = self.editor._initial_edits()
        self.assertTrue(ImageEditor._edits_can_share_input(edits))

        # Rotation
        edits_rot = dict(edits)
        edits_rot["rotation"] = 90
        self.assertFalse(ImageEditor._edits_can_share_input(edits_rot))

        # Straighten
        edits_str = dict(edits)
        edits_str["straighten_angle"] = 2.0
        self.assertFalse(ImageEditor._edits_can_share_input(edits_str))

        # Crop
        edits_crop = dict(edits)
        edits_crop["crop_box"] = (100, 100, 900, 900)
        self.assertFalse(ImageEditor._edits_can_share_input(edits_crop))

        # Vignette (in-place *=)
        edits_vig = dict(edits)
        edits_vig["vignette"] = 0.5
        self.assertFalse(ImageEditor._edits_can_share_input(edits_vig))


if __name__ == "__main__":
    unittest.main()
