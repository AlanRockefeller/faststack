"""Regression coverage for FS-P11-001's Qt completion handoff."""

import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication

from faststack.app import AppController, RawDevelopmentOperation
from faststack.config import config
from faststack.models import ImageFile


class FakePopen:
    def __init__(self, cmd, runner):
        self.cmd = cmd
        self.runner = runner
        self.returncode = None

    def communicate(self, timeout=None):
        result = self.runner(self.cmd)
        self.returncode = result.returncode
        return result.stdout, result.stderr

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def _popen_from(runner):
    return lambda cmd, **_kwargs: FakePopen(cmd, runner)


@pytest.fixture(scope="module")
def qt_app():
    # QApplication is a QCoreApplication and remains compatible with later
    # widget tests in the same pytest process.
    return QCoreApplication.instance() or QApplication([])


def _controller(image: ImageFile):
    controller = AppController.__new__(AppController)
    QObject.__init__(controller)
    controller.image_files = [image]
    controller.current_index = 0
    controller._raw_developing_keys = set()
    controller._raw_develop_lock = threading.Lock()
    controller._raw_development_operations = {}
    controller._shutting_down = False
    controller.status_messages = []
    controller.update_status_message = controller.status_messages.append
    controller.load_image_for_editing = MagicMock()
    controller._rawDevelopmentFinished.connect(
        controller._on_develop_finished,
        Qt.ConnectionType.QueuedConnection,
    )
    return controller


def _wait_for_completion(controller, timeout_ms=3000):
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(5)

    def finished():
        with controller._raw_develop_lock:
            if not controller._raw_developing_keys:
                loop.quit()

    timer.timeout.connect(finished)
    timer.start()
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    timer.stop()
    with controller._raw_develop_lock:
        assert not controller._raw_developing_keys


def _config_value(executable):
    def get(section, option):
        if (section, option) == ("rawtherapee", "exe"):
            return str(executable)
        if (section, option) == ("rawtherapee", "args"):
            return None
        return None

    return get


def _write_worker_output(cmd, contents=b"tiff"):
    Path(cmd[cmd.index("-o") + 1]).write_bytes(contents)


def test_success_is_queued_to_controller_thread_and_loads_current(qt_app, tmp_path):
    jpg = tmp_path / "image.jpg"
    raw = tmp_path / "image.CR2"
    jpg.write_bytes(b"jpg")
    raw.write_bytes(b"raw")
    controller = _controller(ImageFile(jpg, raw))
    handler_threads = []
    emissions = []
    main_thread = threading.get_ident()
    controller.rawDevelopmentStateChanged.connect(
        lambda: handler_threads.append(threading.get_ident())
    )
    controller._rawDevelopmentFinished.connect(lambda result: emissions.append(result))

    def run(cmd, **kwargs):
        _write_worker_output(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with (
        patch.object(config, "get", side_effect=_config_value(tmp_path / "rt")),
        patch("faststack.app.os.path.exists", return_value=True),
        patch("faststack.app.subprocess.Popen", side_effect=_popen_from(run)),
    ):
        assert controller._develop_raw_backend() is True
        _wait_for_completion(controller)

    assert controller.status_messages[-1] == "RAW Development complete."
    controller.load_image_for_editing.assert_called_once()
    assert len(emissions) == 1
    assert emissions[0].success is True
    assert handler_threads[-1] == main_thread


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        ("nonzero", "exit code 2"),
        ("timeout", "timed out"),
        ("missing", "missing or empty"),
        ("unexpected", "Unexpected error"),
    ],
)
def test_failure_paths_clear_state_and_emit_once(qt_app, tmp_path, outcome, message):
    jpg = tmp_path / f"{outcome}.jpg"
    raw = tmp_path / f"{outcome}.CR2"
    jpg.write_bytes(b"jpg")
    raw.write_bytes(b"raw")
    controller = _controller(ImageFile(jpg, raw))
    emissions = []
    controller._rawDevelopmentFinished.connect(lambda result: emissions.append(result))

    def run(cmd, **kwargs):
        if outcome == "nonzero":
            return subprocess.CompletedProcess(cmd, 2, "stdout", "stderr")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd, 60)
        if outcome == "unexpected":
            raise RuntimeError("boom")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with (
        patch.object(config, "get", side_effect=_config_value(tmp_path / "rt")),
        patch("faststack.app.os.path.exists", return_value=True),
        patch("faststack.app.subprocess.Popen", side_effect=_popen_from(run)),
    ):
        assert controller._develop_raw_backend() is True
        _wait_for_completion(controller)

    assert message in controller.status_messages[-1]
    controller.load_image_for_editing.assert_not_called()
    assert len(emissions) == 1
    assert emissions[0].success is False


