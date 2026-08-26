"""Update-check policy tests: version normalization, URLs, cooldowns.

These cover faststack/updater.py only, which is deliberately Qt-free, so they
run without a display or a network connection.
"""

import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from faststack import updater
from faststack.updater import (
    FAILURE_RETRY_INTERVAL,
    SUCCESS_CHECK_INTERVAL,
    UpdateCheckError,
    check_for_update,
    is_newer_version,
    is_release_url_allowed,
    normalize_version,
    release_url_for_tag,
    same_base_version,
    should_check_for_updates,
    summarize_release_body,
)

NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


def _release_payload(**overrides):
    payload = {
        "tag_name": "v1.6.9-build2",
        "name": "FastStack 1.6.9",
        "html_url": "https://github.com/AlanRockefeller/faststack/releases/tag/v1.6.9-build2",
        "published_at": "2026-08-20T10:00:00Z",
        "body": "## What's new in 1.6.9\n\n- Something good\n",
        "assets": [{"name": "FastStack-windows-x64.zip"}],
    }
    payload.update(overrides)
    return payload


class TestNormalizeVersion(unittest.TestCase):
    def test_strips_v_prefix(self):
        self.assertEqual(normalize_version("v1.6.8"), "1.6.8")
        self.assertEqual(normalize_version("V1.6.8"), "1.6.8")
        self.assertEqual(normalize_version("1.6.8"), "1.6.8")

    def test_strips_build_suffixes(self):
        for tag in (
            "v1.6.8-build1",
            "v1.6.8-build2",
            "v1.6.8-build47",
            "v1.6.8_build3",
            "v1.6.8.build3",
            "v1.6.8+build3",
            "v1.6.8-BUILD3",
            "v1.6.8-build3-hotfix",
        ):
            with self.subTest(tag=tag):
                self.assertEqual(normalize_version(tag), "1.6.8")

    def test_strips_local_version_segment(self):
        self.assertEqual(normalize_version("1.6.8+g1234abc"), "1.6.8")

    def test_handles_junk(self):
        self.assertEqual(normalize_version(""), "")
        self.assertEqual(normalize_version("   "), "")
        self.assertEqual(normalize_version(None), "")
        self.assertEqual(normalize_version("unknown"), "unknown")


class TestBuildOnlyReleasesAreNotUpdates(unittest.TestCase):
    """The product rule: -buildN is the same user-facing version."""

    def test_same_base_version_with_build_suffix_is_not_newer(self):
        for latest in ("v1.6.8-build1", "v1.6.8-build4", "v1.6.8-build11", "1.6.8"):
            with self.subTest(latest=latest):
                self.assertFalse(is_newer_version(latest, "1.6.8"))

    def test_newer_base_version_with_build_suffix_is_newer(self):
        self.assertTrue(is_newer_version("v1.6.9-build1", "1.6.8"))
        self.assertTrue(is_newer_version("v1.7.0-build1", "1.6.9"))
        self.assertTrue(is_newer_version("v1.6.9", "1.6.8"))
        self.assertTrue(is_newer_version("v2.0.0-build9", "1.6.8"))

    def test_installed_build_variant_still_compares_by_base_version(self):
        self.assertFalse(is_newer_version("v1.6.9-build3", "1.6.9"))
        self.assertFalse(is_newer_version("v1.6.9-build3", "1.6.9-build1"))
        self.assertTrue(is_newer_version("v1.7.0-build1", "1.6.9-build3"))

    def test_older_release_is_not_newer(self):
        self.assertFalse(is_newer_version("v1.6.7", "1.6.8"))
        self.assertFalse(is_newer_version("v1.6.7-build9", "1.6.8"))
        self.assertFalse(is_newer_version("v1.5.0", "1.6.8-build2"))

    def test_build_number_is_not_a_patch_bump(self):
        # A high build counter must never outrank a real version component.
        self.assertFalse(is_newer_version("v1.6.8-build99", "1.6.8"))
        self.assertFalse(is_newer_version("v1.6.8-build99", "1.6.9"))

    def test_malformed_versions_never_notify(self):
        for latest, current in (
            ("", "1.6.8"),
            ("1.6.8", ""),
            ("not-a-version", "1.6.8"),
            ("1.6.9", "unknown"),
            ("unknown", "1.6.8"),
            ("v", "1.6.8"),
        ):
            with self.subTest(latest=latest, current=current):
                self.assertFalse(is_newer_version(latest, current))


class TestFallbackVersionComparison(unittest.TestCase):
    """The packaging-less fallback path must follow the same policy."""

    def test_fallback_key_comparison(self):
        with patch.object(updater, "Version", None):
            self.assertFalse(is_newer_version("v1.6.8-build4", "1.6.8"))
            self.assertTrue(is_newer_version("v1.6.9-build1", "1.6.8"))
            self.assertFalse(is_newer_version("v1.6.7", "1.6.8"))
            self.assertFalse(is_newer_version("garbage", "1.6.8"))


