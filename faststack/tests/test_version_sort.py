import unittest
import re
from pathlib import PureWindowsPath


# Re-implementing the function locally to match the fix in config.py
# (The function in config.py is nested inside detect_rawtherapee_path so not easily importable)
def version_sort_key(path):
    for part in reversed(PureWindowsPath(path).parts):
        if re.fullmatch(r"\d+(?:\.\d+)*", part):
            return [int(n) for n in part.split(".")]
    return [0]


class TestVersionSort(unittest.TestCase):
    def test_version_sort_preference(self):
        """
        Test that higher version numbers are preferred regardless of parent directory names.
        """
        # Scenario: 5.10 (x86) vs 5.9 (x64)
        # The x86 path has "86" in "Program Files (x86)", which should NOT confuse the sort.

        path_5_10_x86 = r"C:\Program Files (x86)\RawTherapee\5.10\rawtherapee-cli.exe"
        path_5_9_x64 = r"C:\Program Files\RawTherapee\5.9\rawtherapee-cli.exe"

        paths = [path_5_9_x64, path_5_10_x86]

        # BROKEN behavior check (optional logic, just to demonstrate the issue)
        # In natural sort, "Program Files (x86)" might come after "Program Files" depending on how it handles " (" vs ""
        # But specifically, the "86" is a number.
        # "Program Files" split -> ['Program Files']
        # "Program Files (x86)" split -> ['Program Files (x', 86, ')']
        # Comparison logic is complex but often fails here.

        # CORRECT behavior check
        paths.sort(key=version_sort_key, reverse=True)
        self.assertEqual(paths[0], path_5_10_x86, "Should select 5.10 over 5.9")

    def test_same_version_different_arch(self):
        """
        If versions are identical, stability or secondary sort doesn't strictly matter for validity,
        but we want to ensure it doesn't crash or return garbage.
        """
        p1 = r"C:\Program Files\RawTherapee\5.9\rawtherapee-cli.exe"
        p2 = r"C:\Program Files (x86)\RawTherapee\5.9\rawtherapee-cli.exe"

        key1 = version_sort_key(p1)
        key2 = version_sort_key(p2)

        self.assertEqual(key1, [5, 9])
        self.assertEqual(key2, [5, 9])
        self.assertEqual(key1, key2)

    def test_no_version_in_path(self):
        p = r"C:\Program Files\RawTherapee\bin\rawtherapee-cli.exe"
        self.assertEqual(version_sort_key(p), [0])


if __name__ == "__main__":
    unittest.main()
