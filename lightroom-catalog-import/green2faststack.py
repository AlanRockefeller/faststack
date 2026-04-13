#!/usr/bin/env python3
"""
green2faststack.py — Migrate Lightroom Classic green labels into FastStack

PURPOSE:
  Export Lightroom Classic green-labeled image paths from a .lrcat catalog,
  and update FastStack faststack.json files using the exported paths.

  This is the main user-facing migration tool.

DESIGN GOALS:
  - Read the Lightroom catalog once and export all green-labeled paths to a text file.
  - Later, read that text file as many times as desired to update FastStack JSON files.
  - Match FastStack entries by exact lowercase stem.
  - Create new FastStack entries (and the JSON itself) for green-labeled files not yet
    tracked. Each green-labeled file gets its own entry under its own exact stem — a
    Lightroom export like "IMG_1234 Description.JPG" is a distinct file from the
    original "IMG_1234.ORF" and is tracked separately.
  - Propagate uploaded state to sibling originals: if an exported file's stem starts
    with a shorter-stemmed original's name (e.g. "img_1234 description" starts with
    "img_1234"), the original is also marked uploaded, because the processed version
    was derived from it and uploading the export means the original no longer needs
    to be worked on.
  - Be safe by default: dry-run support, automatic backups, verbose help, and summaries.

WORKFLOW:
  Step 1 — Export green-labeled paths from the catalog:
    python green2faststack.py -i catalog.lrcat -o green.txt

  Step 2 — Update a FastStack JSON from the exported paths:
    python green2faststack.py --paths green.txt --json /path/to/dir-or-faststack.json

  The --json argument accepts a faststack.json path or its parent directory.
  If no faststack.json exists, one is created. You can repeat step 2 for
  different directories as needed.

OBSERVED SCHEMA:
  The Lightroom catalog join chain for path reconstruction:
    Adobe_images.rootFile -> AgLibraryFile.id_local
    AgLibraryFile.folder  -> AgLibraryFolder.id_local
    AgLibraryFolder.rootFolder -> AgLibraryRootFolder.id_local

  Green labels are stored as Adobe_images.colorLabels = 'Green'.

  These observations are from specific Lightroom Classic catalogs. Other
  versions may differ. Use the helper scripts (inspect_lrcat_photo.py,
  lrcat_diff.py, test_lrcat_join.py) to verify against your own catalog.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

PROGRAM_NAME = "green2faststack"
DEFAULT_UPLOADED_DATE = "1970-01-01"


@dataclass(frozen=True)
class GreenPathRecord:
    """A single green-labeled image path extracted from the Lightroom catalog."""

    image_id: int
    full_path: str
    stem_key: str


class Logger:
    """Simple leveled logger that writes to stdout/stderr."""

    def __init__(self, verbose: bool = False, debug: bool = False) -> None:
        self.verbose_enabled = verbose or debug
        self.debug_enabled = debug

    def info(self, msg: str) -> None:
        print(msg)

    def verbose(self, msg: str) -> None:
        if self.verbose_enabled:
            print(msg)

    def debug(self, msg: str) -> None:
        if self.debug_enabled:
            print(f"[debug] {msg}")

    def warn(self, msg: str) -> None:
        print(f"[warn] {msg}", file=sys.stderr)

    def error(self, msg: str) -> None:
        print(f"[error] {msg}", file=sys.stderr)


def verbose_description() -> str:
    return f"""
{PROGRAM_NAME} bridges old Lightroom Classic "green label means uploaded"
workflows into FastStack's per-directory JSON tracking.

This tool has two modes:

1) Export mode
   Read a Lightroom Classic .lrcat catalog (a SQLite database), find all photos
   where Adobe_images.colorLabels = 'Green', reconstruct their full catalog paths,
   and write those paths to a plain text file, one path per line.

   Example:
     {PROGRAM_NAME} -i catalog.lrcat -o green.txt