def test_replacement_failure_cleans_temp_and_clears_state(qt_app, tmp_path):
    jpg = tmp_path / "replace.jpg"
    raw = tmp_path / "replace.CR2"
    jpg.write_bytes(b"jpg")
    raw.write_bytes(b"raw")
    controller = _controller(ImageFile(jpg, raw))

    def run(cmd, **kwargs):
        _write_worker_output(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with (
        patch.object(config, "get", side_effect=_config_value(tmp_path / "rt")),
        patch("faststack.app.os.path.exists", return_value=True),
        patch("faststack.app.subprocess.Popen", side_effect=_popen_from(run)),
        patch("faststack.app._safe_replace", side_effect=OSError("locked")),
    ):
        controller._develop_raw_backend()
        _wait_for_completion(controller)

    assert "could not replace working TIFF" in controller.status_messages[-1]
    assert list(tmp_path.glob(".replace-working_*.tif")) == []


def test_navigation_makes_success_stale(qt_app, tmp_path):
    first = ImageFile(tmp_path / "first.jpg", tmp_path / "first.CR2")
    second = ImageFile(tmp_path / "second.jpg", tmp_path / "second.CR2")
    for path in (first.path, first.raw_pair, second.path, second.raw_pair):
        path.write_bytes(b"data")
    controller = _controller(first)
    release = threading.Event()

    def run(cmd, **kwargs):
        assert release.wait(2)
        _write_worker_output(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with (
        patch.object(config, "get", side_effect=_config_value(tmp_path / "rt")),
        patch("faststack.app.os.path.exists", return_value=True),
        patch("faststack.app.subprocess.Popen", side_effect=_popen_from(run)),
    ):
        controller._develop_raw_backend()
        controller.image_files = [first, second]
        controller.current_index = 1
        release.set()
        _wait_for_completion(controller)

    controller.load_image_for_editing.assert_not_called()


def test_duplicate_guard_releases_after_real_worker_completion(qt_app, tmp_path):
    image = ImageFile(tmp_path / "duplicate.jpg", tmp_path / "duplicate.CR2")
    image.path.write_bytes(b"jpg")
    image.raw_pair.write_bytes(b"raw")
    controller = _controller(image)
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def run(cmd, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(2)
        _write_worker_output(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with (
        patch.object(config, "get", side_effect=_config_value(tmp_path / "rt")),
        patch("faststack.app.os.path.exists", return_value=True),
        patch("faststack.app.subprocess.Popen", side_effect=_popen_from(run)),
    ):
        assert controller._develop_raw_backend() is True
        assert entered.wait(2)
        assert controller._develop_raw_backend() is False
        release.set()
        _wait_for_completion(controller)
        assert controller._develop_raw_backend() is True
        _wait_for_completion(controller)

    assert calls == 2


def test_late_completion_during_shutdown_is_harmless(qt_app, tmp_path):
    image = ImageFile(tmp_path / "shutdown.jpg", tmp_path / "shutdown.CR2")
    image.path.write_bytes(b"jpg")
    image.raw_pair.write_bytes(b"raw")
    controller = _controller(image)
    release = threading.Event()

    def run(cmd, **kwargs):
        assert release.wait(2)
        _write_worker_output(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with (
        patch.object(config, "get", side_effect=_config_value(tmp_path / "rt")),
        patch("faststack.app.os.path.exists", return_value=True),
        patch("faststack.app.subprocess.Popen", side_effect=_popen_from(run)),
    ):
        controller._develop_raw_backend()
        controller._shutting_down = True
        release.set()
        deadline = time.monotonic() + 3
        while controller._raw_developing_keys and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.005)

    assert not controller._raw_developing_keys
    controller.load_image_for_editing.assert_not_called()
    assert controller.status_messages == ["Developing RAW... please wait."]


def test_failure_before_process_creation_cleans_state(qt_app, tmp_path):
    image = ImageFile(tmp_path / "spawn.jpg", tmp_path / "spawn.CR2")
    image.path.write_bytes(b"jpg")
    image.raw_pair.write_bytes(b"raw")
    controller = _controller(image)
    with (
        patch.object(config, "get", side_effect=_config_value(tmp_path / "rt")),
        patch("faststack.app.os.path.exists", return_value=True),
        patch("faststack.app.subprocess.Popen", side_effect=OSError("no process")),
    ):
        assert controller._develop_raw_backend() is True
        _wait_for_completion(controller)
    assert "Unexpected error" in controller.status_messages[-1]
    assert list(tmp_path.glob(".spawn-working_*.tif")) == []


def test_shutdown_cancels_owned_process_and_removes_partial_output(qt_app, tmp_path):
    image = ImageFile(tmp_path / "cancel.jpg", tmp_path / "cancel.CR2")
    image.path.write_bytes(b"jpg")
    image.raw_pair.write_bytes(b"raw")
    controller = _controller(image)
    entered = threading.Event()
    release = threading.Event()
    processes = []

    def run(cmd):
        _write_worker_output(cmd, b"partial")
        entered.set()
        assert release.wait(2)
        return subprocess.CompletedProcess(cmd, -15, "", "")

    def popen(cmd, **_kwargs):
        process = FakePopen(cmd, run)
        original_terminate = process.terminate

        def terminate():
            original_terminate()
            release.set()

        process.terminate = terminate
        processes.append(process)
        return process

    with (
        patch.object(config, "get", side_effect=_config_value(tmp_path / "rt")),
        patch("faststack.app.os.path.exists", return_value=True),
        patch("faststack.app.subprocess.Popen", side_effect=popen),
    ):
        controller._develop_raw_backend()
        assert entered.wait(2)
        controller._shutting_down = True
        controller._cancel_raw_development_operations()
        deadline = time.monotonic() + 2
        while controller._raw_developing_keys and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.001)

    assert processes[0].returncode == -15
    assert not image.working_tif_path.exists()
    assert list(tmp_path.glob(".cancel-working_*.tif")) == []
    controller.load_image_for_editing.assert_not_called()
    assert controller.status_messages == ["Developing RAW... please wait."]


def _operation(tmp_path, *, process=None, worker=None):
    return RawDevelopmentOperation(
        operation_id="operation",
        develop_key="develop-key",
        source_path=tmp_path / "image.jpg",
        raw_path=tmp_path / "image.CR2",
        tif_path=tmp_path / "image-working.tif",
        tmp_tif_path=tmp_path / ".image-working_tmp.tif",
        process=process,
        worker=worker,
    )


def test_shutdown_terminates_child_and_joins_worker_without_lock(tmp_path):
    controller = _controller(ImageFile(tmp_path / "image.jpg", tmp_path / "image.CR2"))
    child_stopped = threading.Event()
    worker_exited = threading.Event()

    class Process:
        def poll(self):
            return None

        def terminate(self):
            child_stopped.set()

        def wait(self, timeout=None):
            return -15

    def finish_after_child_stops():
        child_stopped.wait()
        worker_exited.set()

    worker = threading.Thread(target=finish_after_child_stops)
    operation = _operation(tmp_path, process=Process(), worker=worker)
    controller._raw_development_operations = {operation.develop_key: operation}
    worker.start()

    controller._cancel_raw_development_operations()

    assert child_stopped.is_set()
    assert worker_exited.is_set()
    assert not worker.is_alive()


def test_shutdown_worker_join_is_bounded_and_lock_is_released(tmp_path):
    controller = _controller(ImageFile(tmp_path / "image.jpg", tmp_path / "image.CR2"))

    class StuckWorker:
        def __init__(self):
            self.join_timeout = None
            self.lock_was_available = False

        def join(self, timeout=None):
            self.join_timeout = timeout
            self.lock_was_available = controller._raw_develop_lock.acquire(False)
            if self.lock_was_available:
                controller._raw_develop_lock.release()

        def is_alive(self):
            return True

    worker = StuckWorker()
    operation = _operation(tmp_path, worker=worker)
    controller._raw_development_operations = {operation.develop_key: operation}

    controller._cancel_raw_development_operations()

    assert worker.join_timeout == 0.5
    assert worker.lock_was_available is True


def test_shutdown_never_joins_current_thread(tmp_path):
    controller = _controller(ImageFile(tmp_path / "image.jpg", tmp_path / "image.CR2"))
    worker = MagicMock()
    operation = _operation(tmp_path, worker=worker)
    controller._raw_development_operations = {operation.develop_key: operation}

    with patch("faststack.app.threading.current_thread", return_value=worker):
        controller._cancel_raw_development_operations()

    worker.join.assert_not_called()


def test_termination_failure_escalates_to_kill():
    class StubbornProcess:
        def __init__(self):
            self.terminated = False
            self.killed = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            if not self.killed:
                raise subprocess.TimeoutExpired("rawtherapee", timeout)
            return -9

        def kill(self):
            self.killed = True

    process = StubbornProcess()
    AppController._stop_raw_process(process, grace_seconds=0)
    assert process.terminated is True
    assert process.killed is True
