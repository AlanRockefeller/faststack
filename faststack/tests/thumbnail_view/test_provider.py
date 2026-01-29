"""Tests for PathResolver (ThumbnailProvider requires Qt GUI)."""

from pathlib import Path
from unittest.mock import MagicMock

from faststack.thumbnail_view.provider import PathResolver


class TestPathResolver:
    """Tests for PathResolver."""

    def test_register_and_resolve(self):
        """Test registering and resolving paths."""
        resolver = PathResolver()
        path = Path("/test/image.jpg")
        path_hash = "abc123"

        resolver.register(path, path_hash)
        resolved = resolver.resolve(path_hash)

        assert resolved == path

    def test_resolve_unknown_hash(self):
        """Test resolving unknown hash returns None."""
        resolver = PathResolver()
        assert resolver.resolve("unknown") is None

    def test_clear(self):
        """Test clearing the resolver."""
        resolver = PathResolver()
        resolver.register(Path("/test/image.jpg"), "abc123")

        resolver.clear()

        assert resolver.resolve("abc123") is None

    def test_multiple_registrations(self):
        """Test registering multiple paths."""
        resolver = PathResolver()

        resolver.register(Path("/test/image1.jpg"), "hash1")
        resolver.register(Path("/test/image2.jpg"), "hash2")
        resolver.register(Path("/test/image3.jpg"), "hash3")

        assert resolver.resolve("hash1") == Path("/test/image1.jpg")
        assert resolver.resolve("hash2") == Path("/test/image2.jpg")
        assert resolver.resolve("hash3") == Path("/test/image3.jpg")

    def test_overwrite_existing_hash(self):
        """Test that registering with same hash overwrites."""
        resolver = PathResolver()

        resolver.register(Path("/test/old.jpg"), "hash1")
        resolver.register(Path("/test/new.jpg"), "hash1")

        assert resolver.resolve("hash1") == Path("/test/new.jpg")

    def test_update_from_model(self):
        """Test updating resolver from a model."""
        resolver = PathResolver()

        # Mock model
        mock_model = MagicMock()
        mock_model.rowCount.return_value = 2

        entry1 = MagicMock()
        entry1.is_folder = False
        entry1.path = Path("/test/image1.jpg")

        entry2 = MagicMock()
        entry2.is_folder = True  # Folders should be skipped
        entry2.path = Path("/test/folder")

        mock_model.get_entry.side_effect = [entry1, entry2]

        resolver.update_from_model(mock_model)

        # Should have registered the non-folder entry
        assert len(resolver._hash_to_path) == 1

    def test_update_from_model_clears_first(self):
        """Test that update_from_model clears existing registrations."""
        resolver = PathResolver()
        resolver.register(Path("/old/path.jpg"), "oldhash")

        # Mock model with empty entries
        mock_model = MagicMock()
        mock_model.rowCount.return_value = 0

        resolver.update_from_model(mock_model)

        # Old registration should be cleared
        assert resolver.resolve("oldhash") is None


# Note: ThumbnailProvider tests are skipped because they require a running
# Qt GUI application (QApplication). The provider creates QPixmap objects
# which require a display connection.
#
# To test ThumbnailProvider functionality, use integration tests with
# pytest-qt and a proper QApplication fixture.
