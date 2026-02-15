"""Variant (backup + developed) parsing and grouping for image files.

Pure-logic module with no Qt dependencies.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# Token-boundary regex: match `-developed` as a real dash-delimited token.
# Ensures "undeveloped" or "mydeveloped" do NOT match.
_DEVELOPED_TOKEN_RE = re.compile(r"(?:^|(?<=-))[Dd][Ee][Vv][Ee][Ll][Oo][Pp][Ee][Dd](?=$|(?=-))")

# Trailing `-backup(\d+)?` token at end of (stripped) stem.
_BACKUP_TRAILING_RE = re.compile(r"(?:^|-)([Bb][Aa][Cc][Kk][Uu][Pp])(\d+)?$")


@dataclass
class VariantInfo:
    """Parsed information about a single image file's variant role."""

    path: Path
    group_key: str  # case-preserved (NOT casefolded)
    is_developed: bool
    backup_number: Optional[int]  # None = not a backup, 1 = -backup, N = -backupN


@dataclass
class VariantGroup:
    """A group of related variant files sharing the same base stem."""

    group_key: str
    main_path: Optional[Path] = None
    developed_path: Optional[Path] = None
    backup_paths: Dict[int, Path] = field(default_factory=dict)  # N -> path
    all_files: List[VariantInfo] = field(default_factory=list)


def parse_variant_stem(stem: str) -> Tuple[str, bool, Optional[int]]:
    """Parse a filename stem into (group_key, is_developed, backup_number).

    Token matching rules:
    - `-developed` must be a real dash-delimited token (not a substring)
    - `-backup(\\d+)?` must be a trailing token

    Returns:
        (group_key, is_developed, backup_number)
        - group_key: case-preserved stem with variant tokens removed
        - is_developed: True if stem contains a `-developed` token
        - backup_number: None if not a backup, 1 for `-backup`, N for `-backupN`
    """
    # 1. Check for -developed token
    is_developed = bool(_DEVELOPED_TOKEN_RE.search(stem))

    # 2. Remove exactly one -developed token (first occurrence) -> stripped
    if is_developed:
        # Find the match and remove it, handling leading/trailing dashes
        m = _DEVELOPED_TOKEN_RE.search(stem)
        start, end = m.start(), m.end()
        # Remove the token and any resulting double-dash or leading/trailing dash
        before = stem[:start]
        after = stem[end:]
        # Clean up dashes at the join point
        if before.endswith("-") and (after.startswith("-") or after == ""):
            before = before[:-1]
        elif before == "" and after.startswith("-"):
            after = after[1:]
        stripped = before + after
    else:
        stripped = stem

    # 3. Check stripped for trailing -backup(\d+)? token
    backup_number = None
    bm = _BACKUP_TRAILING_RE.search(stripped)
    if bm:
        num_str = bm.group(2)
        backup_number = int(num_str) if num_str else 1
        group_key = stripped[: bm.start()]
    else:
        group_key = stripped

    return group_key, is_developed, backup_number


def build_variant_map(
    all_jpg_paths: List[Path],
) -> Dict[str, VariantGroup]:
    """Build a mapping from group_key (casefolded) to VariantGroup.

    Args:
        all_jpg_paths: All JPG paths in the directory (including backups).

    Returns:
        Dict keyed by group_key.casefold() -> VariantGroup with selection rules applied.
    """
    # 1. Parse all files
    groups: Dict[str, VariantGroup] = {}  # keyed by group_key.casefold()

    for path in all_jpg_paths:
        group_key, is_developed, backup_number = parse_variant_stem(path.stem)
        key_cf = group_key.casefold()

        info = VariantInfo(
            path=Path(norm_path(path)),
            group_key=group_key,
            is_developed=is_developed,
            backup_number=backup_number,
        )

        if key_cf not in groups:
            groups[key_cf] = VariantGroup(group_key=group_key)
        groups[key_cf].all_files.append(info)

    # 2. Apply selection rules for each group
    for group in groups.values():
        _select_main(group)
        _select_developed(group)
        _select_backups(group)

    return groups


def _select_main(group: VariantGroup) -> None:
    """Select the main file for a variant group.

    Priority: non-backup non-developed > non-backup developed > lowest backup.
    Tiebreak: str(path) lexicographic.
    """
    candidates = []
    for info in group.all_files:
        # Priority tier: (0=non-backup non-dev, 1=non-backup dev, 2=backup)
        if info.backup_number is None and not info.is_developed:
            tier = 0
        elif info.backup_number is None and info.is_developed:
            tier = 1
        else:
            tier = 2
        backup_n = info.backup_number if info.backup_number is not None else 0
        candidates.append((tier, backup_n, str(info.path), info))

    candidates.sort()
    if candidates:
        group.main_path = candidates[0][3].path


def _select_developed(group: VariantGroup) -> None:
    """Select the developed target for a variant group.

    Tier 1: developed + non-backup.
    Tier 2: developed + backup (lowest N).
    Tiebreak: str(path) lexicographic.
    """
    candidates = []
    for info in group.all_files:
        if not info.is_developed:
            continue
        if info.backup_number is None:
            tier = 0
        else:
            tier = 1
        backup_n = info.backup_number if info.backup_number is not None else 0
        candidates.append((tier, backup_n, str(info.path), info))

    candidates.sort()
    if candidates:
        group.developed_path = candidates[0][3].path


def _select_backups(group: VariantGroup) -> None:
    """Populate backup_paths: for each backup N, prefer non-developed."""
    by_n: Dict[int, List[VariantInfo]] = {}
    for info in group.all_files:
        if info.backup_number is not None:
            by_n.setdefault(info.backup_number, []).append(info)

    for n, infos in by_n.items():
        # Prefer non-developed; tiebreak lexicographic
        infos.sort(key=lambda i: (i.is_developed, str(i.path)))
        group.backup_paths[n] = infos[0].path


def get_group_key_for_path(
    path: Path, variant_map: Dict[str, VariantGroup],
) -> Optional[str]:
    """Look up the casefolded group key for a file path."""
    group_key, _, _ = parse_variant_stem(path.stem)
    key_cf = group_key.casefold()
    if key_cf in variant_map:
        return key_cf
    return None


def build_badge_list(group: VariantGroup) -> List[Dict]:
    """Build ordered badge list for a variant group.

    Order: Main, D (if present), B, B2, B3... by N ascending.
    Each badge is a dict: {"label": str, "path": str, "kind": str}.
    """
    badges = []
    norm = norm_path

    if group.main_path is not None:
        badges.append({
            "label": "Main",
            "path": norm(group.main_path),
            "kind": "main",
        })

    if group.developed_path is not None and group.developed_path != group.main_path:
        badges.append({
            "label": "D",
            "path": norm(group.developed_path),
            "kind": "developed",
        })

    for n in sorted(group.backup_paths.keys()):
        bp = group.backup_paths[n]
        # Skip if this backup is already the main or developed path
        if bp == group.main_path or bp == group.developed_path:
            continue
        label = "Bk" if n == 1 else f"Bk{n}"
        badges.append({
            "label": label,
            "path": norm(bp),
            "kind": "backup",
        })

    return badges


def norm_path(p: Path) -> str:
    """Normalize a path for consistent comparison."""
    return os.path.normcase(os.path.abspath(str(p)))
