"""Tests for AppController._get_bulk_metadata_map error isolation.

The grid (ThumbnailModel.refresh_from_controller) treats a non-None metadata
map as authoritative — it does not fall back to per-image sidecar lookups on
miss. So per-image errors during bulk build must be isolated; one bad path
must not silently force every grid flag to false for the whole folder.
"""

from faststack.models import EntryMetadata, ImageFile


def _make_image(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"\xff\xd8")
    return ImageFile(path=p, timestamp=1000.0)


def test_per_image_error_does_not_invalidate_other_entries(app_controller, tmp_path):
    """One sidecar.get_metadata raise must not drop other images from the map."""
    a = _make_image(tmp_path, "a.jpg")
    bad = _make_image(tmp_path, "b.jpg")
    c = _make_image(tmp_path, "c.jpg")
    app_controller.image_files = [a, bad, c]

    def fake_get_metadata(path, *, create):
        if path == bad.path:
            raise OSError("simulated sidecar failure for one image")
        return EntryMetadata(favorite=True, uploaded=True)

    app_controller.sidecar.get_metadata.side_effect = fake_get_metadata

    bulk_map = app_controller._get_bulk_metadata_map()

    assert str(a.path) in bulk_map
    assert str(c.path) in bulk_map
    assert str(bad.path) not in bulk_map
    assert bulk_map[str(a.path)]["favorite"] is True
    assert bulk_map[str(a.path)]["uploaded"] is True
    assert bulk_map[str(c.path)]["favorite"] is True


def test_all_images_raise_yields_empty_map(app_controller, tmp_path):
    """Documents the contract: when every lookup fails, the map is empty.

    The grid treats this empty map as authoritative — the resulting all-false
    flags are intentional, not a silent regression. Any future change to the
    error-path contract should update this test.
    """
    a = _make_image(tmp_path, "a.jpg")
    b = _make_image(tmp_path, "b.jpg")
    app_controller.image_files = [a, b]

    app_controller.sidecar.get_metadata.side_effect = OSError("everything is broken")

    bulk_map = app_controller._get_bulk_metadata_map()

    assert bulk_map == {}


def test_first_image_raises_subsequent_succeed(app_controller, tmp_path):
    """Earlier exceptions must not skip later images (regression for outer try)."""
    bad = _make_image(tmp_path, "a.jpg")
    good = _make_image(tmp_path, "b.jpg")
    app_controller.image_files = [bad, good]

    def fake_get_metadata(path, *, create):
        if path == bad.path:
            raise RuntimeError("first one fails")
        return EntryMetadata(stacked=True)

    app_controller.sidecar.get_metadata.side_effect = fake_get_metadata

    bulk_map = app_controller._get_bulk_metadata_map()

    assert str(bad.path) not in bulk_map
    assert str(good.path) in bulk_map
    assert bulk_map[str(good.path)]["stacked"] is True