2) JSON mode
   Read a previously exported text file of green-labeled paths and update a
   FastStack faststack.json file by matching entries on lowercase stem. Green-
   labeled files in the target directory that are not yet tracked in the JSON
   get new entries created with uploaded=True. If no faststack.json exists,
   one is created. This is intended for cases where Lightroom is no longer
   part of the workflow, and you want to apply the exported upload knowledge
   to FastStack as needed.

   Example:
     {PROGRAM_NAME} --paths green.txt --json /path/to/faststack.json

What this tool hopes to accomplish:
- Preserve historical "uploaded" decisions you made in Lightroom Classic.
- Let FastStack reflect those decisions without needing to reopen Lightroom.
- Work naturally with RAW/JPG pairs by matching same-stem entries.
- Handle Lightroom exports that have descriptions appended to the original
  filename (e.g. "IMG_1234 Trip Name stacked.JPG" derived from "IMG_1234.ORF").
  The export is tracked as its own FastStack entry, and the original is also
  marked uploaded since it no longer needs to be worked on.

Important limitations and behavior:
- Export mode reads the Lightroom catalog only; it does not require the image files
  to be mounted or present on disk.
- JSON mode reads only from the exported text file, not from the .lrcat.
- JSON mode updates one faststack.json at a time. If none exists, one is created.
- Matching is by exact lowercase stem. IMG_0001.ORF and IMG_0001.JPG both map to
  the same FastStack key "img_0001". But "IMG_0001 Description.JPG" is a different
  file with its own key "img_0001 description" — it gets its own FastStack entry.
- After processing green-listed files, the tool scans the target directory for
  sibling originals whose stem is a prefix of a green stem (separated by a space).
  Those originals are also marked uploaded, since the exported version was derived
  from them. This is upload-state propagation, not stem remapping.
- JSON mode sanity-checks whether the exported file paths currently exist on disk
  and includes those counts in the summary. These existence checks are best-effort
  and do not affect the stem-based matching.
- If an entry is already uploaded in faststack.json, it is left unchanged and
  counted as already present.
- Existing uploaded_date values are preserved. If a matching entry is newly marked
  uploaded and has no uploaded_date, the default date is {DEFAULT_UPLOADED_DATE}
  unless overridden with --uploaded-date YYYY-MM-DD.
- Before any JSON write, an automatic rotating backup is created:
    faststack.json.bak
    faststack.json.bak1
    faststack.json.bak2
    ...
- Use --dry-run to preview changes without writing anything.
- Use --verbose for normal detailed progress.
- Use --debug for very chatty troubleshooting output.

Cross-platform path resolution:
  Lightroom catalogs store Windows-style paths (e.g. C:/Users/...). When running
  in WSL or other environments, those stored paths may not exist at their original
  location. Both the file existence checks and the directory-matching logic convert
  Windows drive paths to /mnt/<drive>/... form (and vice versa) for comparison.

  The target directory for matching is determined by resolving the --json path's
  parent directory with Path.resolve(), which follows symlinks. For example, if
  ~/pictures is a symlink to /mnt/c/Users/alanr/Pictures, the resolved path is
  /mnt/c/Users/alanr/Pictures/..., which correctly matches green-list entries
  stored as C:/Users/alanr/Pictures/....

  Strategies tried for file existence checks:
    1. The path exactly as stored in the catalog.
    2. On WSL/Linux: if the path looks like a Windows drive letter (C:/... or C:\\...),
       try /mnt/c/... (lowercase drive letter).
    3. On Windows: if the path looks like /mnt/c/..., try C:/... instead.
  These are best-effort for the summary counts only. The actual stem-based matching
  into FastStack does NOT depend on file existence.

Examples:
  Export green paths from a catalog:
    {PROGRAM_NAME} -i "Alan Rockefeller-v13-3.lrcat" -o green.txt

  Update an existing FastStack JSON from an exported paths file:
    {PROGRAM_NAME} --paths green.txt --json /mnt/c/.../faststack.json

  Preview JSON changes without writing:
    {PROGRAM_NAME} --paths green.txt --json /mnt/c/.../faststack.json --dry-run --verbose

  Use a specific uploaded date for newly marked entries:
    {PROGRAM_NAME} --paths green.txt --json /mnt/c/.../faststack.json --uploaded-date 2026-04-01

