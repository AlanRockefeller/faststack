"""GitHub release update checks for FastStack.

This module is deliberately UI-free and dependency-light so the update policy
can be unit tested without Qt or network access. FastStack only *notifies*
about updates; it never downloads or installs one.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any

try:
    from packaging.version import InvalidVersion, Version
except ImportError:  # pragma: no cover - dependency fallback for stale dev envs
    InvalidVersion = ValueError
    Version = None

log = logging.getLogger(__name__)

GITHUB_REPOSITORY = "AlanRockefeller/faststack"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
USER_AGENT = "FastStack Update Checker"
FALLBACK_VERSION = "unknown"

# Release tags are cut as v<version>[-buildN] (see build-release.sh). The build
# counter identifies a *rebuild of the same application version*, so it is
# stripped before any comparison: 1.6.8, v1.6.8-build2 and v1.6.8_build11 all
# mean user-facing version 1.6.8. Build numbers are never reinterpreted as
# patch or post-release increments.
BUILD_SUFFIX_RE = re.compile(
    r"[-_.+]?build[-_.]?\d+(?:[-_.].*)?$",
    re.IGNORECASE,
)

# Hosts and path shape accepted when opening a release page in the browser.
ALLOWED_RELEASE_HOSTS = frozenset({"github.com", "www.github.com"})
# Git tags may contain far more than this, but every tag this project cuts is
# v<digits and dots>[-buildN]. Keeping the accepted set tight means a URL built
# from an API-supplied tag cannot smuggle in path traversal, a query string or
# credentials.
SAFE_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

# Automatic-check pacing. A check that actually reached GitHub is good for a
# day; one that failed (offline, DNS, timeout, 5xx, unparseable body) must not
# silence the checker until tomorrow, so it is retried much sooner.
SUCCESS_CHECK_INTERVAL = timedelta(hours=24)
FAILURE_RETRY_INTERVAL = timedelta(hours=1)


class UpdateCheckError(RuntimeError):
    """Raised when an update check cannot be completed."""


@dataclass(frozen=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    tag_name: str
    release_name: str
    release_url: str
    published_at: str
    summary: str
    body: str
    asset_names: tuple[str, ...]
    is_newer: bool

    def to_qml_dict(self) -> dict[str, Any]:
        return {
            "currentVersion": self.current_version,
            "latestVersion": self.latest_version,
            "tagName": self.tag_name,
            "releaseName": self.release_name,
            "releaseUrl": self.release_url,
            "publishedAt": self.published_at,
            "summary": self.summary,
            "body": self.body,
            "assetNames": list(self.asset_names),
            "isNewer": self.is_newer,
        }


def _read_pyproject_version(pyproject_path: Path) -> str | None:
    """Return ``[project].version`` from pyproject.toml when available."""
    try:
        with pyproject_path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        log.debug("Could not read version from %s", pyproject_path, exc_info=True)
        return None

    version = data.get("project", {}).get("version")
    if isinstance(version, str) and version.strip():
        return version.strip()

    log.debug("No [project].version found in %s", pyproject_path)
    return None


def get_current_version() -> str:
    """Return the user-facing FastStack version that is actually running.

    Frozen (PyInstaller) builds have no repository next to them, so they use
    the package metadata the spec bundles via ``copy_metadata``. Source-tree
    runs trust the nearby pyproject.toml first, because an editable install's
    metadata keeps whatever version it was installed at and would otherwise
    make a developer's checkout look out of date. Installed wheels have no
    pyproject next to them and fall through to metadata as well.

    Returns FALLBACK_VERSION when nothing is resolvable; that value never
    parses as a version, so it can only ever suppress an update notification,
    never invent one.
    """
    frozen = bool(getattr(sys, "frozen", False))

    pyproject_version = None
    if not frozen:
        pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
        if pyproject_path.is_file():
            pyproject_version = _read_pyproject_version(pyproject_path)

    metadata_version = None
    try:
        metadata_version = metadata.version("faststack")
    except metadata.PackageNotFoundError:
        pass
    except Exception:  # pragma: no cover - corrupt metadata on odd installs
        log.debug("Could not read faststack package metadata", exc_info=True)

    if pyproject_version:
        if metadata_version and metadata_version != pyproject_version:
            log.debug(
                "FastStack version mismatch: pyproject.toml has %s, "
                "package metadata has %s",
                pyproject_version,
                metadata_version,
            )
        return pyproject_version

    if metadata_version:
        return metadata_version

    return FALLBACK_VERSION


def normalize_version(version: str) -> str:
    """Return the user-facing base version encoded in a tag or version string.

    Strips a leading ``v``, any local version segment and the ``-buildN``
    rebuild counter, so ``v1.6.8-build4`` normalizes to ``1.6.8``.
    """
    if not isinstance(version, str):
        return ""
    value = version.strip()
    if value.startswith(("v", "V")):
        value = value[1:]
    value = value.split("+", 1)[0]
    value = BUILD_SUFFIX_RE.sub("", value)
    return value.strip(" -_.")


def parse_version(version: str) -> Version | None:
    """Parse a normalized version, returning None when it is not a version."""
    normalized = normalize_version(version)
    if not normalized or Version is None:
        return None
    try:
        return Version(normalized)
    except InvalidVersion:
        return None


def same_base_version(left: str, right: str) -> bool:
    """True when two tags/versions describe the same user-facing version.

    Used for "Skip This Version": skipping 1.6.8 must also cover every
    ``v1.6.8-buildN`` rebuild of it.
    """
    left_normalized = normalize_version(left)
    right_normalized = normalize_version(right)
    if not left_normalized or not right_normalized:
        return False
    if left_normalized == right_normalized:
        return True

    left_version = parse_version(left_normalized)
    right_version = parse_version(right_normalized)
    if left_version is None or right_version is None:
        return False
    return left_version == right_version


def is_newer_version(latest: str, current: str) -> bool:
    """Return True only when latest has a newer *base* version than current.

    A build-only release (same base version, different ``-buildN``) is never
    newer, and an unparseable version on either side never triggers a
    notification.
    """
    if Version is None:
        latest_key = _fallback_version_key(latest)
        current_key = _fallback_version_key(current)
        if latest_key is None or current_key is None:
            log.warning(
                "Could not compare update versions: latest=%r current=%r",
                latest,
                current,
            )
            return False
        return latest_key > current_key

    latest_version = parse_version(latest)
    current_version = parse_version(current)
    if latest_version is None or current_version is None:
        log.warning(
            "Could not parse update versions: latest=%r current=%r",
            latest,
            current,
        )
        return False
    return latest_version > current_version


def _fallback_version_key(version: str) -> tuple[int, ...] | None:
    """Best-effort numeric comparison when packaging is unavailable."""
    parts = re.findall(r"\d+", normalize_version(version))
    if not parts:
        return None
    return tuple(int(part) for part in parts)


def release_url_for_tag(tag: str) -> str:
    """Build the canonical release page URL for a tag, or "" if unsafe.

    The tag comes from the GitHub API, so it is only trusted after it matches
    SAFE_TAG_RE; anything else yields an empty string rather than a URL.
    """
    candidate = str(tag or "").strip()
    if not SAFE_TAG_RE.match(candidate):
        return ""
    return (
        f"https://github.com/{GITHUB_REPOSITORY}/releases/tag/"
        f"{urllib.parse.quote(candidate, safe='')}"
    )


def is_release_url_allowed(url: str) -> bool:
    """True only for an https GitHub release URL of this project's repository.

    API payloads are untrusted input: nothing is handed to the OS browser
    unless the scheme, host and owner/repo/releases path all check out.
    """
    if not isinstance(url, str):
        return False
    candidate = url.strip()
    if not candidate or len(candidate) > 2048:
        return False

    try:
        parsed = urllib.parse.urlsplit(candidate)
        port = parsed.port
        host = parsed.hostname
        username = parsed.username
        password = parsed.password
    except ValueError:
        return False

    if parsed.scheme.lower() != "https":
        return False
    if username or password:
        return False
    if port not in (None, 443):
        return False
    if (host or "").lower() not in ALLOWED_RELEASE_HOSTS:
        return False

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3:
        return False
    owner, repo, section = parts[0], parts[1], parts[2]
    if f"{owner}/{repo}".lower() != GITHUB_REPOSITORY.lower():
        return False
    return section == "releases"


def should_check_for_updates(
    *,
    now: datetime,
    last_success: datetime | None = None,
    last_failure: datetime | None = None,
    success_interval: timedelta = SUCCESS_CHECK_INTERVAL,
    failure_interval: timedelta = FAILURE_RETRY_INTERVAL,
) -> bool:
    """Automatic-check cooldown policy.

    A *successful* check (GitHub answered and the answer parsed) suppresses
    automatic checks for ``success_interval``. A *failed* attempt only holds
    off for ``failure_interval``, so a laptop that was offline at launch
    retries within the same session rather than staying quiet for a day.

    Timestamps in the future (a clock that moved backwards, a hand-edited INI)
    are treated as unusable rather than blocking checks indefinitely. Manual
    checks never consult this function.
    """
    for timestamp, interval in (
        (last_success, success_interval),
        (last_failure, failure_interval),
    ):
        if timestamp is None:
            continue
        elapsed = now - timestamp
        if timedelta(0) <= elapsed < interval:
            return False
    return True


def summarize_release_body(body: str, limit: int = 900) -> str:
    """Return a compact summary suitable for the in-app update dialog."""
    lines: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lines.append(line)
        if len("\n".join(lines)) >= limit or len(lines) >= 10:
            break

    summary = "\n".join(lines).strip()
    if len(summary) > limit:
        summary = summary[: limit - 3].rstrip() + "..."
    return summary


def fetch_latest_release(timeout: float = 5.0) -> dict[str, Any]:
    """Fetch the latest non-prerelease GitHub release payload."""
    request = urllib.request.Request(
        LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if getattr(response, "status", 200) >= 400:
                raise UpdateCheckError(f"GitHub returned HTTP {response.status}")
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise UpdateCheckError(f"GitHub returned HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise UpdateCheckError(f"Could not reach GitHub: {e.reason}") from e
    except TimeoutError as e:
        raise UpdateCheckError("GitHub update check timed out") from e
    except OSError as e:
        raise UpdateCheckError(f"Could not reach GitHub: {e}") from e
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise UpdateCheckError("GitHub returned an invalid release response") from e


def check_for_update(
    *,
    current_version: str | None = None,
    timeout: float = 5.0,
) -> UpdateInfo:
    """Check GitHub Releases and return normalized update information."""
    current = current_version or get_current_version()
    payload = fetch_latest_release(timeout=timeout)
    if not isinstance(payload, dict):
        raise UpdateCheckError(
            f"GitHub returned an unexpected release payload shape: "
            f"{type(payload).__name__}"
        )

    tag_name = str(payload.get("tag_name") or "").strip()
    latest_version = normalize_version(tag_name)
    if not latest_version:
        raise UpdateCheckError("Latest GitHub release did not include a tag")

    release_name = str(payload.get("name") or tag_name)
    published_at = str(payload.get("published_at") or "")
    body = str(payload.get("body") or "")

    release_url = str(payload.get("html_url") or "").strip()
    if release_url and not is_release_url_allowed(release_url):
        log.warning(
            "Ignoring release URL that is not a %s release page: %r",
            GITHUB_REPOSITORY,
            release_url,
        )
        release_url = ""
    if not release_url:
        # Rebuild the canonical page from the tag rather than shipping a dead
        # button. Returns "" when even the tag looks untrustworthy.
        release_url = release_url_for_tag(tag_name)

    assets = payload.get("assets")
    if not isinstance(assets, list):
        assets = []
    asset_names = tuple(
        str(asset.get("name"))
        for asset in assets
        if isinstance(asset, dict) and asset.get("name")
    )

    return UpdateInfo(
        current_version=current,
        latest_version=latest_version,
        tag_name=tag_name,
        release_name=release_name,
        release_url=release_url,
        published_at=published_at,
        summary=summarize_release_body(body),
        body=body,
        asset_names=asset_names,
        is_newer=is_newer_version(latest_version, current),
    )
