import pytest
import os
from unittest.mock import MagicMock, call, patch
from pathlib import Path

from faststack.app import AppController
from faststack.models import ImageFile

from PySide6.QtWidgets import QApplication
import sys

# Ensure QApplication exists before AppController is imported/used in tests
if not QApplication.instance():
    _qapp = QApplication(sys.argv)


@pytest.fixture
def mock_app():
    """Create a partial mock of AppController for deletion testing."""
    with (
        patch("faststack.app.ByteLRUCache"),
        patch("faststack.app.ThumbnailModel"),
        patch("faststack.app.Prefetcher"),
        patch("faststack.app.PathResolver"),
        patch("faststack.app.Watcher"),
        patch("faststack.app.uuid"),
        patch("faststack.app.QTimer"),
        patch("faststack.app.concurrent.futures.ThreadPoolExecutor"),
    ):

        # Pass mock engine
        mock_engine = MagicMock()
        app = AppController(Path("."), mock_engine)
        app.image_cache = MagicMock()
        # Ensure evict_paths is a method on the mock
        app.image_cache.evict_paths = MagicMock()
        app.prefetcher = MagicMock()
        app._thumbnail_model = MagicMock()
        app._path_resolver = MagicMock()
        app._path_to_index = {}
        app.sidecar = MagicMock()
        app._delete_executor = MagicMock()

        # Mock PathResolver update to verify no resolve calls
        return app


def test_delete_uses_targeted_eviction(mock_app):
    """Verify delete_indices calls evict_paths and NOT clear."""
    app = mock_app

    # Setup data
    img1 = ImageFile(Path("c:/images/img1.jpg"), raw_pair=Path("c:/images/img1.CR2"))
    img2 = ImageFile(Path("c:/images/img2.jpg"))
    app.image_files = [img1, img2]

    # Manually populate path index since we bypassed load()
    app._path_to_index = {app._key(img1.path): 0, app._key(img2.path): 1}
    app.current_index = 0
    app.display_generation = 10

    # Act
    app._delete_indices([0], "test")

    # Assert
    # 1. Should not clear entire cache
    app.image_cache.clear.assert_not_called()

    # 2. Should not bump display generation (targeted update handled elsewhere)
    # Note: optimistic deletion might bump generation if it triggers refresh,
    # but here we check it doesn't do a full destructive clear that resets everything.
    # Actually _delete_indices does NOT bump display_generation itself,
    # that happens in undo or refresh.
    assert app.display_generation == 10

    # 3. Should call evict_paths with correct paths
    app.image_cache.evict_paths.assert_called_once()
    args, _ = app.image_cache.evict_paths.call_args
    evicted = args[0]
    assert len(evicted) == 2
    assert img1.path in evicted
    assert img1.raw_pair in evicted

    # 4. Should cancel prefetch
    app.prefetcher.cancel_all.assert_called_once()


def test_evict_paths_windows_handling():
    """Verify ByteLRUCache.evict_paths handles Windows paths correctly."""
    from faststack.imaging.cache import ByteLRUCache

    # Create a real cache instance (mocking LRUCache methods if needed, but ByteLRUCache is simple)
    # Pass a simple size_of function to avoid dependency on get_decoded_image_size
    cache = ByteLRUCache(1000, size_of=lambda _: 1)

    # Add entries with forward slashes (as build_cache_key does)
    key1 = "C:/images/img1.jpg::0"
    key2 = "C:/images/img1.jpg::1"  # Different generation
    key3 = "C:/images/img2.jpg::0"  # Keep this

    cache[key1] = 1
    cache[key2] = 1
    cache[key3] = 1

    # Act: Evict using Windows-style path string
    win_path = "C:\\images\\img1.jpg"
    cache.evict_paths([win_path])

    # Assert
    assert key1 not in cache
    assert key2 not in cache
    assert key3 in cache

    # Act: Evict using Path object
    path_obj = Path("C:/images/img2.jpg")
    cache.evict_paths([path_obj])

    # Assert
    assert key3 not in cache


def test_model_hashing_no_resolve():
    """Verify PathResolver and ThumbnailModel do NOT call resolve()."""
    from faststack.thumbnail_view.model import ThumbnailModel
    from faststack.thumbnail_view.provider import PathResolver
    from faststack.models import ImageFile as ModelImageFile

    # Mock Path.resolve to raise exception
    with patch(
        "pathlib.Path.resolve", side_effect=Exception("Should not call resolve!")
    ):
        # Note: we need to patch wherever usage might occur or globally.
        # Since we changed code to NOT use it, calling the methods should be safe.

        # Test Helper directly
        from faststack.io.utils import compute_path_hash

        p = Path("c:/foo/bar.jpg")
        # This should NOT fail
        h = compute_path_hash(p)
        assert len(h) == 16

        # Test Resolver update
        resolver = PathResolver()
        model = MagicMock()
        model.rowCount.return_value = 1
        entry = MagicMock()
        entry.path = p
        entry.is_folder = False
        model.get_entry.return_value = entry

        resolver.update_from_model(model)
        # Should succeed and have entry
        assert len(resolver._hash_to_path) == 1
