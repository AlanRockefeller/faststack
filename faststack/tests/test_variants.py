"""Tests for variant (backup + developed) parsing, grouping, and integration."""

import os
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from faststack.io.variants import (
    parse_variant_stem,
    build_variant_map,
    build_badge_list,
    norm_path,
    VariantInfo,
    VariantGroup,
)


# ── parse_variant_stem ──────────────────────────────────────────────────────


class TestParseVariantStem:
    """Tests for parse_variant_stem()."""

    def test_plain_stem(self):
        key, dev, backup = parse_variant_stem("photo")
        assert key == "photo"
        assert dev is False
        assert backup is None

    def test_developed(self):
        key, dev, backup = parse_variant_stem("photo-developed")
        assert key == "photo"
        assert dev is True
        assert backup is None

    def test_developed_case_insensitive(self):
        key, dev, backup = parse_variant_stem("photo-Developed")
        assert key == "photo"
        assert dev is True
        assert backup is None

    def test_backup_no_number(self):
        key, dev, backup = parse_variant_stem("photo-backup")
        assert key == "photo"
        assert dev is False
        assert backup == 1

    def test_backup_with_number(self):
        key, dev, backup = parse_variant_stem("photo-backup2")
        assert key == "photo"
        assert dev is False
        assert backup == 2

    def test_backup_high_number(self):
        key, dev, backup = parse_variant_stem("photo-backup33")
        assert key == "photo"
        assert dev is False
        assert backup == 33

    def test_developed_and_backup(self):
        """photo-developed-backup2 → developed + backup 2."""
        key, dev, backup = parse_variant_stem("photo-developed-backup2")
        assert key == "photo"
        assert dev is True
        assert backup == 2

    def test_backup_and_developed(self):
        """photo-backup2-developed → developed + backup 2.
        Note: -developed token comes after -backup, but -backup is at end of stripped stem.
        """
        key, dev, backup = parse_variant_stem("photo-backup2-developed")
        assert key == "photo"
        assert dev is True
        assert backup == 2

    def test_not_developed_substring(self):
        """'photo-undeveloped' should NOT match -developed (substring)."""
        key, dev, backup = parse_variant_stem("photo-undeveloped")
        assert dev is False
        assert key == "photo-undeveloped"

    def test_not_backup_prefix(self):
        """'mybackup-photo' should NOT match -backup (not at end)."""
        key, dev, backup = parse_variant_stem("mybackup-photo")
        assert dev is False
        assert backup is None
        assert key == "mybackup-photo"

    def test_case_insensitive_mixed(self):
        key, dev, backup = parse_variant_stem("photo-Developed-Backup2")
        assert dev is True
        assert backup == 2
        assert key == "photo"

    def test_backup_case_insensitive(self):
        key, dev, backup = parse_variant_stem("photo-BACKUP3")
        assert backup == 3
        assert key == "photo"

    def test_stem_with_dashes(self):
        """Stems with dashes should work correctly."""
        key, dev, backup = parse_variant_stem("my-cool-photo-developed")
        assert key == "my-cool-photo"
        assert dev is True
        assert backup is None

    def test_stem_with_dashes_and_backup(self):
        key, dev, backup = parse_variant_stem("my-cool-photo-backup2")
        assert key == "my-cool-photo"
        assert dev is False
        assert backup == 2


# ── build_variant_map ────────────────────────────────────────────────────────


