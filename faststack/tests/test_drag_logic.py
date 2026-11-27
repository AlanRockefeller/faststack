from types import SimpleNamespace

from faststack.app import AppController
from faststack.models import ImageFile

# AppController is heavy to build, so the drag path choice is tested through
# AppController._export_path_for (shared by drag, iNaturalist upload and
# Ctrl+Shift+C) on a stand-in controller.


def get_drag_paths(
    image_files, current_index, existing_indices, current_edit_source_mode
):
    fake = SimpleNamespace(current_edit_source_mode=current_edit_source_mode)
    return [
        AppController._export_path_for(fake, image_files[idx])
        for idx in existing_indices
    ]


def test_drag_logic_jpeg_mode(tmp_path):
    """In JPEG mode, prefer the original JPG even if -developed exists."""
    jpg_path = tmp_path / "A.jpg"
    dev_path = tmp_path / "A-developed.jpg"
    jpg_path.touch()
    dev_path.touch()

    img = ImageFile(path=jpg_path)
    # Note: developed_jpg_path is a property that calculates the path

    paths = get_drag_paths([img], 0, [0], "jpeg")
    assert paths == [jpg_path]


def test_drag_logic_raw_mode(tmp_path):
    """In RAW mode, prefer -developed.jpg if it exists."""
    jpg_path = tmp_path / "A.jpg"
    dev_path = tmp_path / "A-developed.jpg"
    jpg_path.touch()
    dev_path.touch()

    img = ImageFile(path=jpg_path)

    paths = get_drag_paths([img], 0, [0], "raw")
    assert paths == [dev_path]


def test_drag_logic_developed_artifact(tmp_path):
    """If the dragged file IS a developed artifact, it should prefer -developed.jpg (itself)."""
    # This case might be rare if the indexer handles it, but let's test the logic.
    dev_path = tmp_path / "A-developed.jpg"
    dev_path.touch()

    # In this case, developed_jpg_path will be "A-developed-developed.jpg"
    # which won't exist. So it should fallback to itself.
    img = ImageFile(path=dev_path)

    paths = get_drag_paths([img], 0, [0], "jpeg")
    assert paths == [dev_path]


def test_drag_logic_raw_mode_missing_developed(tmp_path):
    """In RAW mode, if -developed.jpg is missing, fallback to main path."""
    jpg_path = tmp_path / "A.jpg"
    jpg_path.touch()

    img = ImageFile(path=jpg_path)

    paths = get_drag_paths([img], 0, [0], "raw")
    assert paths == [jpg_path]
