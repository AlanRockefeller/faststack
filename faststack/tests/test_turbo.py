import importlib
import logging
from types import SimpleNamespace


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


def test_all_candidates_fail_emits_one_warning(monkeypatch, caplog):
    """When all locations fail, exactly one warning is emitted (not one per candidate)."""
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

    warning_records = [
        r for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert len(warning_records) == 1
    assert "Falling back to Pillow" in warning_records[0].message
    assert "3 location(s) tried" in warning_records[0].message


def test_all_candidates_fail_details_at_debug(monkeypatch, caplog):
    """Per-candidate failure details are available at DEBUG level."""
    turbo = importlib.import_module("faststack.imaging.turbo")

    def fake_decoder(path=None):
        raise RuntimeError(f"boom:{path}")

    monkeypatch.setattr(turbo, "TurboJPEG", fake_decoder)
    monkeypatch.setattr(
        turbo,
        "_candidate_library_paths",
        lambda: [None, "C:/one/turbojpeg.dll"],
    )

    with caplog.at_level(logging.DEBUG):
        turbo.create_turbojpeg()

    debug_records = [
        r for r in caplog.records if r.levelno == logging.DEBUG
    ]
    debug_text = " ".join(r.message for r in debug_records)
    assert "default loader" in debug_text
    assert "C:/one/turbojpeg.dll" in debug_text


def test_missing_turbojpeg_package_emits_one_warning(monkeypatch, caplog):
    """When the turbojpeg package is not installed, exactly one warning is emitted."""
    turbo = importlib.import_module("faststack.imaging.turbo")

    monkeypatch.setattr(turbo, "TurboJPEG", None)

    with caplog.at_level(logging.WARNING):
        decoder, available = turbo.create_turbojpeg()

    assert decoder is None
    assert available is False

    warning_records = [
        r for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert len(warning_records) == 1
    assert "PyTurboJPEG not found" in warning_records[0].message
    assert "Pillow" in warning_records[0].message
