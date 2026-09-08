from unittest.mock import patch

import pytest

from faststack.app import AppController
from faststack.io.indexer import (
    DirectoryScanError,
    find_images,
    find_images_with_variants,
)
from faststack.io.variants import VariantGroup
from faststack.models import ImageFile


@pytest.mark.parametrize("scan", [find_images, find_images_with_variants])
@pytest.mark.parametrize(
    "failure", [PermissionError("access denied"), OSError("drive unavailable")]
)
def test_directory_scan_failure_is_not_reported_as_empty(tmp_path, scan, failure):
    with patch("faststack.io.indexer.os.scandir", side_effect=failure):
        with pytest.raises(DirectoryScanError) as caught:
            scan(tmp_path)

    assert caught.value.directory == tmp_path
    assert caught.value.cause is failure


def test_refresh_preserves_last_known_good_state_on_scan_failure(app_controller):
    image = ImageFile(path=app_controller.image_dir / "known.jpg")
    variant_map = {"known.jpg": VariantGroup(group_key="known.jpg")}
    app_controller._all_images = [image]
    app_controller.image_files = [image]
    app_controller._variant_map = variant_map
    app_controller.refresh_image_list = AppController.refresh_image_list.__get__(
        app_controller
    )

    with patch(
        "faststack.app.find_images_with_variants",
        side_effect=PermissionError("temporarily unavailable"),
    ):
        assert app_controller.refresh_image_list() is False

    assert app_controller._all_images == [image]
    assert app_controller.image_files == [image]
    assert app_controller._variant_map is variant_map
    app_controller._thumbnail_model.refresh_from_controller.assert_not_called()
    app_controller.update_status_message.assert_called_once()


def test_watcher_scan_failure_preserves_state_and_allows_retry(app_controller):
    image = ImageFile(path=app_controller.image_dir / "known.jpg")
    variant_map = {"known": object()}
    app_controller._all_images = [image]
    app_controller.image_files = [image]
    app_controller._variant_map = variant_map
    app_controller._index_scan_epoch = 7
    app_controller._index_scan_inflight = True
    app_controller._index_rescan_needed = False

    app_controller._on_index_scan_ready(
        {"epoch": 7, "result": None, "error": "drive unavailable"}
    )

    assert app_controller._all_images == [image]
    assert app_controller.image_files == [image]
    assert app_controller._variant_map is variant_map
    assert app_controller._index_scan_inflight is False
    app_controller.update_status_message.assert_called_once()
