#!/usr/bin/env python3
"""
inspect_lrcat_photo.py — Exploratory helper for reverse-engineering Lightroom catalogs

PURPOSE:
  Given a Lightroom Classic .lrcat catalog and an Adobe_images.id_local value,
  print the Adobe_images row and scan all tables for columns that might reference
  that image ID. Also lists tables containing path-related columns.

  This is a reverse-engineering/exploration helper, NOT the main migration tool.
  Use green2faststack.py for the actual green-label migration workflow.

WHEN TO USE:
  - You want to understand the schema of a specific Lightroom catalog.
  - You want to trace how a specific image is referenced across tables.
  - You're investigating which tables/columns store file paths, folder
    structures, or other metadata for a given photo.

HOW TO FIND AN IMAGE ID:
  Open the .lrcat file in a SQLite browser (e.g. DB Browser for SQLite) and
  query Adobe_images. Or use lrcat_diff.py to find rows that changed after
  a known edit.

EXAMPLES:
  python inspect_lrcat_photo.py catalog.lrcat 12345
  python inspect_lrcat_photo.py "Alan Rockefeller-v13-3.lrcat" 99

WHAT IT DOES:
  1. Prints all columns from the Adobe_images row for the given id_local.
  2. Scans every table in the catalog for columns whose names suggest they
     might be foreign keys pointing to an image (e.g. "image", "rootFile").
     For each such column, queries for rows matching the given image ID.
     This is a heuristic scan — it may produce false positives or miss
     columns with unusual naming.
  3. Lists all tables that have columns with path-related names (path,
     filename, folder, volume, root, etc.) to help identify where file
     locations are stored.

NOTES:
  - The catalog is opened read-only; no changes are made.
  - The candidate-reference scan checks column names heuristically.
    Lightroom's schema is not publicly documented, so column naming
    conventions were inferred from observation and may vary by version.
  - Lightroom Classic catalogs are SQLite databases. This script requires
    only the Python standard library (sqlite3).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys


def quote_ident(name: str) -> str:
    """Quote a SQL identifier to prevent injection. Doubles internal quotes."""
    return '"' + name.replace('"', '""') + '"'


def connect_ro(path: str) -> sqlite3.Connection:
    """Open a read-only SQLite connection to the given file."""
    abs_path = os.path.abspath(path)
    uri = f"file:{abs_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_tables(conn: sqlite3.Connection) -> list[str]:
    """Return all user table names in the database, sorted."""
    rows = conn.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type='table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
    """).fetchall()
    return [row["name"] for row in rows]


def get_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return column names for the given table."""
    rows = conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()
    return [row["name"] for row in rows]


def print_row(title: str, row: sqlite3.Row | None) -> None:
    """Pretty-print a single database row with a section title."""
    print(f"\n=== {title} ===")
    if row is None:
        print("  <none>")
        return
    for key in row.keys():
        print(f"{key} = {row[key]!r}")


# Column names (lowercased) that might be foreign keys pointing to an image.
# These were observed in Lightroom Classic catalogs; other versions may differ.
CANDIDATE_IMAGE_COLUMN_NAMES = {
    "image",
    "imageid",
    "id_image",
    "image_id",
    "rootfile",
    "rootfileid",
    "id_rootfile",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="inspect_lrcat_photo",
        description=(
            "Inspect a single image record in a Lightroom Classic .lrcat catalog.\n"
            "\n"
            "Prints the Adobe_images row for the given id_local, scans all tables\n"
            "for columns that might reference this image, and lists tables with\n"
            "path-related columns.\n"
            "\n"
            "This is an exploratory/reverse-engineering helper. For the actual\n"
            "green-label migration, use green2faststack.py instead."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  %(prog)s catalog.lrcat 12345\n"
            '  %(prog)s "Alan Rockefeller-v13-3.lrcat" 99\n'
            "\n"
            "The catalog is opened read-only; no changes are made.\n"
            "Requires only the Python standard library."
        ),
    )
    parser.add_argument(
        "catalog",
        help="Path to a Lightroom Classic .lrcat file (a SQLite database).",
    )
    parser.add_argument(
        "image_id",
        type=int,
        help="The Adobe_images.id_local value to inspect.",
    )

    if len(sys.argv) == 1:
        parser.print_help()
        return 1

    args = parser.parse_args()

    if not os.path.exists(args.catalog):
        print(f"error: catalog not found: {args.catalog}", file=sys.stderr)
        return 2

    conn = connect_ro(args.catalog)
    try:
        # Step 1: Print the Adobe_images row for this id_local.
        row = conn.execute(
            "SELECT * FROM Adobe_images WHERE id_local = ?",
            (args.image_id,),
        ).fetchone()
        print_row("Adobe_images", row)

        if row is None:
            print(f"\nNo Adobe_images row found for id_local = {args.image_id}.")
            print("Check that the image_id is correct. You can find valid IDs by")
            print("querying Adobe_images in a SQLite browser or using lrcat_diff.py.")
            return 1

        image_id = args.image_id
        root_file_id = row["rootFile"]
        tables = get_tables(conn)

        # Step 2: Scan all tables for columns that might reference this image.
        # This is a heuristic: we look for column names that suggest foreign keys
        # to image-related tables (e.g. "image", "rootFile", etc.).
        # This can produce false positives if unrelated columns happen to have
        # matching names or values.
        candidates = []
        for table in tables:
            cols = get_columns(conn, table)
            matching_cols = [
                col for col in cols if col.lower() in CANDIDATE_IMAGE_COLUMN_NAMES
            ]
            if matching_cols:
                candidates.append((table, matching_cols))

        print("\n=== Candidate references to this image ===")
        print("(Scanning tables for columns whose names suggest image foreign keys.)")
        print("(This is heuristic — column naming may vary by Lightroom version.)")
        found_any = False
        for table, cols in candidates:
            for col in cols:
                # If column name specifically suggests it's a "rootFile" foreign key,
                # use the library-file ID instead of the image ID.
                probe_id = root_file_id if "rootfile" in col.lower() else image_id
                sql = f"SELECT * FROM {quote_ident(table)} WHERE {quote_ident(col)} = ? LIMIT 5"
                rows = conn.execute(sql, (probe_id,)).fetchall()
                if rows:
                    found_any = True
                    print(f"\n--- {table}.{col} ---")
                    for i, r in enumerate(rows, 1):
                        print(f"[row {i}]")
                        for key in r.keys():
                            print(f"{key} = {r[key]!r}")

        if not found_any:
            print("No obvious direct references found.")

        # Step 3: List tables that have columns with path-related names.
        # This helps identify where Lightroom stores file locations.
        print("\n=== Tables with likely path columns ===")
        print("(Tables containing columns named like path, filename, folder, etc.)")
        for table in tables:
            cols = get_columns(conn, table)
            interesting = [
                c
                for c in cols
                if any(
                    x in c.lower()
                    for x in [
                        "path",
                        "filename",
                        "basename",
                        "folder",
                        "volume",
                        "root",
                    ]
                )
            ]
            if interesting:
                print(f"{table}: {', '.join(interesting)}")

        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