class TestSkipVersionAcrossBuilds(unittest.TestCase):
    def test_skipping_a_version_covers_its_builds(self):
        self.assertTrue(same_base_version("1.6.8", "v1.6.8-build3"))
        self.assertTrue(same_base_version("v1.6.8-build1", "v1.6.8-build9"))
        self.assertTrue(same_base_version("1.6.8", "1.6.8"))

    def test_skipping_does_not_cover_a_later_version(self):
        self.assertFalse(same_base_version("1.6.8", "v1.6.9-build1"))
        self.assertFalse(same_base_version("1.6.8", "1.7.0"))

    def test_junk_never_matches(self):
        self.assertFalse(same_base_version("", "1.6.8"))
        self.assertFalse(same_base_version("1.6.8", ""))
        self.assertFalse(same_base_version("nonsense", "1.6.8"))


class TestReleaseUrlAllowlist(unittest.TestCase):
    def test_accepts_project_release_urls(self):
        for url in (
            "https://github.com/AlanRockefeller/faststack/releases/tag/v1.6.9-build1",
            "https://github.com/AlanRockefeller/faststack/releases/latest",
            "https://www.github.com/AlanRockefeller/faststack/releases/tag/v1.6.9",
            "https://github.com/alanrockefeller/FastStack/releases/tag/v1.6.9",
        ):
            with self.subTest(url=url):
                self.assertTrue(is_release_url_allowed(url))

    def test_rejects_everything_else(self):
        for url in (
            "",
            "   ",
            None,
            "http://github.com/AlanRockefeller/faststack/releases/tag/v1",
            "https://evil.example.com/AlanRockefeller/faststack/releases/tag/v1",
            "https://github.com.evil.example/AlanRockefeller/faststack/releases/v1",
            "https://github.com/SomeoneElse/faststack/releases/tag/v1",
            "https://github.com/AlanRockefeller/otherproject/releases/tag/v1",
            "https://github.com/AlanRockefeller/faststack/issues/1",
            "https://github.com/AlanRockefeller/faststack",
            "javascript:alert(1)",
            "file:///etc/passwd",
            "https://user:pass@github.com/AlanRockefeller/faststack/releases/tag/v1",
            "https://github.com:8443/AlanRockefeller/faststack/releases/tag/v1",
            "https://github.com/AlanRockefeller/faststack/releases/tag/v1" + "x" * 4096,
        ):
            with self.subTest(url=url):
                self.assertFalse(is_release_url_allowed(url))

    def test_release_url_for_tag_is_always_allowed(self):
        url = release_url_for_tag("v1.6.9-build2")
        self.assertEqual(
            url,
            "https://github.com/AlanRockefeller/faststack/releases/tag/v1.6.9-build2",
        )
        self.assertTrue(is_release_url_allowed(url))

    def test_release_url_for_unsafe_tag_is_empty(self):
        for tag in ("", "../../etc/passwd", "v1.6.9?x=1", "v1 6", "v1.6.9#frag"):
            with self.subTest(tag=tag):
                self.assertEqual(release_url_for_tag(tag), "")


class TestCheckCooldownPolicy(unittest.TestCase):
    def test_first_run_checks(self):
        self.assertTrue(should_check_for_updates(now=NOW))

    def test_recent_success_suppresses(self):
        last = NOW - (SUCCESS_CHECK_INTERVAL - timedelta(minutes=5))
        self.assertFalse(should_check_for_updates(now=NOW, last_success=last))

    def test_old_success_allows(self):
        last = NOW - (SUCCESS_CHECK_INTERVAL + timedelta(minutes=1))
        self.assertTrue(should_check_for_updates(now=NOW, last_success=last))

    def test_recent_failure_suppresses_only_briefly(self):
        recent = NOW - timedelta(minutes=10)
        self.assertFalse(should_check_for_updates(now=NOW, last_failure=recent))

    def test_failure_retries_long_before_the_daily_interval(self):
        # The whole point: a timeout must not buy GitHub 24 hours of silence.
        older = NOW - (FAILURE_RETRY_INTERVAL + timedelta(minutes=1))
        self.assertTrue(should_check_for_updates(now=NOW, last_failure=older))
        self.assertLess(FAILURE_RETRY_INTERVAL, SUCCESS_CHECK_INTERVAL)

    def test_stale_success_with_recent_failure_still_waits(self):
        self.assertFalse(
            should_check_for_updates(
                now=NOW,
                last_success=NOW - timedelta(days=30),
                last_failure=NOW - timedelta(minutes=1),
            )
        )

    def test_future_timestamps_do_not_block_forever(self):
        # A clock that jumped backwards must not disable update checks.
        self.assertTrue(
            should_check_for_updates(now=NOW, last_success=NOW + timedelta(days=400))
        )
        self.assertTrue(
            should_check_for_updates(now=NOW, last_failure=NOW + timedelta(days=400))
        )


