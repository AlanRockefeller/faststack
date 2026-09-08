import logging
import threading
import time
from pathlib import Path
from unittest.mock import patch

from faststack.app import AppController
from faststack.io.helicon import HeliconLaunch, launch_helicon_focus


class ControlledProcess:
    def __init__(self, *, exited=False):
        self.exited = threading.Event()
        if exited:
            self.exited.set()

    def wait(self, timeout=None):
        if not self.exited.wait(timeout or 2):
            raise TimeoutError
        return 0


def controller():
    app = AppController.__new__(AppController)
    app._helicon_launches = {}
    app._helicon_launches_lock = threading.Lock()
    return app


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.001)
    assert predicate()


def test_manifest_removed_when_helicon_exits(tmp_path):
    app = controller()
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("files")
    process = ControlledProcess()
    app._track_helicon_launch(HeliconLaunch(process, manifest))
    process.exited.set()
    wait_until(lambda: not manifest.exists())
    assert app._helicon_launches == {}


def test_shutdown_does_not_delete_running_child_manifest(tmp_path):
    app = controller()
    manifest = tmp_path / "running.txt"
    manifest.write_text("files")
    process = ControlledProcess()
    app._track_helicon_launch(HeliconLaunch(process, manifest))
    app._shutting_down = True
    assert manifest.exists()
    assert len(app._helicon_launches) == 1
    process.exited.set()
    wait_until(lambda: not manifest.exists())


def test_multiple_manifests_are_owned_independently(tmp_path):
    app = controller()
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first")
    second.write_text("second")
    p1 = ControlledProcess()
    p2 = ControlledProcess()
    app._track_helicon_launch(HeliconLaunch(p1, first))
    app._track_helicon_launch(HeliconLaunch(p2, second))
    p1.exited.set()
    wait_until(lambda: not first.exists())
    assert second.exists()
    assert len(app._helicon_launches) == 1
    p2.exited.set()
    wait_until(lambda: not second.exists())


def test_popen_failure_removes_manifest_immediately(tmp_path):
    raw = tmp_path / "input.raw"
    raw.write_bytes(b"raw")
    created = tmp_path / "manifest.txt"

    class TempFile:
        def __enter__(self):
            self.name = str(created)
            self.handle = created.open("w", encoding="utf-8")
            return self.handle

        def __exit__(self, *_args):
            self.handle.close()

    with (
        patch("faststack.io.helicon.validate_executable_path", return_value=(True, "")),
        patch(
            "faststack.io.helicon.tempfile.NamedTemporaryFile", return_value=TempFile()
        ),
        patch("faststack.io.helicon.subprocess.Popen", side_effect=OSError("boom")),
        patch("faststack.io.helicon.config.get", side_effect=["helicon", ""]),
    ):
        success, launch = launch_helicon_focus([raw])
    assert success is False
    assert launch is None
    assert not created.exists()


def test_child_that_exits_quickly_is_cleaned(tmp_path):
    app = controller()
    manifest = tmp_path / "quick.txt"
    manifest.write_text("files")
    app._track_helicon_launch(HeliconLaunch(ControlledProcess(exited=True), manifest))
    wait_until(lambda: not manifest.exists())


def test_cleanup_failure_is_logged_and_does_not_crash(tmp_path, caplog):
    app = controller()
    manifest = tmp_path / "artifact.txt"
    manifest.write_text("files")
    process = ControlledProcess()
    real_unlink = Path.unlink

    def fail_target(path, *args, **kwargs):
        if path == manifest:
            raise OSError("locked")
        return real_unlink(path, *args, **kwargs)

    with patch.object(Path, "unlink", fail_target):
        with caplog.at_level(logging.WARNING):
            app._track_helicon_launch(HeliconLaunch(process, manifest))
            process.exited.set()
            wait_until(lambda: app._helicon_launches == {})
    assert manifest.exists()
    assert "artifact remains" in caplog.text
