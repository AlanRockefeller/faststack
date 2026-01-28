"""Tests for folder_stats module."""

import json
import pytest
from pathlib import Path
from faststack.thumbnail_view.folder_stats import (
    FolderStats,
    read_folder_stats,
    clear_stats_cache,
    _stats_cache,
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
            }
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
            }
        }
        json_path.write_text(json.dumps(data))

        stats = read_folder_stats(temp_folder)

        assert stats is not None
        assert stats.total_images == 2  # Both entries counted
        assert stats.stacked_count == 1  # Only valid entry counted
