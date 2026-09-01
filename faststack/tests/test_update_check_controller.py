"""AppController-level update-check behavior.

Covers the cooldown gating, the manual bypass, and the rule that an automatic
check never interrupts the user: it raises the footer banner (or stays silent
on failure) while a manual check opens the dialog.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from faststack.app import AppController
from faststack.updater import FAILURE_RETRY_INTERVAL, SUCCESS_CHECK_INTERVAL

NOW = datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


class FakeConfig:
    """Minimal stand-in for faststack.config.config backed by a dict."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.save_count = 0

    def get(self, section, key, fallback=None):
        return self.values.get((section, key), fallback)

    def getboolean(self, section, key, fallback=None):
        value = self.values.get((section, key), fallback)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value) if value is not None else fallback

    def getint(self, section, key, fallback=None):
        value = self.values.get((section, key), fallback)
        return int(value) if value is not None else fallback

    def getfloat(self, section, key, fallback=None):
        value = self.values.get((section, key), fallback)
        return float(value) if value is not None else fallback

    def set(self, section, key, value):
        self.values[(section, key)] = str(value)

    def save(self):
        self.save_count += 1


class UpdateCheckControllerTest(unittest.TestCase):
    def setUp(self):
        self.config = FakeConfig()
        config_patch = patch("faststack.app.config", self.config)
        config_patch.start()
        self.addCleanup(config_patch.stop)

        self.patches = [
            patch("faststack.app.QTimer"),
            patch("faststack.app.QDrag"),
            patch("faststack.app.QPixmap"),
            patch("faststack.app.QMimeData"),
            patch("faststack.app.QFileDialog"),
            patch("faststack.app.DecodedImage"),
            patch("faststack.app.ImageEditor"),
            patch("faststack.app.Prefetcher"),
            patch("faststack.app.ByteLRUCache"),
            patch("faststack.app.SidecarManager"),
            patch("faststack.app.Keybinder"),
            patch("faststack.app.Watcher"),
            patch("faststack.app.ThumbnailModel"),
            patch("faststack.app.ThumbnailCache"),
            patch("faststack.app.ThumbnailPrefetcher"),
            patch("faststack.app.ThumbnailProvider"),
            patch("faststack.app.PathResolver"),
            patch("faststack.app.UIState"),
            patch("faststack.app.ImageProvider"),
            patch("faststack.app.Path"),
            patch("faststack.app.concurrent.futures.ThreadPoolExecutor"),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

        self.controller = AppController(MagicMock(), MagicMock())
        self.controller.update_status_message = MagicMock()
        self.controller.main_window = MagicMock()
        self.controller._update_executor = MagicMock()

    # ---- automatic cooldown -------------------------------------------------

    def _run_maybe_check(self):
        with patch.object(self.controller, "check_for_updates") as check:
            self.controller.maybe_check_for_updates()
        return check

    def test_first_launch_checks(self):
        self._run_maybe_check().assert_called_once_with(False)

    def test_recent_success_suppresses_the_automatic_check(self):
        self.config.set(
            "updates",
            "last_successful_update_check",
            _iso(NOW - timedelta(hours=2)),
        )
        self._run_maybe_check().assert_not_called()

    def test_success_older_than_the_interval_checks_again(self):
        self.config.set(
            "updates",
            "last_successful_update_check",
            _iso(NOW - SUCCESS_CHECK_INTERVAL - timedelta(minutes=5)),
        )
        self._run_maybe_check().assert_called_once_with(False)

    def test_a_failed_check_does_not_buy_24_hours_of_silence(self):
        # This is the regression the retry policy exists for: GitHub timed out
        # two hours ago, so the next launch must try again.
        self.config.set(
            "updates",
            "last_failed_update_check",
            _iso(NOW - timedelta(hours=2)),
        )
        self._run_maybe_check().assert_called_once_with(False)

    def test_a_very_recent_failure_still_backs_off(self):
        self.config.set(
            "updates",
            "last_failed_update_check",
            _iso(NOW - (FAILURE_RETRY_INTERVAL - timedelta(minutes=5))),
        )
        self._run_maybe_check().assert_not_called()

    def test_disabled_setting_stops_automatic_checks(self):
        self.config.set("updates", "check_for_updates", "false")
        self._run_maybe_check().assert_not_called()

    def test_unparseable_timestamp_is_ignored(self):
        self.config.set("updates", "last_successful_update_check", "not a date")
        self._run_maybe_check().assert_called_once_with(False)

    # ---- manual checks ------------------------------------------------------

    def test_manual_check_bypasses_the_cooldown(self):
        self.config.set("updates", "last_successful_update_check", _iso(NOW))
        self.config.set("updates", "last_failed_update_check", _iso(NOW))

        self.controller.check_for_updates(True)

        self.controller._update_executor.submit.assert_called_once()

    def test_manual_check_runs_even_when_automatic_checks_are_disabled(self):
        self.config.set("updates", "check_for_updates", "false")

        self.controller.check_for_updates(True)

        self.controller._update_executor.submit.assert_called_once()

    def test_check_does_not_block_the_gui_thread(self):
        # The controller only ever hands check_for_update to the executor.
        self.controller.check_for_updates(True)
        submit = self.controller._update_executor.submit
        submit.assert_called_once()
        self.assertEqual(submit.call_args.args[0].__name__, "check_for_update")

    def test_no_timestamp_is_written_before_the_attempt_finishes(self):
        self.controller.check_for_updates(True)
        self.assertIsNone(self.config.get("updates", "last_successful_update_check"))
        self.assertIsNone(self.config.get("updates", "last_failed_update_check"))

    # ---- results ------------------------------------------------------------

    def _finish(self, **payload):
        payload.setdefault("token", self.controller._update_check_token)
        payload.setdefault("error", "")
        payload.setdefault("currentVersion", "1.6.8")
        self.controller._on_update_check_finished(payload)

    def test_automatic_failure_is_silent_and_recorded(self):
        self._finish(manual=False, error="Could not reach GitHub: timed out")

        self.controller.update_status_message.assert_not_called()
        self.controller.main_window.openUpdateDialog.assert_not_called()
        self.assertTrue(self.config.get("updates", "last_failed_update_check"))
        self.assertFalse(self.config.get("updates", "last_successful_update_check"))

    def test_manual_failure_reports_a_status_message(self):
        self._finish(manual=True, error="GitHub returned HTTP 503")

        self.controller.update_status_message.assert_called_once()
        message = self.controller.update_status_message.call_args.args[0]
        self.assertIn("503", message)
        self.assertIn("Could not check for updates", message)

    def test_success_records_the_timestamp_and_clears_the_failure(self):
        self.config.set("updates", "last_failed_update_check", _iso(NOW))

        self._finish(manual=False, isNewer=False, latestVersion="1.6.8")

        self.assertTrue(self.config.get("updates", "last_successful_update_check"))
        self.assertEqual(self.config.get("updates", "last_failed_update_check"), "")

    def test_automatic_update_raises_the_banner_instead_of_a_dialog(self):
        self._finish(manual=False, isNewer=True, latestVersion="1.6.9")

        self.controller.main_window.openUpdateDialog.assert_not_called()
        self.controller.update_status_message.assert_not_called()
        self.assertEqual(self.controller.get_pending_update_version(), "1.6.9")

    def test_manual_update_opens_the_dialog_immediately(self):
        self._finish(manual=True, isNewer=True, latestVersion="1.6.9")

        self.controller.main_window.openUpdateDialog.assert_called_once()
        self.assertEqual(self.controller.get_pending_update_version(), "")

    def test_banner_action_opens_the_dialog(self):
        self._finish(manual=False, isNewer=True, latestVersion="1.6.9")

        self.controller.show_pending_update()

        self.controller.main_window.openUpdateDialog.assert_called_once()

    def test_dismissing_the_banner_does_not_skip_the_version(self):
        self._finish(manual=False, isNewer=True, latestVersion="1.6.9")

        self.controller.dismiss_pending_update()

        self.assertEqual(self.controller.get_pending_update_version(), "")
        self.assertIsNone(self.config.get("updates", "last_ignored_version"))

        # "Remind Me Later" must not suppress the next automatic check.
        self._finish(manual=False, isNewer=True, latestVersion="1.6.9")
        self.assertEqual(self.controller.get_pending_update_version(), "1.6.9")

    def test_skipped_version_suppresses_all_of_its_builds(self):
        self.controller.skip_update_version("v1.6.9-build2")
        self.assertEqual(self.config.get("updates", "last_ignored_version"), "1.6.9")

        for latest in ("1.6.9", "1.6.9"):
            self._finish(manual=False, isNewer=True, latestVersion=latest)
            self.assertEqual(self.controller.get_pending_update_version(), "")

    def test_skipped_version_does_not_suppress_the_next_version(self):
        self.controller.skip_update_version("1.6.8")

        self._finish(manual=False, isNewer=True, latestVersion="1.6.9")

        self.assertEqual(self.controller.get_pending_update_version(), "1.6.9")

    def test_skipped_version_still_shows_on_a_manual_check(self):
        self.controller.skip_update_version("1.6.9")

        self._finish(manual=True, isNewer=True, latestVersion="1.6.9")

        self.controller.main_window.openUpdateDialog.assert_called_once()

    def test_stale_token_results_are_discarded(self):
        self.controller._update_check_token = 7
        self.controller._on_update_check_finished(
            {
                "token": 3,
                "manual": True,
                "error": "",
                "isNewer": True,
                "latestVersion": "1.6.9",
            }
        )
        self.controller.main_window.openUpdateDialog.assert_not_called()

    def test_up_to_date_manual_check_says_so(self):
        self._finish(manual=True, isNewer=False, currentVersion="1.6.8")

        message = self.controller.update_status_message.call_args.args[0]
        self.assertIn("up to date", message)

    # ---- release URL --------------------------------------------------------

    def test_open_update_release_rejects_a_foreign_url(self):
        with patch("faststack.app.QDesktopServices") as desktop:
            self.controller.open_update_release("https://evil.example.com/pwn")
            desktop.openUrl.assert_not_called()
        self.controller.update_status_message.assert_called_once()
        self.assertIn(
            "not opening", self.controller.update_status_message.call_args.args[0]
        )

    def test_open_update_release_opens_a_real_release_url(self):
        url = "https://github.com/AlanRockefeller/faststack/releases/tag/v1.6.9-build1"
        with patch("faststack.app.QDesktopServices") as desktop:
            desktop.openUrl.return_value = True
            self.controller.open_update_release(url)
            desktop.openUrl.assert_called_once()

    def test_open_update_release_handles_an_empty_url(self):
        with patch("faststack.app.QDesktopServices") as desktop:
            self.controller.open_update_release("")
            desktop.openUrl.assert_not_called()
        self.assertIn(
            "No update release URL",
            self.controller.update_status_message.call_args.args[0],
        )


if __name__ == "__main__":
    unittest.main()