Helper scripts:
  inspect_lrcat_photo.py — Inspect a single image record across all catalog tables.
  lrcat_diff.py          — Compare two catalog snapshots to discover schema changes.
  test_lrcat_join.py     — Test the 4-table path-reconstruction join for one image.
""".strip()


class RichHelpFormatter(argparse.RawDescriptionHelpFormatter):
    pass


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with comprehensive help text."""
    parser = argparse.ArgumentParser(
        prog=PROGRAM_NAME,
        description=verbose_description(),
        formatter_class=RichHelpFormatter,
    )
    parser.add_argument(
        "-i",
        "--input",
        help="Path to Lightroom Classic .lrcat file for export mode.",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output text file for export mode; one green-labeled path per line.",
    )
    parser.add_argument(
        "--paths",
        help="Previously exported text file of green-labeled paths for JSON mode.",
    )
    parser.add_argument(
        "--json",
        help="Path to a FastStack faststack.json (or its parent directory) to update. Created if missing.",
    )
    parser.add_argument(
        "--uploaded-date",
        default=DEFAULT_UPLOADED_DATE,
        help=f"Date to use for newly marked uploaded entries with no date (default: {DEFAULT_UPLOADED_DATE}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview JSON changes without writing any file.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print normal detailed progress information.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print very verbose debugging information.",
    )
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    """Validate argument combinations and return the selected mode ('export' or 'json')."""
    export_selected = bool(args.input or args.output)
    json_selected = bool(args.paths or args.json)

    if not export_selected and not json_selected:
        parser.print_help()
        raise SystemExit(1)

    if export_selected and json_selected:
        parser.error(
            "Choose either export mode (-i/-o) or JSON mode (--paths/--json), not both."
        )

    if export_selected:
        if not args.input or not args.output:
            parser.error("Export mode requires both -i/--input and -o/--output.")
        return "export"

    if not args.paths or not args.json:
        parser.error("JSON mode requires both --paths and --json.")
    return "json"


def connect_ro_sqlite(path: str) -> sqlite3.Connection:
    """Open a read-only SQLite connection to the given file."""
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_catalog_path(
    root: str, subdir: str, base_name: str, extension: str
) -> str:
    """Assemble a full path from the Lightroom join components.

    The components come from:
      root      = AgLibraryRootFolder.absolutePath  (typically ends with /)
      subdir    = AgLibraryFolder.pathFromRoot       (typically ends with /)
      base_name = AgLibraryFile.baseName             (no extension)
      extension = AgLibraryFile.extension            (no leading dot)
    """
    filename = f"{base_name}.{extension}" if extension else base_name
    return os.path.normpath((root or "") + (subdir or "") + filename)


def stem_key_from_path(path_str: str) -> str:
    """Extract the exact lowercase stem (filename without extension) as a FastStack key.

    Each distinct filename stem becomes its own FastStack entry. For example:
      IMG_0001.ORF            -> 'img_0001'
      IMG_0001.JPG            -> 'img_0001'       (same key — RAW/JPG pair)
      IMG_0001 Description.JPG -> 'img_0001 description'  (different key — separate file)

    This function does NOT strip descriptions or map exports back to original
    stems. Lightroom exports with appended text are distinct files that get
    their own entries. Upload-state propagation to related originals is handled
    separately by the sibling-matching logic in update_faststack_json().
    """
    return Path(path_str).stem.lower()


# ---------------------------------------------------------------------------
# Cross-platform path existence checking
# ---------------------------------------------------------------------------
#
# Lightroom catalogs store paths in the format they were added on the original
# OS — typically Windows paths like "C:/Users/alan/Photos/IMG_0001.ORF".
#
# When running this tool in different environments, those stored paths may not
# resolve directly:
#   - In WSL: C:/Users/... does not exist, but /mnt/c/Users/... does.
#   - On native Windows: paths should work as-is.
#   - On macOS/Linux: Windows paths will not exist unless the drive is mounted.
#
# The functions below try multiple path forms to give a best-effort existence
# check. This is used ONLY for the summary counts ("X files exist on disk").
# The actual stem-based matching into FastStack does NOT depend on whether the
# file is found on disk.
# ---------------------------------------------------------------------------

