import os
from pathlib import Path
from unittest.mock import patch


def _delete_action(original: Path, recycled: Path):
    return ((original, recycled), (None, None))


def test_restore_all_chooses_newest_history_independent_of_enumeration(
    app_controller, tmp_path
):
    recycle_bin = app_controller.image_dir / "image recycle bin"
    recycle_bin.mkdir()
    old = recycle_bin / "photo._fs_11111111.jpg"
    new = recycle_bin / "photo._fs_22222222.jpg"
    old.write_bytes(b"old pixels")
    new.write_bytes(b"new pixels")
    destination = app_controller.image_dir / "photo.jpg"
    app_controller.active_recycle_bins = {recycle_bin}
    app_controller.undo_history = [
        ("delete", _delete_action(destination, old), 10.0),
        ("delete", _delete_action(destination, new), 20.0),
    ]
    app_controller.delete_history = []

    original_iterdir = Path.iterdir

    def reversed_bin_listing(path):
        entries = list(original_iterdir(path))
        return iter(reversed(entries)) if path == recycle_bin else iter(entries)

    with patch.object(Path, "iterdir", reversed_bin_listing):
        result = app_controller.restore_single_bin(str(recycle_bin))

    assert destination.read_bytes() == b"new pixels"
    assert old.read_bytes() == b"old pixels"
    assert result["restored_count"] == 1
    assert result["superseded_count"] == 1


def test_restore_destination_identity_follows_platform_path_normalization(
    app_controller, tmp_path
):
    lower = app_controller._restore_destination_identity(tmp_path, "photo.jpg")
    upper = app_controller._restore_destination_identity(tmp_path, "PHOTO.jpg")

    if os.path.normcase("photo.jpg") == os.path.normcase("PHOTO.jpg"):
        assert lower == upper
    else:
        assert lower != upper
