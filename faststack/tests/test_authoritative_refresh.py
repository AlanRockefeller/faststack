from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from faststack.models import ImageFile


def _image(directory: Path, name: str) -> ImageFile:
    return ImageFile(path=directory / name)


def _prepare_index_apply(controller, old_images, current_index):
    controller._all_images = list(old_images)
    controller.image_files = list(old_images)
    controller.current_index = current_index
    controller._variant_map = {}
    controller._filter_enabled = False
    controller.sidecar.data = MagicMock(stack_paths=[])
    controller._watcher_changed_paths = {None}
    controller.view_override_path = None
    controller.view_override_kind = None

    def apply_filter(*, notify=True):
        controller.image_files = list(controller._all_images)
        controller._rebuild_path_to_index()

    controller._apply_filter_to_cached_list = apply_filter
    controller._begin_direct_image_transition = MagicMock()
    controller._consume_photoshop_overwrites = MagicMock(return_value=set())
    controller._persist_photoshop_overwrite_batches = MagicMock(return_value=False)
    controller._refresh_thumbnail_model_from_controller = MagicMock()
    controller._bump_display_generation = MagicMock()
    controller._do_prefetch = MagicMock()
    controller._restart_quality_decode_timer = MagicMock()
    controller._notify_thumbnail_current_path_changed = MagicMock()


def test_refresh_preserves_current_path_when_earlier_row_disappears(app_controller):
    a = _image(app_controller.image_dir, "a.jpg")
    b = _image(app_controller.image_dir, "b.jpg")
    c = _image(app_controller.image_dir, "c.jpg")
    _prepare_index_apply(app_controller, [a, b, c], 2)

    app_controller._on_index_scan_ready(
        {"epoch": app_controller._index_scan_epoch, "result": ([b, c], {})}
    )

    assert app_controller.current_index == 1
    assert app_controller.image_files[app_controller.current_index].path == c.path


def test_refresh_selects_same_slot_when_current_row_disappears(app_controller):
    a = _image(app_controller.image_dir, "a.jpg")
    b = _image(app_controller.image_dir, "b.jpg")
    c = _image(app_controller.image_dir, "c.jpg")
    _prepare_index_apply(app_controller, [a, b, c], 1)

    app_controller._on_index_scan_ready(
        {"epoch": app_controller._index_scan_epoch, "result": ([a, c], {})}
    )

    assert app_controller.current_index == 1
    assert app_controller.image_files[app_controller.current_index].path == c.path


def test_manual_refresh_marks_all_same_path_pixels_stale(app_controller):
    replacement = _image(app_controller.image_dir, "same.jpg")
    app_controller._thumbnail_cache = MagicMock()
    app_controller._on_index_scan_ready = MagicMock()

    with patch(
        "faststack.app.find_images_with_variants", return_value=([replacement], {})
    ):
        app_controller.refresh_grid()

    app_controller._thumbnail_cache.clear.assert_called_once()
    payload = app_controller._on_index_scan_ready.call_args.args[0]
    assert payload["result"] == ([replacement], {})
    assert None in app_controller._watcher_changed_paths


@pytest.mark.parametrize("failure", [PermissionError("denied"), OSError("offline")])
def test_manual_refresh_failure_keeps_current_list(app_controller, failure):
    current = _image(app_controller.image_dir, "same.jpg")
    app_controller._all_images = [current]
    app_controller.image_files = [current]
    app_controller._on_index_scan_ready = MagicMock()

    with patch("faststack.app.find_images_with_variants", side_effect=failure):
        app_controller.refresh_grid()

    assert app_controller.image_files == [current]
    app_controller._on_index_scan_ready.assert_not_called()
    app_controller.update_status_message.assert_called_once()
