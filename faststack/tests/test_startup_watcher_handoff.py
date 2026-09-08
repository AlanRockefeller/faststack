import threading
from unittest.mock import MagicMock, patch

from faststack.app import AppController
from faststack.io.indexer import find_images_with_variants as real_find_images


def test_watcher_is_active_before_authoritative_startup_scan(app_controller):
    scan_entered = threading.Event()
    mutation_done = threading.Event()
    mutation_thread = None

    def mutate_during_handoff():
        assert scan_entered.wait(2), "startup scan never began after watcher start"
        (app_controller.image_dir / "late.jpg").write_bytes(b"late")
        mutation_done.set()

    def start_watcher():
        nonlocal mutation_thread
        mutation_thread = threading.Thread(target=mutate_during_handoff)
        mutation_thread.start()
        return True

    def barrier_scan(directory):
        assert app_controller.watcher.start.called
        scan_entered.set()
        assert mutation_done.wait(2), "mutation did not complete during scan handoff"
        return real_find_images(directory)

    app_controller.watcher.start.side_effect = start_watcher
    app_controller.sidecar.data.last_path = None
    app_controller.sidecar.data.last_index = 0
    app_controller.sidecar.data.stack_paths = []
    app_controller.sidecar.data.stacks = []
    app_controller.refresh_image_list = AppController.refresh_image_list.__get__(
        app_controller
    )
    app_controller._refresh_thumbnail_model_from_controller = MagicMock()

    with patch("faststack.app.find_images_with_variants", side_effect=barrier_scan):
        app_controller.load(skip_thumbnail_refresh=True)

    mutation_thread.join(timeout=2)
    assert [image.path.name for image in app_controller.image_files] == ["late.jpg"]