# Matches Windows drive-letter paths like "C:/..." or "C:\..."
_WINDOWS_DRIVE_RE = re.compile(r"^([A-Za-z]):[/\\]")

# Matches WSL mount paths like "/mnt/c/..."
_WSL_MOUNT_RE = re.compile(r"^/mnt/([a-z])/", re.IGNORECASE)


def _is_wsl() -> bool:
    """Detect whether we are running inside Windows Subsystem for Linux.

    Checks for 'microsoft' or 'WSL' in the kernel release string, which is
    the standard detection method. Returns False on non-Linux platforms.
    """
    if platform.system() != "Linux":
        return False
    try:
        release = platform.release().lower()
        return "microsoft" in release or "wsl" in release
    except Exception:
        return False


# Cache the WSL check at module load time so we don't re-check per path.
_RUNNING_IN_WSL = _is_wsl()


def check_file_exists(path_str: str, logger: Logger) -> bool:
    """Best-effort check whether a file exists, trying cross-platform path forms.

    Strategy:
      1. Try the path exactly as given.
      2. If it looks like a Windows drive path (C:/...) and we're on WSL/Linux,
         normalize slashes and try /mnt/<drive>/... (lowercase drive letter).
      3. If it looks like a WSL /mnt/<drive>/... path and we're on Windows,
         try <DRIVE>:/... instead.

    Returns True if the file is found via any of these strategies.
    """
    # Normalize forward/back slashes for consistent matching.
    normalized = path_str.replace("\\", "/")

    # Strategy 1: try the path exactly as stored (after slash normalization).
    if os.path.exists(normalized):
        logger.debug(f"Exists (direct): {normalized}")
        return True

    # Strategy 2: Windows drive path -> WSL mount path.
    # e.g. "C:/Users/alan/file.jpg" -> "/mnt/c/Users/alan/file.jpg"
    drive_match = _WINDOWS_DRIVE_RE.match(normalized)
    if drive_match and (platform.system() == "Linux"):
        drive_letter = drive_match.group(1).lower()
        rest = normalized[2:]  # strip "C:" prefix, keep the leading "/"
        wsl_path = f"/mnt/{drive_letter}{rest}"
        if os.path.exists(wsl_path):
            logger.debug(f"Exists (WSL mount): {wsl_path}")
            return True

    # Strategy 3: WSL mount path -> Windows drive path.
    # e.g. "/mnt/c/Users/alan/file.jpg" -> "C:/Users/alan/file.jpg"
    wsl_match = _WSL_MOUNT_RE.match(normalized)
    if wsl_match and (platform.system() == "Windows" or os.name == "nt"):
        drive_letter = wsl_match.group(1).upper()
        rest = normalized[len(wsl_match.group(0)) - 1 :]  # keep leading "/"
        win_path = f"{drive_letter}:{rest}"
        if os.path.exists(win_path):
            logger.debug(f"Exists (Windows drive): {win_path}")
            return True

    logger.debug(f"Not found on disk: {path_str}")
    return False


# ---------------------------------------------------------------------------
# Export mode
# ---------------------------------------------------------------------------


