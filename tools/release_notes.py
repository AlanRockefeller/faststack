#!/usr/bin/env python3
"""Build GitHub Release title and notes for a FastStack tag.

ChangeLog.md is the human-maintained source of truth for what shipped in a
version. This script lifts the section for the tag's base version out of it so
the published release carries real release notes instead of "Automated
FastStack executable build for ...".

Tags look like ``v1.6.8-build2`` (see build-release.sh): the ``-buildN``
counter is a rebuild of the *same* application version, so it never changes
which changelog section is used, and rebuilds after the first are labelled as
such so nobody reads them as a new version.

Used by .github/workflows/build-executables.yml; importable (and tested) as
tools.release_notes.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Reuse the shipping normalization so the release notes and the in-app update
# check can never disagree about which version a tag represents.
from faststack.updater import GITHUB_REPOSITORY, normalize_version  # noqa: E402

CHANGELOG_URL = f"https://github.com/{GITHUB_REPOSITORY}/blob/main/ChangeLog.md"
BUILD_NUMBER_RE = re.compile(r"[-_.+]?build[-_.]?(\d+)", re.IGNORECASE)
SECTION_RE = re.compile(r"^##\s+(?!#)")


def build_number(tag: str) -> int | None:
    """Return the ``-buildN`` counter in a tag, or None when there is none."""
    match = BUILD_NUMBER_RE.search(str(tag or ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:  # pragma: no cover - regex only matches digits
        return None


def extract_changelog_section(changelog_text: str, version: str) -> str:
    """Return the body of the ``## <version>`` section, or "" when absent.

    Matching is anchored on the version token so ``1.6.8`` never matches
    ``1.6.80``. Everything up to the next top-level ``##`` heading is kept,
    including ``###`` subsections.
    """
    if not version:
        return ""

    heading_re = re.compile(rf"^##\s+v?{re.escape(version)}(?![\w.])")
    lines = changelog_text.splitlines()
    collected: list[str] = []
    in_section = False

    for line in lines:
        if in_section:
            if SECTION_RE.match(line):
                break
            collected.append(line)
            continue
        if heading_re.match(line):
            in_section = True

    return "\n".join(collected).strip()


def release_title(tag: str) -> str:
    """Title for the GitHub Release of this tag."""
    version = normalize_version(tag) or str(tag or "").strip()
    number = build_number(tag)
    if number is not None and number > 1:
        return f"FastStack {version} (build {number})"
    return f"FastStack {version}"


def render_release_notes(changelog_text: str, tag: str) -> str:
    """Compose the release body for a tag from the changelog."""
    version = normalize_version(tag) or str(tag or "").strip()
    section = extract_changelog_section(changelog_text, version)
    number = build_number(tag)

    parts: list[str] = []
    if number is not None and number > 1:
        # A rebuild ships the same application version. Say so plainly: the
        # in-app updater deliberately does not offer these to existing users.
        parts.append(
            f"Packaging rebuild of FastStack {version} (build {number}). "
            f"The application version is unchanged, so FastStack will not "
            f"offer this as an update to anyone already running {version}."
        )

    if section:
        parts.append(f"## What's new in {version}\n\n{section}")
    else:
        parts.append(
            f"FastStack {version}. ChangeLog.md has no section for this "
            f"version yet; see the full changelog for details."
        )

    parts.append(f"---\n\nFull changelog: {CHANGELOG_URL}")
    return "\n\n".join(parts).strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="release tag, e.g. v1.6.8-build2")
    parser.add_argument(
        "--changelog",
        default=str(REPO_ROOT / "ChangeLog.md"),
        help="path to ChangeLog.md",
    )
    parser.add_argument(
        "--notes-output",
        default="",
        help="write the release body here instead of stdout",
    )
    parser.add_argument(
        "--print-title",
        action="store_true",
        help="print the release title instead of the body",
    )
    args = parser.parse_args(argv)

    if args.print_title:
        print(release_title(args.tag))
        return 0

    try:
        changelog_text = Path(args.changelog).read_text(encoding="utf-8")
    except OSError as e:
        # A missing changelog must not fail a release; fall back to a
        # minimal but honest body.
        print(f"warning: could not read {args.changelog}: {e}", file=sys.stderr)
        changelog_text = ""

    notes = render_release_notes(changelog_text, args.tag)
    if args.notes_output:
        Path(args.notes_output).write_text(notes, encoding="utf-8")
    else:
        sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
