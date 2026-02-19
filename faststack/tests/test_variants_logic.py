import unittest
import os
from pathlib import Path
from faststack.io.variants import build_variant_map, VariantGroup, norm_path


class TestVariantsLogic(unittest.TestCase):
    def test_main_selection_priority(self):
        """Verify Main selection priority: Non-dev > Dev > Backup."""

        # Case 1: Pure Main exists
        paths = [
            Path("img.jpg"),
            Path("img-developed.jpg"),
            Path("img-backup.jpg"),
        ]
        groups = build_variant_map(paths)
        group = groups["img"]

        self.assertEqual(group.main_path.name, "img.jpg")
        self.assertEqual(group.developed_path.name, "img-developed.jpg")
        self.assertEqual(group.backup_paths[1].name, "img-backup.jpg")

    def test_main_selection_orphan_developed(self):
        """Verify Developed is chosen as Main if no pure Main exists."""

        paths = [
            Path("img-developed.jpg"),
            Path("img-backup.jpg"),
        ]
        groups = build_variant_map(paths)
        group = groups["img"]

        # developed becomes Main because it's Tier 1 (vs Backup Tier 2)
        self.assertEqual(group.main_path.name, "img-developed.jpg")
        # It is ALSO the developed path
        self.assertEqual(group.developed_path.name, "img-developed.jpg")

    def test_main_selection_orphan_backup(self):
        """Verify Backup is chosen as Main if nothing else exists."""

        paths = [
            Path("img-backup.jpg"),
            Path("img-backup2.jpg"),
        ]
        groups = build_variant_map(paths)
        group = groups["img"]

        self.assertEqual(group.main_path.name, "img-backup.jpg")  # Lowest backup
        self.assertIsNone(group.developed_path)

    def test_backup_sorting(self):
        """Verify backups are correctly identified and sorted."""
        paths = [
            Path("img-backup2.jpg"),
            Path("img.jpg"),
            Path("img-backup10.jpg"),
            Path("img-backup.jpg"),  # implies 1
        ]
        groups = build_variant_map(paths)
        group = groups["img"]

        self.assertEqual(len(group.backup_paths), 3)
        self.assertEqual(group.backup_paths[1].name, "img-backup.jpg")
        self.assertEqual(group.backup_paths[2].name, "img-backup2.jpg")
        self.assertEqual(group.backup_paths[10].name, "img-backup10.jpg")

    def test_developed_backup_handling(self):
        """Verify developed backups are handled correctly."""
        # Policy: developed-backup is "developed" candidate, AND "backup" candidate.
        # But _select_developed prefers non-backup developed.

        paths = [
            Path("img.jpg"),
            Path("img-developed.jpg"),
            Path("img-backup-developed.jpg"),  # This is a backup AND developed?
            # Note: parse_variant_stem handles "-developed" then "-backup".
            # "img-backup-developed" -> stem "img-backup". is_developed=True.
            # Then "img-backup" -> group "img", backup=1.
        ]

        groups = build_variant_map(paths)
        group = groups["img"]

        self.assertEqual(group.main_path.name, "img.jpg")
        self.assertEqual(group.developed_path.name, "img-developed.jpg")

        # Check that backup list contains the backup-developed file
        # It has backup_number=1.
        self.assertEqual(group.backup_paths[1].name, "img-backup-developed.jpg")

    def test_path_normalization(self):
        """Verify path normalization (case-insensitivity, absolute paths)."""
        if os.name == "nt":
            p1 = Path("C:/Test/File.JPG")
            p2 = Path("C:/Test/file.jpg")
            n1 = Path(norm_path(p1))
            n2 = Path(norm_path(p2))
            # On Windows, these should match after normalization
            self.assertEqual(n1, n2)
            self.assertTrue(n1.is_absolute())
        else:
            # On POSIX, case is preserved
            p1 = Path("/tmp/File.JPG")
            p2 = Path("/tmp/file.jpg")
            n1 = norm_path(p1)
            n2 = norm_path(p2)
            self.assertNotEqual(n1, n2)
            self.assertTrue(Path(n1).is_absolute())


if __name__ == "__main__":
    unittest.main()