class TestBuildVariantMap:
    """Tests for build_variant_map()."""

    def test_singleton_group(self):
        """A single file creates a group with itself as main."""
        paths = [Path("/dir/photo.jpg")]
        vmap = build_variant_map(paths)
        assert "photo" in vmap
        group = vmap["photo"]
        # Paths are normalized in build_variant_map
        assert group.main_path == Path(norm_path(paths[0]))
        assert len(group.all_files) == 1

    def test_main_selection_prefers_non_backup_non_developed(self):
        paths = [
            Path("/dir/photo.jpg"),
            Path("/dir/photo-developed.jpg"),
            Path("/dir/photo-backup.jpg"),
        ]
        vmap = build_variant_map(paths)
        group = vmap["photo"]
        assert group.main_path == Path(norm_path(Path("/dir/photo.jpg")))

    def test_main_selection_fallback_to_developed(self):
        """If no non-backup non-developed exists, main is non-backup developed."""
        paths = [
            Path("/dir/photo-developed.jpg"),
            Path("/dir/photo-backup.jpg"),
        ]
        vmap = build_variant_map(paths)
        group = vmap["photo"]
        assert group.main_path == Path(norm_path(Path("/dir/photo-developed.jpg")))

    def test_developed_path_selection(self):
        paths = [
            Path("/dir/photo.jpg"),
            Path("/dir/photo-developed.jpg"),
            Path("/dir/photo-backup.jpg"),
        ]
        vmap = build_variant_map(paths)
        group = vmap["photo"]
        assert group.developed_path == Path(norm_path(Path("/dir/photo-developed.jpg")))

    def test_backup_paths(self):
        paths = [
            Path("/dir/photo.jpg"),
            Path("/dir/photo-backup.jpg"),
            Path("/dir/photo-backup2.jpg"),
        ]
        vmap = build_variant_map(paths)
        group = vmap["photo"]
        assert 1 in group.backup_paths
        assert 2 in group.backup_paths
        assert group.backup_paths[1] == Path(norm_path(Path("/dir/photo-backup.jpg")))
        assert group.backup_paths[2] == Path(norm_path(Path("/dir/photo-backup2.jpg")))

    def test_grouping_case_insensitive(self):
        """Files with different case stems should group together."""
        paths = [
            Path("/dir/Photo.jpg"),
            Path("/dir/photo-backup.jpg"),
        ]
        vmap = build_variant_map(paths)
        # Should be grouped under casefolded key "photo"
        assert "photo" in vmap
        group = vmap["photo"]
        assert len(group.all_files) == 2

    def test_no_developed_when_only_main(self):
        paths = [Path("/dir/photo.jpg")]
        vmap = build_variant_map(paths)
        group = vmap["photo"]
        assert group.developed_path is None

    def test_complex_variant_set(self):
        """Full set: main, developed, backup, backup2, developed-backup."""
        paths = [
            Path("/dir/photo.jpg"),
            Path("/dir/photo-developed.jpg"),
            Path("/dir/photo-backup.jpg"),
            Path("/dir/photo-backup2.jpg"),
            Path("/dir/photo-developed-backup.jpg"),
        ]
        vmap = build_variant_map(paths)
        group = vmap["photo"]

        assert group.main_path == Path(norm_path(Path("/dir/photo.jpg")))
        assert group.developed_path == Path(norm_path(Path("/dir/photo-developed.jpg")))
        assert group.backup_paths[1] == Path(norm_path(Path("/dir/photo-backup.jpg")))
        assert group.backup_paths[2] == Path(norm_path(Path("/dir/photo-backup2.jpg")))


# ── build_badge_list ─────────────────────────────────────────────────────────


class TestBuildBadgeList:
    """Tests for build_badge_list()."""

    def test_badge_order_main_d_backups(self):
        paths = [
            Path("/dir/photo.jpg"),
            Path("/dir/photo-developed.jpg"),
            Path("/dir/photo-backup.jpg"),
            Path("/dir/photo-backup2.jpg"),
        ]
        vmap = build_variant_map(paths)
        group = vmap["photo"]
        badges = build_badge_list(group)

        labels = [b["label"] for b in badges]
        assert labels == ["Main", "D", "Bk", "Bk2"]

    def test_badge_no_duplicates(self):
        """If developed IS main (no non-dev exists), D badge should not duplicate Main."""
        paths = [
            Path("/dir/photo-developed.jpg"),
            Path("/dir/photo-backup.jpg"),
        ]
        vmap = build_variant_map(paths)
        group = vmap["photo"]
        badges = build_badge_list(group)
        labels = [b["label"] for b in badges]
        # photo-developed.jpg is main AND developed, so D badge is suppressed
        assert "Main" in labels
        assert "D" not in labels

    def test_singleton_no_badges(self):
        paths = [Path("/dir/photo.jpg")]
        vmap = build_variant_map(paths)
        group = vmap["photo"]
        badges = build_badge_list(group)
        # Only Main badge
        assert len(badges) == 1
        assert badges[0]["label"] == "Main"


