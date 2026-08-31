import configparser
from unittest.mock import patch

from faststack.config import AppConfig


def _make_config(tmp_path) -> AppConfig:
    with (
        patch("faststack.config.get_app_data_dir", return_value=tmp_path),
        patch.object(AppConfig, "_detect_external_tool_paths", return_value=False),
    ):
        return AppConfig()


def _read(path):
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    return parser


def test_two_stale_instances_merge_unrelated_settings(tmp_path):
    first = _make_config(tmp_path)
    second = _make_config(tmp_path)

    first.set("core", "theme", "2")
    second.set("updates", "last_ignored_version", "1.9.0")
    assert first.save() is True
    assert second.save() is True

    persisted = _read(tmp_path / "faststack.ini")
    assert persisted.get("core", "theme") == "2"
    assert persisted.get("updates", "last_ignored_version") == "1.9.0"


def test_atomic_replace_failure_keeps_old_file_and_dirty_change(tmp_path):
    config = _make_config(tmp_path)
    config_path = tmp_path / "faststack.ini"
    before = config_path.read_bytes()
    config.set("core", "theme", "2")

    with patch("faststack.config.os.replace", side_effect=OSError("disk failure")):
        assert config.save() is False

    assert config_path.read_bytes() == before
    assert config.get("core", "theme") == "2"
    assert config.save() is True
    assert _read(config_path).get("core", "theme") == "2"


def test_malformed_config_is_preserved_before_default_recovery(tmp_path):
    config_path = tmp_path / "faststack.ini"
    malformed = b"theme = 2\n[interrupted"
    config_path.write_bytes(malformed)

    recovered = _make_config(tmp_path)

    backups = list(tmp_path.glob("faststack.ini.unreadable-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == malformed
    assert recovered.get("core", "theme") is not None
    assert _read(config_path).has_section("core")

