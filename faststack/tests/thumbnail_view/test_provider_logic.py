"""Tests for _parse_id logic in ThumbnailProvider."""

import pytest
from faststack.thumbnail_view.provider import ThumbnailProvider

class TestProviderLogic:
    @pytest.fixture
    def provider(self):
        p = ThumbnailProvider.__new__(ThumbnailProvider)
        p._default_size = 200
        return p

    def test_parse_id_file_success(self, provider):
        id_str = "256/pathhash123/123456789?r=1&reason=scroll"
        id_clean, parts, thumb_size, path_hash, mtime_ns, reason, is_folder, is_valid = provider._parse_id(id_str)
        
        assert id_clean == "256/pathhash123/123456789"
        assert parts == ["256", "pathhash123", "123456789"]
        assert thumb_size == 256
        assert path_hash == "pathhash123"
        assert mtime_ns == 123456789
        assert reason == "scroll"
        assert is_folder is False
        assert is_valid is True

    def test_parse_id_folder_success(self, provider):
        id_str = "folder/pathhash456/987654321?r=2"
        id_clean, parts, thumb_size, path_hash, mtime_ns, reason, is_folder, is_valid = provider._parse_id(id_str)
        
        assert id_clean == "folder/pathhash456/987654321"
        assert parts == ["folder", "pathhash456", "987654321"]
        assert thumb_size == 200  # Default size
        assert path_hash == "pathhash456"
        assert mtime_ns == 987654321
        assert reason == "unknown"
        assert is_folder is True
        assert is_valid is True

    def test_parse_id_invalid_format(self, provider):
        id_str = "invalid/id"
        *_, is_valid = provider._parse_id(id_str)
        
        assert is_valid is False

    def test_parse_id_invalid_number(self, provider):
        id_str = "abc/pathhash/123"
        *_, is_valid = provider._parse_id(id_str)
        
        assert is_valid is False