def export_green_paths(catalog_path: str, output_path: str, logger: Logger) -> int:
    """Export all green-labeled image paths from a Lightroom catalog to a text file.

    Returns the number of paths written.
    """
    if not os.path.exists(catalog_path):
        raise FileNotFoundError(f"Catalog not found: {catalog_path}")

    logger.verbose(f"Opening Lightroom catalog read-only: {catalog_path}")
    conn = connect_ro_sqlite(catalog_path)
    try:
        # This query uses the observed join chain:
        #   Adobe_images.rootFile -> AgLibraryFile.id_local
        #   AgLibraryFile.folder  -> AgLibraryFolder.id_local
        #   AgLibraryFolder.rootFolder -> AgLibraryRootFolder.id_local
        # and filters for colorLabels = 'Green' (observed storage for green labels).
        sql = """
        SELECT
            i.id_local AS image_id,
            r.absolutePath,
            d.pathFromRoot,
            f.baseName,
            f.extension
        FROM Adobe_images i
        JOIN AgLibraryFile f
            ON i.rootFile = f.id_local
        JOIN AgLibraryFolder d
            ON f.folder = d.id_local
        JOIN AgLibraryRootFolder r
            ON d.rootFolder = r.id_local
        WHERE i.colorLabels = 'Green'
        ORDER BY r.absolutePath, d.pathFromRoot, f.baseName
        """
        rows = conn.execute(sql).fetchall()
        logger.verbose(f"Found {len(rows)} Lightroom rows where colorLabels = 'Green'.")

        output_records: list[GreenPathRecord] = []
        for row in rows:
            full_path = normalize_catalog_path(
                row["absolutePath"] or "",
                row["pathFromRoot"] or "",
                row["baseName"] or "",
                row["extension"] or "",
            )
            record = GreenPathRecord(
                image_id=int(row["image_id"]),
                full_path=full_path,
                stem_key=stem_key_from_path(full_path),
            )
            output_records.append(record)
            logger.debug(
                f"Export row image_id={record.image_id} path={record.full_path}"
            )

        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8", newline="\n") as handle:
            for record in output_records:
                handle.write(record.full_path)
                handle.write("\n")

        logger.info(f"Wrote {len(output_records)} green-labeled paths to {out_path}")
        return len(output_records)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# JSON mode
# ---------------------------------------------------------------------------


@dataclass
class JsonUpdateSummary:
    """Tracks counts and outcomes for a JSON update operation."""

    paths_read: int = 0
    unique_stems_in_paths: int = 0
    existing_files: int = 0
    missing_files: int = 0
    existence_check_note: str = ""
    green_in_this_dir: int = 0
    json_entries_total: int = 0
    matching_entries_found: int = 0
    newly_marked_uploaded: int = 0
    already_uploaded: int = 0
    newly_created_entries: int = 0
    sibling_originals_marked: int = 0
    json_created: bool = False
    backup_path: str | None = None
    json_written: bool = False


# Default shape for FastStack entries. Used to fill in missing fields when
# updating entries that may have been created by an older FastStack version.
DEFAULT_FASTSTACK_ENTRY_SHAPE = {
    "stack_id": None,
    "stacked": False,
    "stacked_date": None,
    "uploaded": False,
    "uploaded_date": None,
    "edited": False,
    "edited_date": None,
    "restacked": False,
    "restacked_date": None,
    "favorite": False,
    "todo": False,
    "todo_date": None,
}


