"""Regression tests for FS-P1-005: a watcher failure must not abort folder load.

``AppController.load()`` runs *after* the window is on screen, and it called
``watcher.start()`` before publishing folder-loaded state, before
``sync_ui_state()``, and before the initial prefetch. Any watchdog backend
failure (inotify instance limit, permission denied, an unsupported network or
FUSE mount) therefore propagated out of ``load()`` and left a visible but
half-initialised application.

Filesystem watching is now best-effort: the folder still loads, it is simply
not live-monitored.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from faststack.io.watcher import Watcher


@pytest.fixture
def watch_dir(tmp_path):
    directory = tmp_path / "images"
    directory.mkdir()
    return directory


# --------------------------------------------------------------------------
# Watcher owns its own partially-started Observer
# --------------------------------------------------------------------------


@pytest.mark.parametrize("failing_call", ["schedule", "start"])
def test_start_failure_reports_false_and_leaves_no_live_observer(
    watch_dir, failing_call
):
    watcher = Watcher(watch_dir, MagicMock())

    observer = MagicMock()
    getattr(observer, failing_call).side_effect = OSError("inotify limit reached")
    observer.is_alive.return_value = True

    with patch("faststack.io.watcher.Observer", return_value=observer):
        assert watcher.start() is False

    assert watcher.observer is None, "self.observer must not name a broken observer"
    assert watcher.is_alive() is False

    # The half-built observer was torn down rather than leaked.
    observer.unschedule_all.assert_called_once()
    observer.stop.assert_called_once()
    observer.join.assert_called_once()


def test_observer_construction_failure_is_handled(watch_dir):
    watcher = Watcher(watch_dir, MagicMock())
    with patch(
        "faststack.io.watcher.Observer", side_effect=OSError("no backend available")
    ):
        assert watcher.start() is False
    assert watcher.observer is None


def test_cleanup_errors_during_teardown_do_not_escape(watch_dir):
    watcher = Watcher(watch_dir, MagicMock())
    observer = MagicMock()
    observer.schedule.side_effect = OSError("boom")
    observer.unschedule_all.side_effect = RuntimeError("also broken")
    observer.stop.side_effect = RuntimeError("still broken")

    with patch("faststack.io.watcher.Observer", return_value=observer):
        assert watcher.start() is False
    assert watcher.observer is None


def test_stop_is_safe_after_a_failed_start(watch_dir):
    watcher = Watcher(watch_dir, MagicMock())
    with patch("faststack.io.watcher.Observer", side_effect=OSError("nope")):
        watcher.start()
    watcher.stop()  # must not raise
    assert watcher.observer is None


def test_missing_directory_still_returns_false(tmp_path):
    watcher = Watcher(tmp_path / "gone", MagicMock())
    assert watcher.start() is False


def test_successful_start_is_unchanged(watch_dir):
    """The happy path must behave exactly as before, and report True."""
    watcher = Watcher(watch_dir, MagicMock())
    observer = MagicMock()
    observer.is_alive.return_value = True

    with patch("faststack.io.watcher.Observer", return_value=observer):
        assert watcher.start() is True

    assert watcher.observer is observer
    assert watcher.is_alive() is True
    observer.schedule.assert_called_once_with(
        watcher.event_handler, str(watch_dir), recursive=False
    )
    observer.start.assert_called_once()
    observer.unschedule_all.assert_not_called()

    # Already running: no second observer, still True.
    with patch("faststack.io.watcher.Observer") as second:
        assert watcher.start() is True
        second.assert_not_called()

    watcher.stop()
    assert watcher.observer is None


def test_real_observer_start_failure_against_a_real_watcher(watch_dir):
    """End-to-end with the actual watchdog Observer class patched to fail."""
    watcher = Watcher(watch_dir, MagicMock())
    with patch(
        "watchdog.observers.api.BaseObserver.start",
        side_effect=OSError("Failed to start observer"),
    ):
        assert watcher.start() is False
    assert watcher.observer is None
    watcher.stop()


# --------------------------------------------------------------------------
# load() completes and publishes folder state
# --------------------------------------------------------------------------


def test_load_completes_when_the_watcher_cannot_start(app_controller, tmp_path):
    controller = app_controller

    image_dir = tmp_path / "images"
    image_dir.mkdir(exist_ok=True)
    controller.image_dir = image_dir

    # A real Watcher whose backend refuses to start.
    controller.watcher = Watcher(image_dir, MagicMock())

    controller.image_files = []
    controller._startup_restore_path = None
    controller._startup_restore_index = None
    controller._session_registry = MagicMock()
    controller._remember_last_directory = MagicMock()
    controller._do_prefetch = MagicMock()
    controller._set_folder_loaded = MagicMock(return_value=True)
    controller._commit_pending_display_size = MagicMock(return_value=(False, False))
    controller._release_startup_prefetch_constraint = MagicMock()
    controller._maybe_decode_current_image = MagicMock()
    controller._restart_quality_decode_timer = MagicMock()
    controller._maybe_show_turbo_fallback_warning = MagicMock()
    controller.resize_timer = MagicMock()
    controller.resize_timer.isActive.return_value = False
    controller._is_grid_view_active = False

    with patch("faststack.io.watcher.Observer", side_effect=OSError("no inotify")):
        controller.load()  # must not raise

    # Folder loading finished: state published, UI synced, prefetch submitted.
    controller._set_folder_loaded.assert_any_call(True, notify=False)
    controller.sync_ui_state.assert_called_once_with(image_count_changed=True)
    controller._do_prefetch.assert_called_once_with(controller.current_index)
    controller._remember_last_directory.assert_called_once()
    controller._session_registry.update.assert_called_once()

    # And no broken observer was left behind.
    assert controller.watcher.observer is None
    assert controller.watcher.is_alive() is False
    controller.watcher.stop()


def test_load_still_starts_a_healthy_watcher(app_controller, tmp_path):
    controller = app_controller

    image_dir = tmp_path / "images"
    image_dir.mkdir(exist_ok=True)
    controller.image_dir = image_dir
    controller.watcher = Watcher(image_dir, MagicMock())

    controller.image_files = []
    controller._startup_restore_path = None
    controller._startup_restore_index = None
    controller._session_registry = MagicMock()
    controller._remember_last_directory = MagicMock()
    controller._do_prefetch = MagicMock()
    controller._set_folder_loaded = MagicMock(return_value=True)
    controller._commit_pending_display_size = MagicMock(return_value=(False, False))
    controller._release_startup_prefetch_constraint = MagicMock()
    controller._maybe_decode_current_image = MagicMock()
    controller._restart_quality_decode_timer = MagicMock()
    controller._maybe_show_turbo_fallback_warning = MagicMock()
    controller.resize_timer = MagicMock()
    controller.resize_timer.isActive.return_value = False
    controller._is_grid_view_active = False

    observer = MagicMock()
    observer.is_alive.return_value = True
    with patch("faststack.io.watcher.Observer", return_value=observer):
        controller.load()

    assert controller.watcher.observer is observer
    observer.start.assert_called_once()
    controller.sync_ui_state.assert_called_once_with(image_count_changed=True)


def test_shutdown_is_safe_after_a_failed_watcher_start(app_controller, tmp_path):
    controller = app_controller
    image_dir = tmp_path / "images"
    image_dir.mkdir(exist_ok=True)
    controller.watcher = Watcher(image_dir, MagicMock())

    with patch("faststack.io.watcher.Observer", side_effect=OSError("nope")):
        assert controller.watcher.start() is False

    controller.watcher.stop()  # the shutdown_qt path
    assert controller.watcher.observer is None
