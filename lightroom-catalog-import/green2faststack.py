#!/usr/bin/env python3
"""
green2faststack.py — Migrate Lightroom Classic green labels into FastStack

PURPOSE:
  Export Lightroom Classic green-labeled image paths from a .lrcat catalog,
  and optionally update an existing FastStack faststack.json using the exported
  paths file.

  This is the main user-facing migration tool.

DESIGN GOALS:
  - Read the Lightroom catalog once and export all green-labeled paths to a text file.
  - Later, read that text file as many times as desired to update FastStack JSON files.
  - Match FastStack entries by lowercase stem only, so RAW/JPG pairs naturally align.
  - Never create new JSON files implicitly.
  - Be safe by default: dry-run support, automatic backups, verbose help, and summaries.

WORKFLOW:
  Step 1 — Export green-labeled paths from the catalog:
    python green2faststack.py -i catalog.lrcat -o green.txt

  Step 2 — Update an existing FastStack JSON from the exported paths:
    python green2faststack.py --paths green.txt --json /path/to/faststack.json

  You can repeat step 2 for different faststack.json files as needed.

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
   Read a previously exported text file of green-labeled paths and update one
   existing FastStack faststack.json file by matching entries on lowercase stem.
   This is intended for cases where Lightroom is no longer part of the workflow,
   and you want to apply the exported upload knowledge to FastStack as needed.

   Example:
     {PROGRAM_NAME} --paths green.txt --json /path/to/faststack.json

What this tool hopes to accomplish:
- Preserve historical "uploaded" decisions you made in Lightroom Classic.
- Let FastStack reflect those decisions without needing to reopen Lightroom.
- Work naturally with RAW/JPG pairs by matching same-stem entries.

Important limitations and behavior:
- Export mode reads the Lightroom catalog only; it does not require the image files
  to be mounted or present on disk.
- JSON mode reads only from the exported text file, not from the .lrcat.
- JSON mode updates only one existing faststack.json at a time.
- This tool does not create a new faststack.json. The JSON must already exist.
- Matching is stem-based only. For example, IMG_0001.ORF and IMG_0001.JPG both map
  to the same FastStack key img_0001 if that is how FastStack tracks the entry.
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
  in WSL or other environments, these paths may not exist at their stored location.
  The existence check tries several strategies:
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
        help="Path to an existing FastStack faststack.json to update in JSON mode.",
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
    """Extract the lowercase stem (filename without extension) for matching.

    FastStack uses lowercase stems as entry keys, so IMG_0001.ORF and
    IMG_0001.JPG both map to 'img_0001'.
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
    json_entries_total: int = 0
    matching_entries_found: int = 0
    newly_marked_uploaded: int = 0
    already_uploaded: int = 0
    stems_not_present_in_json: int = 0
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
        f"  Paths read: {summary.paths_read}",
        f"  Unique stems in paths file: {summary.unique_stems_in_paths}",
        f"  Exported image paths that currently exist on disk: {summary.existing_files}",
        f"  Exported image paths missing on disk: {summary.missing_files}",
    ]
    if summary.existence_check_note:
        lines.append(f"  Note: {summary.existence_check_note}")
    lines.extend(
        [
            f"  FastStack entries present in JSON: {summary.json_entries_total}",
            f"  Matching FastStack entries found: {summary.matching_entries_found}",
            f"  Newly marked uploaded: {summary.newly_marked_uploaded}",
            f"  Already uploaded: {summary.already_uploaded}",
            f"  Exported stems not present in this JSON: {summary.stems_not_present_in_json}",
        ]
    )
    if summary.backup_path:
        lines.append(f"  Backup created: {summary.backup_path}")
    if summary.json_written:
        lines.append("  JSON file written: yes")
    else:
        lines.append("  JSON file written: no")
    return "\n".join(lines)


def update_faststack_json(
    paths_file: str,
    json_path_str: str,
    uploaded_date: str,
    dry_run: bool,
    logger: Logger,
) -> JsonUpdateSummary:
    """Update a FastStack JSON file based on a previously exported paths file.

    Matches exported paths to FastStack entries by lowercase stem. Sets
    uploaded=True on matching entries that are not already marked.
    """
    json_path = Path(json_path_str)
    if not json_path.exists():
        raise FileNotFoundError(
            f"FastStack JSON not found: {json_path}. This tool does not create new JSON files."
        )

    path_lines = load_paths_file(paths_file, logger)
    summary = JsonUpdateSummary(paths_read=len(path_lines))

    # Build a mapping from lowercase stem to the list of catalog paths that
    # share that stem. Multiple paths can share a stem (e.g. RAW + JPG pairs).
    stem_to_paths: dict[str, list[str]] = {}
    for path_str in path_lines:
        stem = stem_key_from_path(path_str)
        stem_to_paths.setdefault(stem, []).append(path_str)

        # Best-effort existence check using cross-platform path resolution.
        # This is for informational summary only — matching is stem-based
        # and does not depend on whether the file is found on disk.
        if check_file_exists(path_str, logger):
            summary.existing_files += 1
        else:
            summary.missing_files += 1

    summary.unique_stems_in_paths = len(stem_to_paths)

    # Add a note explaining the existence check strategy.
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

    logger.verbose(f"Opening FastStack JSON: {json_path}")
    data = load_json(json_path)
    entries = data.get("entries")
    if not isinstance(entries, dict):
        raise ValueError(f"FastStack JSON missing an 'entries' dictionary: {json_path}")

    summary.json_entries_total = len(entries)
    changed = False

    json_stems = set(entries.keys())
    exported_stems = set(stem_to_paths.keys())
    summary.stems_not_present_in_json = len(exported_stems - json_stems)

    for stem in sorted(exported_stems & json_stems):
        summary.matching_entries_found += 1
        entry = entries[stem]
        if not isinstance(entry, dict):
            logger.warn(
                f"Skipping malformed FastStack entry for stem {stem!r}: not an object"
            )
            continue

        ensure_faststack_entry_shape(entry)
        source_paths = stem_to_paths[stem]
        logger.debug(f"Matching stem {stem!r} with source paths: {source_paths}")

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

    if changed and not dry_run:
        backup_path = next_backup_path(json_path)
        shutil.copy2(json_path, backup_path)
        summary.backup_path = str(backup_path)
        save_json(json_path, data)
        summary.json_written = True
    elif changed and dry_run:
        logger.info("[dry-run] Changes detected; JSON was not written.")
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
