from faststack.models import ImageFile

# We can't easily instantiate AppController without complex mocks for QML engine, etc.
# So we test the logic extracted from start_drag_current_image.


def get_drag_paths(
    image_files, current_index, existing_indices, current_edit_source_mode
):
    file_paths = []
    for idx in existing_indices:
        img = image_files[idx]

        # logic from app.py
        is_developed_artifact = img.path.stem.lower().endswith("-developed")
        in_raw_mode = current_edit_source_mode == "raw"

        if (in_raw_mode or is_developed_artifact) and img.developed_jpg_path.exists():
            file_paths.append(img.developed_jpg_path)
        else:
            file_paths.append(img.path)
    return file_paths


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
