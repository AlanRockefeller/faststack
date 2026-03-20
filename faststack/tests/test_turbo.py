import importlib
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


def test_create_turbojpeg_prefers_explicit_env_path(monkeypatch):
    turbo = importlib.import_module("faststack.imaging.turbo")

    calls = []

    def fake_decoder(path=None):
        calls.append(path)
        if path == "C:/turbo/bin/turbojpeg.dll":
            return SimpleNamespace(source=path)
        raise RuntimeError(f"boom:{path}")

    monkeypatch.setattr(turbo, "TurboJPEG", fake_decoder)
    monkeypatch.setenv("FASTSTACK_TURBOJPEG_LIB", "C:/turbo/bin/turbojpeg.dll")

    decoder, available = turbo.create_turbojpeg()

    assert available is True
    assert decoder.source == "C:/turbo/bin/turbojpeg.dll"
    assert calls == ["C:/turbo/bin/turbojpeg.dll"]


def test_create_turbojpeg_retries_default_loader_after_bad_env_override(monkeypatch):
    turbo = importlib.import_module("faststack.imaging.turbo")

    calls = []

    def fake_decoder(path=None):
        calls.append(path)
        if path == "/bad/turbojpeg.so":
            raise RuntimeError("bad override")
        if path is None:
            return SimpleNamespace(source="default")
        raise RuntimeError(f"unexpected path:{path}")

    monkeypatch.setattr(turbo, "TurboJPEG", fake_decoder)
    monkeypatch.setattr(turbo.os, "name", "posix")
    monkeypatch.setenv("FASTSTACK_TURBOJPEG_LIB", "/bad/turbojpeg.so")
    monkeypatch.delenv("TURBOJPEG_LIB", raising=False)

    decoder, available = turbo.create_turbojpeg()

    assert available is True
    assert decoder.source == "default"
    assert calls == ["/bad/turbojpeg.so", None]


def test_create_turbojpeg_logs_failed_candidates(monkeypatch, caplog):
    turbo = importlib.import_module("faststack.imaging.turbo")

    def fake_decoder(path=None):
        raise RuntimeError(f"boom:{path}")

    monkeypatch.setattr(turbo, "TurboJPEG", fake_decoder)
    monkeypatch.setattr(
        turbo,
        "_candidate_library_paths",
        lambda: [None, "C:/one/turbojpeg.dll", "C:/two/turbojpeg.dll"],
    )

    with caplog.at_level(logging.WARNING):
        decoder, available = turbo.create_turbojpeg()

    assert decoder is None
    assert available is False
    assert "default loader" in caplog.text
    assert "C:/one/turbojpeg.dll" in caplog.text
    assert "C:/two/turbojpeg.dll" in caplog.text
    assert "Falling back to Pillow" in caplog.text


def test_get_app_data_dir_falls_back_when_appdata_is_not_creatable(monkeypatch, tmp_path):
    logging_setup = importlib.import_module("faststack.logging_setup")

    home_dir = tmp_path / "home"
    home_dir.mkdir()
    fallback_dir = home_dir / ".faststack"
    blocked_candidate = tmp_path / "blocked" / "faststack"

    monkeypatch.setenv("APPDATA", str(tmp_path / "blocked"))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home_dir)
    monkeypatch.setattr(
        logging_setup,
        "_can_create_directory",
        lambda path: False if path == blocked_candidate else True,
    )

    assert logging_setup.get_app_data_dir() == fallback_dir


def test_get_app_data_dir_falls_back_to_tempdir(monkeypatch, tmp_path):
    logging_setup = importlib.import_module("faststack.logging_setup")

    home_dir = tmp_path / "home"
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    home_candidate = home_dir / ".faststack"

    monkeypatch.delenv("FASTSTACK_APPDATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home_dir)
    monkeypatch.setattr(logging_setup, "gettempdir", lambda: str(temp_dir))
    monkeypatch.setattr(
        logging_setup,
        "_can_create_directory",
        lambda path: (
            False
            if path == home_candidate
            else logging_setup._is_writable_directory(path.parent)
        ),
    )

    assert logging_setup.get_app_data_dir() == temp_dir / "faststack"


def test_is_writable_directory_does_not_create_missing_dir(tmp_path):
    logging_setup = importlib.import_module("faststack.logging_setup")

    missing = tmp_path / "missing"

    assert logging_setup._is_writable_directory(missing) is False
    assert missing.exists() is False


def test_setup_logging_keeps_console_handler_when_file_logging_fails(monkeypatch):
    logging_setup = importlib.import_module("faststack.logging_setup")

    monkeypatch.setattr(logging_setup, "get_app_data_dir", lambda: Path("/bad-path"))
    monkeypatch.setattr(
        logging_setup.logging.handlers,
        "RotatingFileHandler",
        Mock(side_effect=OSError("disk full")),
    )

    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    try:
        logging_setup.setup_logging(debug=True)
        assert any(
            isinstance(handler, logging.StreamHandler) for handler in root_logger.handlers
        )
    finally:
        root_logger.handlers = original_handlers
