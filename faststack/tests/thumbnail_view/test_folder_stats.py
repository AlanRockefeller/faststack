"""Tests for folder_stats module."""

import json
import pytest
from faststack.thumbnail_view.folder_stats import (
    FolderStats,
    read_folder_stats,
    clear_stats_cache,
    clear_raw_count_cache,
    _stats_cache,
    _scan_folder_files,
    _compute_coverage_buckets,
    count_images_in_folder,
    get_file_counts_by_extension,
)


@pytest.fixture
def temp_folder(tmp_path):
    """Create a temporary folder for testing."""
    return tmp_path


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear stats cache before each test."""
    clear_stats_cache()
    yield
    clear_stats_cache()


class TestFolderStats:
    """Tests for FolderStats dataclass."""

    def test_folder_stats_creation(self):
        """Test FolderStats can be created with valid values."""
        stats = FolderStats(
            total_images=100,
            stacked_count=25,
            uploaded_count=50,
            edited_count=10,
        )
        assert stats.total_images == 100
        assert stats.stacked_count == 25
        assert stats.uploaded_count == 50
        assert stats.edited_count == 10


class TestReadFolderStats:
    """Tests for read_folder_stats function."""

    def test_read_valid_faststack_json(self, temp_folder):
        """Test reading a valid faststack.json file."""
        json_path = temp_folder / "faststack.json"
        data = {
            "version": 2,
            "entries": {
                "IMG_001": {"stacked": True, "uploaded": False, "edited": False},
                "IMG_002": {"stacked": False, "uploaded": True, "edited": True},
                "IMG_003": {"stacked": True, "uploaded": True, "edited": False},
            },
        }
        json_path.write_text(json.dumps(data))

        stats = read_folder_stats(temp_folder)

        assert stats is not None
        assert stats.total_images == 3
        assert stats.stacked_count == 2
        assert stats.uploaded_count == 2
        assert stats.edited_count == 1

    def test_read_missing_faststack_json(self, temp_folder):
        """Test reading from a folder without faststack.json."""
        stats = read_folder_stats(temp_folder)
        assert stats is None

    def test_read_empty_entries(self, temp_folder):
        """Test reading a faststack.json with no entries."""
        json_path = temp_folder / "faststack.json"
        data = {"version": 2, "entries": {}}
        json_path.write_text(json.dumps(data))

        stats = read_folder_stats(temp_folder)

        assert stats is not None
        assert stats.total_images == 0
        assert stats.stacked_count == 0

    def test_read_corrupt_json(self, temp_folder):
        """Test reading a corrupt faststack.json file."""
        json_path = temp_folder / "faststack.json"
        json_path.write_text("{ invalid json }")

        stats = read_folder_stats(temp_folder)
        assert stats is None

    def test_read_missing_keys(self, temp_folder):
        """Test reading faststack.json with missing keys (old format)."""
        json_path = temp_folder / "faststack.json"
        data = {
            "entries": {
                "IMG_001": {},  # No flags
                "IMG_002": {"stacked": True},  # Only stacked
            }
        }
        json_path.write_text(json.dumps(data))

        stats = read_folder_stats(temp_folder)

        assert stats is not None
        assert stats.total_images == 2
        assert stats.stacked_count == 1
        assert stats.uploaded_count == 0
        assert stats.edited_count == 0

    def test_caching_by_mtime(self, temp_folder):
        """Test that results are cached by mtime_ns."""
        json_path = temp_folder / "faststack.json"
        data = {"version": 2, "entries": {"IMG_001": {"stacked": True}}}
        json_path.write_text(json.dumps(data))

        # First read
        stats1 = read_folder_stats(temp_folder)
        assert stats1 is not None
        assert stats1.stacked_count == 1

        # Check cache was populated
        assert len(_stats_cache) == 1

        # Second read should use cache
        stats2 = read_folder_stats(temp_folder)
        assert stats2 is stats1  # Same object from cache

    def test_cache_invalidation_on_mtime_change(self, temp_folder):
        """Test that cache is invalidated when file mtime changes."""
        json_path = temp_folder / "faststack.json"
        data = {"version": 2, "entries": {"IMG_001": {"stacked": True}}}
        json_path.write_text(json.dumps(data))

        # First read
        stats1 = read_folder_stats(temp_folder)
        assert stats1.stacked_count == 1

        # Modify file with explicit mtime change
        import os

        data["entries"]["IMG_002"] = {"stacked": True}
        json_path.write_text(json.dumps(data))
        # Set mtime to future to ensure cache invalidation
        new_time = json_path.stat().st_mtime + 1
        os.utime(json_path, (new_time, new_time))

        # Second read should get new data
        stats2 = read_folder_stats(temp_folder)
        assert stats2 is not stats1
        assert stats2.stacked_count == 2

    def test_invalid_entries_format(self, temp_folder):
        """Test reading faststack.json with invalid entries format."""
        json_path = temp_folder / "faststack.json"
        data = {"version": 2, "entries": "not a dict"}
        json_path.write_text(json.dumps(data))

        stats = read_folder_stats(temp_folder)
        assert stats is None

    def test_entry_with_non_dict_value(self, temp_folder):
        """Test reading entries where value is not a dict."""
        json_path = temp_folder / "faststack.json"
        data = {
            "version": 2,
            "entries": {
                "IMG_001": {"stacked": True},
                "IMG_002": "invalid",  # Should be skipped
            },
        }
        json_path.write_text(json.dumps(data))

        stats = read_folder_stats(temp_folder)

        assert stats is not None
        assert stats.total_images == 2  # Both entries counted
        assert stats.stacked_count == 1  # Only valid entry counted


class TestScanFolderFiles:
    """Tests for _scan_folder_files function."""

    def test_scan_empty_folder(self, temp_folder):
        """Test scanning an empty folder."""
        jpg_count, raw_count, jpg_files = _scan_folder_files(temp_folder)
        assert jpg_count == 0
        assert raw_count == 0
        assert jpg_files == []

    def test_scan_folder_with_jpgs(self, temp_folder):
        """Test scanning a folder with JPG files."""
        (temp_folder / "a.jpg").touch()
        (temp_folder / "b.jpeg").touch()
        (temp_folder / "c.png").touch()

        jpg_count, raw_count, jpg_files = _scan_folder_files(temp_folder)

        assert jpg_count == 3
        assert raw_count == 0
        assert sorted(jpg_files) == ["a.jpg", "b.jpeg", "c.png"]

    def test_scan_folder_with_raws(self, temp_folder):
        """Test scanning a folder with RAW files."""
        (temp_folder / "photo.orf").touch()
        (temp_folder / "photo.cr2").touch()
        (temp_folder / "photo.nef").touch()

        jpg_count, raw_count, jpg_files = _scan_folder_files(temp_folder)

        assert jpg_count == 0
        assert raw_count == 3
        assert jpg_files == []

    def test_scan_folder_mixed(self, temp_folder):
        """Test scanning a folder with both JPG and RAW files."""
        (temp_folder / "IMG_001.jpg").touch()
        (temp_folder / "IMG_001.orf").touch()
        (temp_folder / "IMG_002.jpg").touch()
        (temp_folder / "IMG_002.orf").touch()

        jpg_count, raw_count, jpg_files = _scan_folder_files(temp_folder)

        assert jpg_count == 2
        assert raw_count == 2
        assert sorted(jpg_files) == ["IMG_001.jpg", "IMG_002.jpg"]

    def test_scan_folder_case_insensitive(self, temp_folder):
        """Test that extensions are matched case-insensitively."""
        (temp_folder / "photo.JPG").touch()
        (temp_folder / "photo.Jpeg").touch()
        (temp_folder / "photo.ORF").touch()

        jpg_count, raw_count, jpg_files = _scan_folder_files(temp_folder)

        assert jpg_count == 2
        assert raw_count == 1

    def test_scan_folder_sorted_output(self, temp_folder):
        """Test that JPG files are sorted alphabetically."""
        (temp_folder / "zebra.jpg").touch()
        (temp_folder / "apple.jpg").touch()
        (temp_folder / "Banana.jpg").touch()

        _, _, jpg_files = _scan_folder_files(temp_folder)

        # Should be case-insensitive sorted
        assert jpg_files == ["apple.jpg", "Banana.jpg", "zebra.jpg"]


class TestComputeCoverageBuckets:
    """Tests for _compute_coverage_buckets function."""

    def test_empty_files(self):
        """Test with no files."""
        buckets = _compute_coverage_buckets([], {})
        assert buckets == []

    def test_single_file_uploaded(self):
        """Test with single uploaded file."""
        jpg_files = ["a.jpg"]
        entries = {"a": {"uploaded": True, "stacked": False}}

        buckets = _compute_coverage_buckets(jpg_files, entries, num_buckets=1)

        assert len(buckets) == 1
        assert buckets[0] == (1.0, 0.0, 0.0)  # uploaded, not stacked, not todo

    def test_single_file_stacked(self):
        """Test with single stacked file."""
        jpg_files = ["a.jpg"]
        entries = {"a": {"uploaded": False, "stacked": True}}

        buckets = _compute_coverage_buckets(jpg_files, entries, num_buckets=1)

        assert len(buckets) == 1
        assert buckets[0] == (0.0, 1.0, 0.0)  # not uploaded, stacked, not todo

    def test_even_distribution(self):
        """Test even distribution across buckets."""
        jpg_files = ["a.jpg", "b.jpg", "c.jpg", "d.jpg"]
        entries = {
            "a": {"uploaded": True},
            "b": {"uploaded": True},
            "c": {"uploaded": False},
            "d": {"uploaded": False},
        }

        buckets = _compute_coverage_buckets(jpg_files, entries, num_buckets=2)

        assert len(buckets) == 2
        # First bucket: a, b (both uploaded)
        assert buckets[0][0] == 1.0
        # Second bucket: c, d (neither uploaded)
        assert buckets[1][0] == 0.0

    def test_more_buckets_than_files(self):
        """Test when num_buckets > num_files."""
        jpg_files = ["a.jpg", "b.jpg"]
        entries = {"a": {"uploaded": True}, "b": {"uploaded": False}}

        buckets = _compute_coverage_buckets(jpg_files, entries, num_buckets=10)

        # Should reduce to 2 buckets (one per file)
        assert len(buckets) == 2

    def test_missing_entries(self):
        """Test files not in entries dict."""
        jpg_files = ["a.jpg", "b.jpg"]
        entries = {"a": {"uploaded": True}}  # b is missing

        buckets = _compute_coverage_buckets(jpg_files, entries, num_buckets=2)

        assert len(buckets) == 2
        assert buckets[0][0] == 1.0  # a: uploaded
        assert buckets[1][0] == 0.0  # b: not in entries, defaults to False

    def test_coverage_buckets_in_stats(self, temp_folder):
        """Test that coverage_buckets is populated in FolderStats."""
        # Create JPG files
        (temp_folder / "a.jpg").touch()
        (temp_folder / "b.jpg").touch()

        # Create faststack.json with metadata
        json_path = temp_folder / "faststack.json"
        data = {
            "entries": {
                "a": {"uploaded": True, "stacked": False},
                "b": {"uploaded": False, "stacked": True},
            }
        }
        json_path.write_text(json.dumps(data))

        stats = read_folder_stats(temp_folder)

        assert stats is not None
        assert len(stats.coverage_buckets) > 0
        # With 2 files and default 40 buckets, should have 2 buckets
        assert len(stats.coverage_buckets) == 2

    def test_coverage_buckets_support_path_keys(self, temp_folder):
        """Path-aware sidecar keys should still contribute to sparkline coverage."""
        (temp_folder / "a.jpg").touch()
        json_path = temp_folder / "faststack.json"
        data = {"entries": {"a.jpg": {"uploaded": True}}}
        json_path.write_text(json.dumps(data))

        stats = read_folder_stats(temp_folder)

        assert stats is not None
        assert stats.coverage_buckets == [(1.0, 0.0, 0.0)]


class TestCountImagesInFolder:
    """Tests for count_images_in_folder function."""

    def test_count_empty_folder(self, temp_folder):
        """Test counting images in empty folder."""
        clear_raw_count_cache()
        stats = count_images_in_folder(temp_folder)
        assert stats is None  # No images

    def test_count_folder_with_images(self, temp_folder):
        """Test counting images in folder with files."""
        (temp_folder / "photo1.jpg").touch()
        (temp_folder / "photo2.orf").touch()

        clear_raw_count_cache()
        stats = count_images_in_folder(temp_folder)

        assert stats is not None
        assert stats.total_images == 2
        assert stats.jpg_count == 1
        assert stats.raw_count == 1
        # No faststack.json, so these should be 0
        assert stats.stacked_count == 0
        assert stats.uploaded_count == 0


class TestGetFileCountsByExtension:
    """Tests for get_file_counts_by_extension function."""

    def test_empty_folder(self, temp_folder):
        """Test counting in empty folder."""
        counts = get_file_counts_by_extension(temp_folder)
        assert counts == {}

    def test_count_by_extension(self, temp_folder):
        """Test counting files by extension (image extensions roll up to IMG)."""
        (temp_folder / "a.jpg").touch()
        (temp_folder / "b.jpg").touch()
        (temp_folder / "c.orf").touch()
        (temp_folder / "d.txt").touch()

        counts = get_file_counts_by_extension(temp_folder)

        assert counts["IMG"] == 2  # .jpg files roll up to IMG
        assert counts["ORF"] == 1
        assert counts["TXT"] == 1

    def test_jpg_extensions_rollup_to_img(self, temp_folder):
        """Test that .jpg, .jpeg, .png, and other image extensions all roll up to IMG."""
        (temp_folder / "a.jpg").touch()
        (temp_folder / "b.jpeg").touch()
        (temp_folder / "c.png").touch()
        (temp_folder / "d.gif").touch()
        (temp_folder / "e.tiff").touch()
        (temp_folder / "f.webp").touch()
        (temp_folder / "g.orf").touch()  # RAW - not rolled up

        counts = get_file_counts_by_extension(temp_folder)

        assert counts["IMG"] == 6  # All image extensions grouped as IMG
        assert counts["ORF"] == 1  # RAW extension kept as-is
        assert "JPG" not in counts  # JPG should not appear separately
        assert "JPEG" not in counts
        assert "PNG" not in counts

    def test_excludes_faststack_json(self, temp_folder):
        """Test that faststack.json is excluded from counts."""
        (temp_folder / "a.jpg").touch()
        (temp_folder / "faststack.json").touch()

        counts = get_file_counts_by_extension(temp_folder)

        assert "JSON" not in counts
        assert counts.get("IMG") == 1

    def test_handles_no_extension(self, temp_folder):
        """Test files without extension are not counted."""
        (temp_folder / "README").touch()
        (temp_folder / "a.jpg").touch()

        counts = get_file_counts_by_extension(temp_folder)

        # Files without extension should not be in counts
        assert "" not in counts
        assert counts.get("IMG") == 1
