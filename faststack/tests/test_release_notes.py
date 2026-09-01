"""Tests for the ChangeLog.md -> GitHub Release notes extraction.

The workflow (.github/workflows/build-executables.yml) shells out to
tools/release_notes.py, so the extraction is validated here independently of
GitHub Actions.
"""

import unittest
from pathlib import Path

from tools.release_notes import (
    build_number,
    extract_changelog_section,
    release_title,
    render_release_notes,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

SAMPLE_CHANGELOG = """# ChangeLog

Todo: some notes that are not a version.

## 1.6.9 (2026-09-01)

- Shiny new thing.
- Another thing.

## 1.6.8 (2026-08-10)

- Auto levels no longer washes out photos.

### Performance

- Faster startup.

## 1.6.80 (2020-01-01)

- Should never be confused with 1.6.8.
"""


class TestBuildNumber(unittest.TestCase):
    def test_extracts_build_counter(self):
        self.assertEqual(build_number("v1.6.8-build1"), 1)
        self.assertEqual(build_number("v1.6.8-build12"), 12)
        self.assertEqual(build_number("v1.6.8_build3"), 3)

    def test_no_build_counter(self):
        self.assertIsNone(build_number("v1.6.8"))
        self.assertIsNone(build_number(""))


class TestExtractChangelogSection(unittest.TestCase):
    def test_extracts_the_matching_section_only(self):
        section = extract_changelog_section(SAMPLE_CHANGELOG, "1.6.9")
        self.assertIn("Shiny new thing.", section)
        self.assertNotIn("Auto levels", section)
        self.assertNotIn("## 1.6.8", section)

    def test_includes_subsections(self):
        section = extract_changelog_section(SAMPLE_CHANGELOG, "1.6.8")
        self.assertIn("Auto levels no longer washes out photos.", section)
        self.assertIn("### Performance", section)
        self.assertIn("Faster startup.", section)
        self.assertNotIn("1.6.80", section)

    def test_version_token_is_not_a_prefix_match(self):
        section = extract_changelog_section(SAMPLE_CHANGELOG, "1.6.80")
        self.assertIn("Should never be confused", section)

    def test_missing_version_returns_empty(self):
        self.assertEqual(extract_changelog_section(SAMPLE_CHANGELOG, "9.9.9"), "")
        self.assertEqual(extract_changelog_section("", "1.6.8"), "")
        self.assertEqual(extract_changelog_section(SAMPLE_CHANGELOG, ""), "")


class TestReleaseTitle(unittest.TestCase):
    def test_first_build_is_titled_as_the_version(self):
        self.assertEqual(release_title("v1.6.8"), "FastStack 1.6.8")
        self.assertEqual(release_title("v1.6.8-build1"), "FastStack 1.6.8")

    def test_later_builds_are_labelled_as_rebuilds(self):
        self.assertEqual(release_title("v1.6.8-build2"), "FastStack 1.6.8 (build 2)")
        self.assertEqual(release_title("v1.6.8-build11"), "FastStack 1.6.8 (build 11)")


class TestRenderReleaseNotes(unittest.TestCase):
    def test_uses_the_changelog_section_for_the_base_version(self):
        notes = render_release_notes(SAMPLE_CHANGELOG, "v1.6.8-build1")
        self.assertIn("What's new in 1.6.8", notes)
        self.assertIn("Auto levels no longer washes out photos.", notes)
        self.assertNotIn("Shiny new thing.", notes)
        self.assertIn("Full changelog:", notes)

    def test_rebuild_says_the_version_is_unchanged(self):
        notes = render_release_notes(SAMPLE_CHANGELOG, "v1.6.8-build3")
        self.assertIn("Packaging rebuild of FastStack 1.6.8 (build 3)", notes)
        self.assertIn("application version is unchanged", notes)
        # Still carries the real notes for anyone installing fresh.
        self.assertIn("Auto levels no longer washes out photos.", notes)

    def test_first_build_is_not_called_a_rebuild(self):
        notes = render_release_notes(SAMPLE_CHANGELOG, "v1.6.8-build1")
        self.assertNotIn("Packaging rebuild", notes)

    def test_missing_section_degrades_gracefully(self):
        notes = render_release_notes(SAMPLE_CHANGELOG, "v9.9.9-build1")
        self.assertIn("FastStack 9.9.9", notes)
        self.assertIn("no section for this version", notes)

    def test_real_changelog_has_notes_for_the_shipping_version(self):
        changelog = (REPO_ROOT / "ChangeLog.md").read_text(encoding="utf-8")
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        version = ""
        for line in pyproject.splitlines():
            if line.startswith("version = "):
                version = line.split('"')[1]
                break
        self.assertTrue(version, "could not read version from pyproject.toml")

        notes = render_release_notes(changelog, f"v{version}-build1")
        self.assertIn(f"What's new in {version}", notes)
        self.assertNotIn("no section for this version", notes)
        self.assertGreater(len(notes), 200)


if __name__ == "__main__":
    unittest.main()
