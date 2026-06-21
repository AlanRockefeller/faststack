"""GitHub release update checks for FastStack."""

from __future__ import annotations

import json
import logging
import re
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
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
BUILD_SUFFIX_RE = re.compile(
    r"[-_.+]?build[-_.]?\d+(?:[-_.].*)?$",
    re.IGNORECASE,
)


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


def get_current_version() -> str:
    """Return the installed FastStack version.

    Installed packages expose metadata. Running directly from a source checkout
    usually does not, so fall back to pyproject.toml and then "unknown" as a
    last resort. Frozen builds include package metadata from the installed
    project, so they should normally use the metadata path.
    """
    try:
        return metadata.version("faststack")
    except metadata.PackageNotFoundError:
        pass

    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    try:
        with pyproject_path.open("rb") as f:
            data = tomllib.load(f)
        version = data.get("project", {}).get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()
    except (OSError, tomllib.TOMLDecodeError):
        log.debug("Could not read version from %s", pyproject_path, exc_info=True)

    return FALLBACK_VERSION


def normalize_version(version: str) -> str:
    """Normalize common release tag forms for comparison."""
    value = version.strip()
    if value.startswith(("v", "V")):
        value = value[1:]
    value = value.split("+", 1)[0]
    value = BUILD_SUFFIX_RE.sub("", value)
    return value


def is_newer_version(latest: str, current: str) -> bool:
    """Return True when latest has a newer public version than current."""
    if Version is None:
        return _fallback_version_key(latest) > _fallback_version_key(current)

    try:
        return Version(normalize_version(latest)) > Version(normalize_version(current))
    except InvalidVersion:
        log.warning(
            "Could not parse update versions: latest=%r current=%r",
            latest,
            current,
        )
        return False


def _fallback_version_key(version: str) -> tuple[int, ...]:
    """Best-effort numeric comparison when packaging is unavailable."""
    parts = re.findall(r"\d+", normalize_version(version))
    if not parts:
        return (0,)
    return tuple(int(part) for part in parts)


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
    release_url = str(payload.get("html_url") or "")
    published_at = str(payload.get("published_at") or "")
    body = str(payload.get("body") or "")
    assets = payload.get("assets") or []
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