def load_paths_file(paths_path: str, logger: Logger) -> list[str]:
    """Load non-empty lines from a previously exported paths text file."""
    if not os.path.exists(paths_path):
        raise FileNotFoundError(f"Paths file not found: {paths_path}")

    result: list[str] = []
    with open(paths_path, "r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                logger.debug(f"Skipping empty line {line_number} in paths file.")
                continue
            result.append(line)
    logger.verbose(f"Loaded {len(result)} non-empty paths from {paths_path}")
    return result


def next_backup_path(json_path: Path) -> Path:
    """Find the next available backup filename (faststack.json.bak, .bak1, .bak2, ...)."""
    first = json_path.with_name(json_path.name + ".bak")
    if not first.exists():
        return first

    MAX_BACKUP_ATTEMPTS = 1000
    index = 1
    while index <= MAX_BACKUP_ATTEMPTS:
        candidate = json_path.with_name(json_path.name + f".bak{index}")
        if not candidate.exists():
            return candidate
        index += 1
    raise RuntimeError(
        f"Could not find an available backup path for {json_path.name} "
        f"within {MAX_BACKUP_ATTEMPTS} attempts."
    )


def ensure_faststack_entry_shape(entry: dict) -> dict:
    """Fill in any missing fields with defaults. Does not overwrite existing values."""
    for key, value in DEFAULT_FASTSTACK_ENTRY_SHAPE.items():
        entry.setdefault(key, value)
    return entry


def load_json(path: Path) -> dict:
    """Load and return the parsed JSON from the given file."""
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, data: dict) -> None:
    """Atomically write JSON data to the given path (write to .tmp, then rename)."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    tmp_path.replace(path)


def human_summary(summary: JsonUpdateSummary, json_path: str) -> str:
    """Format the update summary as a human-readable string."""
    lines = [
        f"Summary for {json_path}",
        f"  Paths read from green list: {summary.paths_read}",
        f"  Unique stems in green list: {summary.unique_stems_in_paths}",
        f"  Green-labeled paths in this directory: {summary.green_in_this_dir}",
        f"  Exported image paths that currently exist on disk: {summary.existing_files}",
        f"  Exported image paths missing on disk: {summary.missing_files}",
    ]
    if summary.existence_check_note:
        lines.append(f"  Note: {summary.existence_check_note}")
    if summary.json_created:
        lines.append("  FastStack JSON created (was missing)")
    lines.extend(
        [
            f"  FastStack entries in JSON before update: {summary.json_entries_total}",
            f"  Existing entries matched to green list: {summary.matching_entries_found}",
            f"  Newly marked uploaded (existing entries): {summary.newly_marked_uploaded}",
            f"  Already uploaded: {summary.already_uploaded}",
            f"  New entries created and marked uploaded: {summary.newly_created_entries}",
            f"  Sibling originals also marked uploaded: {summary.sibling_originals_marked}",
        ]
    )
    if summary.backup_path:
        lines.append(f"  Backup created: {summary.backup_path}")
    if summary.json_written:
        lines.append("  JSON file written: yes")
    else:
        lines.append("  JSON file written: no")
    return "\n".join(lines)


def _normalize_dir_for_comparison(path_str: str) -> str:
    """Normalize a directory path to lowercase /mnt/<drive>/... form for comparison.

    The caller is expected to pass a resolved path (symlinks followed via
    Path.resolve()). For example, if ~/pictures -> /mnt/c/Users/alanr/Pictures,
    the caller passes the resolved /mnt/c/Users/alanr/Pictures/... form.

    This function then handles the remaining conversion: Windows drive paths
    (C:/...) become /mnt/c/..., and everything is lowercased. The result can
    be compared against green-list paths normalized by _path_is_in_dir().

    Returns the normalized path with trailing slash.
    """
    normalized = path_str.replace("\\", "/").rstrip("/") + "/"
    # Convert Windows drive paths to /mnt/ style for comparison.
    drive_match = _WINDOWS_DRIVE_RE.match(normalized)
    if drive_match:
        drive_letter = drive_match.group(1).lower()
        rest = normalized[2:]  # strip "C:", keep leading "/"
        normalized = f"/mnt/{drive_letter}{rest}"
    return normalized.lower()


def _path_is_in_dir(path_str: str, dir_normalized: str) -> bool:
    """Check if a green-list path belongs to the target directory.

    Normalizes the green-list path the same way _normalize_dir_for_comparison
    normalizes the target: Windows C:/... becomes /mnt/c/..., then lowercase.
    Compares the file's parent directory against dir_normalized.
    """
    normalized = path_str.replace("\\", "/").lower()
    drive_match = _WINDOWS_DRIVE_RE.match(normalized)
    if drive_match:
        drive_letter = drive_match.group(1).lower()
        rest = normalized[2:]
        normalized = f"/mnt/{drive_letter}{rest}"
    # Check that the file's parent directory matches.
    parent = normalized.rsplit("/", 1)[0] + "/"
    return parent == dir_normalized


EMPTY_FASTSTACK_JSON = {
    "version": 2,
    "last_index": 0,
    "entries": {},
    "stacks": [],
}


def update_faststack_json(
    paths_file: str,
    json_path_str: str,
    uploaded_date: str,
    dry_run: bool,
    logger: Logger,
) -> JsonUpdateSummary:
    """Update a FastStack JSON file based on a previously exported paths file.

    Each green-labeled file in the target directory is matched or created as
    its own FastStack entry under its exact lowercase stem. A Lightroom export
    like "IMG_1234 Description.JPG" is a distinct file from the original
    "IMG_1234.ORF" and gets its own entry — this is not stem remapping.

    After processing green-listed files, the tool propagates uploaded state to
    sibling originals: if a file in the target directory has a stem that is a
    prefix of a green stem (e.g. "img_1234" is a prefix of "img_1234 description"),
    that original is also marked uploaded, because the processed/exported version
    was derived from it and the original no longer needs to be worked on.

    Creates the JSON file if it does not exist.
    """
    json_path = Path(json_path_str)
    if json_path.is_dir():
        json_path = json_path / "faststack.json"

    path_lines = load_paths_file(paths_file, logger)
    summary = JsonUpdateSummary(paths_read=len(path_lines))

    # Build a mapping from lowercase stem to the list of catalog paths that
    # share that stem. Multiple paths can share a stem (e.g. RAW + JPG pairs).
    stem_to_paths: dict[str, list[str]] = {}
    for path_str in path_lines:
        stem = stem_key_from_path(path_str)
        stem_to_paths.setdefault(stem, []).append(path_str)

        # Best-effort existence check using cross-platform path resolution.
        if check_file_exists(path_str, logger):
            summary.existing_files += 1
        else:
            summary.missing_files += 1

    summary.unique_stems_in_paths = len(stem_to_paths)

    if summary.missing_files > 0 and _RUNNING_IN_WSL:
        summary.existence_check_note = (
            "Existence checks tried both catalog paths and WSL /mnt/ paths. "
            "Missing files may be on unmounted drives or external storage."
        )
    elif summary.missing_files > 0:
        summary.existence_check_note = (
            "Some exported paths were not found on disk. This is expected if "
            "files are on unmounted drives, external storage, or a different OS."
        )

    # Filter green paths to only those in the target directory.
    # resolve() follows symlinks, so ~/pictures -> /mnt/c/Users/alanr/Pictures
    # yields the real /mnt/c/... path, which can then be compared against
    # green-list entries stored as C:/Users/alanr/Pictures/... after both
    # sides are normalized to /mnt/<drive>/... form.
    target_dir = json_path.parent.resolve()
    target_dir_normalized = _normalize_dir_for_comparison(str(target_dir))
    logger.debug(f"Target directory (normalized): {target_dir_normalized}")

    dir_stems: dict[str, list[str]] = {}
    for path_str in path_lines:
        if _path_is_in_dir(path_str, target_dir_normalized):
            stem = stem_key_from_path(path_str)
            dir_stems.setdefault(stem, []).append(path_str)

    summary.green_in_this_dir = len(dir_stems)
    logger.verbose(
        f"Green-labeled paths in target directory: {len(dir_stems)} unique stems"
    )

    # Load or create the JSON.
    if json_path.exists():
        logger.verbose(f"Opening FastStack JSON: {json_path}")
        if json_path_str != str(json_path):
            logger.verbose(f"  (resolved directory to {json_path})")
        data = load_json(json_path)
    else:
        logger.verbose(f"FastStack JSON not found, will create: {json_path}")
        data = json.loads(json.dumps(EMPTY_FASTSTACK_JSON))  # deep copy
        summary.json_created = True

    entries = data.get("entries")
    if not isinstance(entries, dict):
        raise ValueError(f"FastStack JSON missing an 'entries' dictionary: {json_path}")

    summary.json_entries_total = len(entries)
    changed = False

    # Process green stems that are in this directory. Each green-labeled file
    # gets its own entry under its exact stem — an export with an appended
    # description is a separate file and a separate entry from the original.
    for stem in sorted(dir_stems.keys()):
        source_paths = dir_stems[stem]

        if stem in entries:
            # Existing entry — mark uploaded if not already.
            summary.matching_entries_found += 1
            entry = entries[stem]
            if not isinstance(entry, dict):
                logger.warn(
                    f"Skipping malformed FastStack entry for stem {stem!r}: not an object"
                )
                continue

            ensure_faststack_entry_shape(entry)
            logger.debug(f"Existing entry {stem!r}, source paths: {source_paths}")

            if entry.get("uploaded") is True:
                summary.already_uploaded += 1
                logger.verbose(f"Already uploaded: {stem}")
                continue

            entry["uploaded"] = True
            if not entry.get("uploaded_date"):
                entry["uploaded_date"] = uploaded_date
            summary.newly_marked_uploaded += 1
            changed = True
            logger.verbose(f"Marking uploaded: {stem}")
        else:
            # New entry — create it with uploaded=True.
            new_entry = dict(DEFAULT_FASTSTACK_ENTRY_SHAPE)
            new_entry["uploaded"] = True
            new_entry["uploaded_date"] = uploaded_date
            entries[stem] = new_entry
            summary.newly_created_entries += 1
            changed = True
            logger.verbose(f"Creating new entry: {stem}")

    # Propagate uploaded state to sibling originals. A green-labeled export
    # like "20250705-P7058257 Costa Rica Amal stacked.JPG" is a processed
    # version of the original capture "20250705-P7058257.ORF" (or .JPG).
    # Both files may coexist in the same directory as distinct files with
    # distinct stems. The export already got its own entry above. Here we
    # also mark the shorter-stemmed original as uploaded, because once the
    # processed version has been uploaded the original capture no longer
    # needs to be worked on again.
    #
    # Detection: scan the target directory for image files whose stem is a
    # prefix of a green stem followed by a space. The space delimiter avoids
    # false matches between unrelated files that happen to share a prefix.
    IMAGE_EXTENSIONS = {
        ".jpg",
        ".jpeg",
        ".png",
        ".tif",
        ".tiff",
        ".bmp",
        ".orf",
        ".cr2",
        ".cr3",
        ".nef",
        ".arw",
        ".dng",
        ".raf",
        ".rw2",
        ".pef",
        ".srw",
        ".x3f",
    }
    dir_file_stems: set[str] = set()
    if target_dir.is_dir():
        for child in target_dir.iterdir():
            if child.is_file() and child.suffix.lower() in IMAGE_EXTENSIONS:
                dir_file_stems.add(child.stem.lower())

    green_stem_set = set(dir_stems.keys())
    for file_stem in sorted(dir_file_stems):
        # Check if any green stem starts with this file stem + space.
        # e.g. file_stem "20250705-p7058257" matches green stem
        # "20250705-p7058257 costa rica amal stacked"
        is_original_of_green = any(
            gs.startswith(file_stem + " ") for gs in green_stem_set
        )
        if not is_original_of_green:
            continue
        if file_stem in green_stem_set:
            # Already handled directly as a green stem.
            continue

        if file_stem in entries:
            entry = entries[file_stem]
            if not isinstance(entry, dict):
                continue
            ensure_faststack_entry_shape(entry)
            if entry.get("uploaded") is True:
                continue
            entry["uploaded"] = True
            if not entry.get("uploaded_date"):
                entry["uploaded_date"] = uploaded_date
            summary.sibling_originals_marked += 1
            changed = True
            logger.verbose(f"Marking sibling original uploaded: {file_stem}")
        else:
            new_entry = dict(DEFAULT_FASTSTACK_ENTRY_SHAPE)
            new_entry["uploaded"] = True
            new_entry["uploaded_date"] = uploaded_date
            entries[file_stem] = new_entry
            summary.sibling_originals_marked += 1
            changed = True
            logger.verbose(f"Creating sibling original entry: {file_stem}")

    should_write_json = changed or summary.json_created

    if should_write_json and not dry_run:
        if json_path.exists():
            backup_path = next_backup_path(json_path)
            shutil.copy2(json_path, backup_path)
            summary.backup_path = str(backup_path)
        save_json(json_path, data)
        summary.json_written = True
    elif should_write_json and dry_run:
        logger.info("[dry-run] JSON would be created or updated; file was not written.")
    else:
        logger.info("No JSON changes were needed.")

    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    mode = validate_args(parser, args)
    logger = Logger(verbose=args.verbose, debug=args.debug)

    try:
        if mode == "export":
            count = export_green_paths(args.input, args.output, logger)
            logger.info(f"Export complete. {count} paths written.")
            return 0

        summary = update_faststack_json(
            paths_file=args.paths,
            json_path_str=args.json,
            uploaded_date=args.uploaded_date,
            dry_run=args.dry_run,
            logger=logger,
        )
        logger.info("")
        logger.info(human_summary(summary, args.json))
        return 0
    except (
        FileNotFoundError,
        ValueError,
        sqlite3.DatabaseError,
        json.JSONDecodeError,
    ) as exc:
        logger.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