class TestSummarizeReleaseBody(unittest.TestCase):
    def test_drops_blank_lines_and_truncates(self):
        summary = summarize_release_body("a\n\n\nb\n")
        self.assertEqual(summary, "a\nb")

        long_body = "\n".join(f"line {i}" for i in range(100))
        summary = summarize_release_body(long_body, limit=40)
        self.assertLessEqual(len(summary), 40)

    def test_empty_body(self):
        self.assertEqual(summarize_release_body(""), "")


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.status = status

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestCheckForUpdate(unittest.TestCase):
    def _check(self, payload, current="1.6.8"):
        with patch.object(
            updater.urllib.request, "urlopen", return_value=_FakeResponse(payload)
        ):
            return check_for_update(current_version=current)

    def test_build_only_release_reports_no_update(self):
        info = self._check(_release_payload(tag_name="v1.6.8-build4"), current="1.6.8")
        self.assertEqual(info.latest_version, "1.6.8")
        self.assertEqual(info.tag_name, "v1.6.8-build4")
        self.assertFalse(info.is_newer)

    def test_newer_build_tag_reports_the_base_version(self):
        info = self._check(_release_payload(tag_name="v1.6.9-build1"), current="1.6.8")
        self.assertEqual(info.latest_version, "1.6.9")
        self.assertTrue(info.is_newer)

    def test_untrusted_release_url_is_replaced_by_the_canonical_one(self):
        info = self._check(
            _release_payload(html_url="https://evil.example.com/pwn"),
            current="1.6.8",
        )
        self.assertEqual(
            info.release_url,
            "https://github.com/AlanRockefeller/faststack/releases/tag/v1.6.9-build2",
        )
        self.assertTrue(is_release_url_allowed(info.release_url))

    def test_untrusted_url_and_untrusted_tag_yield_no_url(self):
        info = self._check(
            _release_payload(tag_name="v1.6.9 ../evil", html_url="ftp://x/y"),
            current="1.6.8",
        )
        self.assertEqual(info.release_url, "")

    def test_partial_payload_still_parses(self):
        info = self._check({"tag_name": "v1.6.9"}, current="1.6.8")
        self.assertEqual(info.latest_version, "1.6.9")
        self.assertEqual(info.release_name, "v1.6.9")
        self.assertEqual(info.body, "")
        self.assertEqual(info.asset_names, ())
        self.assertTrue(info.is_newer)

    def test_missing_tag_is_an_error(self):
        with self.assertRaises(UpdateCheckError):
            self._check({"name": "no tag here"})

    def test_non_dict_payload_is_an_error(self):
        with self.assertRaises(UpdateCheckError):
            self._check(["not", "a", "release"])

    def test_assets_of_the_wrong_shape_are_ignored(self):
        info = self._check(_release_payload(assets={"nope": True}), current="1.6.8")
        self.assertEqual(info.asset_names, ())

    def test_invalid_json_is_an_error(self):
        with patch.object(
            updater.urllib.request, "urlopen", return_value=_FakeResponse(b"<html>")
        ):
            with self.assertRaises(UpdateCheckError):
                check_for_update(current_version="1.6.8")

    def test_timeout_is_an_error(self):
        with patch.object(
            updater.urllib.request, "urlopen", side_effect=TimeoutError()
        ):
            with self.assertRaises(UpdateCheckError) as ctx:
                check_for_update(current_version="1.6.8")
        self.assertIn("timed out", str(ctx.exception))

    def test_network_failure_is_an_error(self):
        with patch.object(
            updater.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("no route to host"),
        ):
            with self.assertRaises(UpdateCheckError) as ctx:
                check_for_update(current_version="1.6.8")
        self.assertIn("Could not reach GitHub", str(ctx.exception))

    def test_http_error_is_an_error(self):
        with patch.object(
            updater.urllib.request,
            "urlopen",
            side_effect=urllib.error.HTTPError(
                updater.LATEST_RELEASE_URL, 503, "Service Unavailable", {}, None
            ),
        ):
            with self.assertRaises(UpdateCheckError) as ctx:
                check_for_update(current_version="1.6.8")
        self.assertIn("503", str(ctx.exception))

    def test_unknown_current_version_never_claims_an_update(self):
        info = self._check(_release_payload(), current=updater.FALLBACK_VERSION)
        self.assertFalse(info.is_newer)


class TestGetCurrentVersion(unittest.TestCase):
    def test_source_tree_prefers_pyproject_over_stale_metadata(self):
        with patch.object(updater, "_read_pyproject_version", return_value="1.6.8"):
            with patch.object(updater.metadata, "version", return_value="1.5.0"):
                with patch.object(updater.Path, "is_file", return_value=True):
                    self.assertEqual(updater.get_current_version(), "1.6.8")

    def test_frozen_build_uses_package_metadata(self):
        with patch.object(updater.sys, "frozen", True, create=True):
            with patch.object(updater.metadata, "version", return_value="1.6.8"):
                with patch.object(
                    updater, "_read_pyproject_version", return_value="9.9.9"
                ) as read_pyproject:
                    self.assertEqual(updater.get_current_version(), "1.6.8")
                    read_pyproject.assert_not_called()

    def test_no_version_source_falls_back_to_unknown(self):
        with patch.object(updater.Path, "is_file", return_value=False):
            with patch.object(
                updater.metadata,
                "version",
                side_effect=updater.metadata.PackageNotFoundError("faststack"),
            ):
                self.assertEqual(updater.get_current_version(), "unknown")


if __name__ == "__main__":
    unittest.main()