# ── Integration: indexer find_images_with_variants ───────────────────────────


class TestFindImagesWithVariants:
    """Tests for find_images_with_variants() filtering."""

    def test_filters_developed_from_visible(self, tmp_path):
        """Developed files should be removed from visible list when main exists."""
        from faststack.io.indexer import find_images_with_variants

        # Create test files
        (tmp_path / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0")  # minimal JPEG
        (tmp_path / "photo-developed.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        images, vmap = find_images_with_variants(tmp_path)
        names = [img.path.name for img in images]

        assert "photo.jpg" in names
        assert "photo-developed.jpg" not in names

    def test_keeps_orphan_developed(self, tmp_path):
        """If no non-developed exists, developed IS main and stays visible."""
        from faststack.io.indexer import find_images_with_variants

        (tmp_path / "photo-developed.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        images, vmap = find_images_with_variants(tmp_path)
        names = [img.path.name for img in images]
        assert "photo-developed.jpg" in names

    def test_backups_hidden(self, tmp_path):
        """Backup files should be hidden from visible list."""
        from faststack.io.indexer import find_images_with_variants

        (tmp_path / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        (tmp_path / "photo-backup.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        (tmp_path / "photo-backup2.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        images, vmap = find_images_with_variants(tmp_path)
        names = [img.path.name for img in images]

        assert "photo.jpg" in names
        assert "photo-backup.jpg" not in names
        assert "photo-backup2.jpg" not in names

    def test_variant_flags_set(self, tmp_path):
        """ImageFile should have has_backups/has_developed flags set."""
        from faststack.io.indexer import find_images_with_variants

        (tmp_path / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        (tmp_path / "photo-developed.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        (tmp_path / "photo-backup.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        images, vmap = find_images_with_variants(tmp_path)
        photo = [img for img in images if img.path.name == "photo.jpg"][0]

        assert photo.has_backups is True
        assert photo.has_developed is True

    def test_no_flags_for_singles(self, tmp_path):
        """Single images should not have variant flags."""
        from faststack.io.indexer import find_images_with_variants

        (tmp_path / "lonely.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        images, vmap = find_images_with_variants(tmp_path)
        lonely = [img for img in images if img.path.name == "lonely.jpg"][0]

        assert lonely.has_backups is False
        assert lonely.has_developed is False

    def test_variant_map_built(self, tmp_path):
        """Variant map should contain all files including backups."""
        from faststack.io.indexer import find_images_with_variants

        (tmp_path / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        (tmp_path / "photo-backup.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        images, vmap = find_images_with_variants(tmp_path)
        assert "photo" in vmap
        group = vmap["photo"]
        assert len(group.all_files) == 2


# ── Thumbnail model roles ───────────────────────────────────────────────────


class TestThumbnailModelVariantRoles:
    """Tests that ThumbnailModel propagates variant flags."""

    def test_entry_has_variant_fields(self):
        from faststack.thumbnail_view.model import ThumbnailEntry

        entry = ThumbnailEntry(
            path=Path("/dir/photo.jpg"),
            name="photo.jpg",
            is_folder=False,
            has_backups=True,
            has_developed=True,
        )
        assert entry.has_backups is True
        assert entry.has_developed is True

    def test_entry_defaults_false(self):
        from faststack.thumbnail_view.model import ThumbnailEntry

        entry = ThumbnailEntry(
            path=Path("/dir/photo.jpg"),
            name="photo.jpg",
            is_folder=False,
        )
        assert entry.has_backups is False
        assert entry.has_developed is False
