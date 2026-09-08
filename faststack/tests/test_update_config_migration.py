"""Migration of the legacy [updates] config keys onto the current schema."""

import configparser
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_SEED_INI = "[core]\nexternal_tools_detected = True\n"


def _real_config_module(config_dir: Path):
    """Return the genuine faststack.config module.

    faststack/tests/test_prefetch_logic.py installs a MagicMock at
    sys.modules["faststack.config"] when it is imported and never restores it,
    so by the time this test runs the name may be a mock. Import the real
    module ourselves, with its module-level AppConfig() pointed at a throwaway
    directory, and leave sys.modules exactly as we found it.
    """
    existing = sys.modules.get("faststack.config")
    if isinstance(getattr(existing, "AppConfig", None), type):
        return existing, None

    (config_dir / "faststack.ini").write_text(_SEED_INI, encoding="utf-8")
    sys.modules.pop("faststack.config", None)
    try:
        with patch("faststack.logging_setup.get_app_data_dir", return_value=config_dir):
            module = importlib.import_module("faststack.config")
    finally:
        pass
    return module, existing


class UpdateConfigMigrationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_dir = Path(self._tmp.name)

        module, replaced = _real_config_module(self.config_dir)
        self.AppConfig = module.AppConfig
        if replaced is not None:
            # Put the mock back so unrelated tests see what they expect.
            self.addCleanup(sys.modules.__setitem__, "faststack.config", replaced)

        patcher = patch.object(module, "get_app_data_dir", return_value=self.config_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

        # External tool detection walks the filesystem; irrelevant here.
        detect_patcher = patch.object(
            self.AppConfig, "_detect_external_tool_paths", return_value=False
        )
        detect_patcher.start()
        self.addCleanup(detect_patcher.stop)

    def _write_ini(self, text: str) -> None:
        (self.config_dir / "faststack.ini").write_text(text, encoding="utf-8")

    def _read_ini(self) -> configparser.ConfigParser:
        parser = configparser.ConfigParser()
        parser.read(self.config_dir / "faststack.ini")
        return parser

    def test_legacy_last_check_at_becomes_last_successful_check(self):
        self._write_ini(
            "[core]\nexternal_tools_detected = True\n\n"
            "[updates]\n"
            "check_for_updates = false\n"
            "auto_update = false\n"
            "last_check_at = 2026-08-20T10:00:00+00:00\n"
            "last_ignored_version = 1.6.7\n"
        )

        config = self.AppConfig()

        self.assertEqual(
            config.get("updates", "last_successful_update_check"),
            "2026-08-20T10:00:00+00:00",
        )
        # Existing preferences survive untouched.
        self.assertFalse(config.getboolean("updates", "check_for_updates"))
        self.assertEqual(config.get("updates", "last_ignored_version"), "1.6.7")
        # And the migration is persisted, not just in memory.
        self.assertEqual(
            self._read_ini().get("updates", "last_successful_update_check"),
            "2026-08-20T10:00:00+00:00",
        )

    def test_dead_keys_are_dropped(self):
        self._write_ini(
            "[core]\nexternal_tools_detected = True\n\n"
            "[updates]\n"
            "auto_update = true\n"
            "last_check_at = 2026-08-20T10:00:00+00:00\n"
        )

        self.AppConfig()

        parser = self._read_ini()
        self.assertFalse(parser.has_option("updates", "auto_update"))
        self.assertFalse(parser.has_option("updates", "last_check_at"))

    def test_existing_new_key_is_not_overwritten(self):
        self._write_ini(
            "[core]\nexternal_tools_detected = True\n\n"
            "[updates]\n"
            "last_check_at = 2020-01-01T00:00:00+00:00\n"
            "last_successful_update_check = 2026-08-25T09:00:00+00:00\n"
        )

        config = self.AppConfig()

        self.assertEqual(
            config.get("updates", "last_successful_update_check"),
            "2026-08-25T09:00:00+00:00",
        )

    def test_config_without_updates_section_gets_defaults(self):
        self._write_ini("[core]\nexternal_tools_detected = True\n")

        config = self.AppConfig()

        self.assertTrue(config.getboolean("updates", "check_for_updates"))
        self.assertEqual(config.get("updates", "last_successful_update_check"), "")
        self.assertEqual(config.get("updates", "last_failed_update_check"), "")

    def test_fresh_config_has_no_dead_keys(self):
        config = self.AppConfig()

        self.assertTrue(config.getboolean("updates", "check_for_updates"))
        self.assertIsNone(config.get("updates", "auto_update"))
        self.assertIsNone(config.get("updates", "last_check_at"))


if __name__ == "__main__":
    unittest.main()
