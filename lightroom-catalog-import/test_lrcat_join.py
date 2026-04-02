#!/usr/bin/env python3
"""
test_lrcat_join.py — Schema-discovery helper for Lightroom catalog path reconstruction

PURPOSE:
  Given a Lightroom Classic .lrcat catalog and an Adobe_images.id_local value,
  run the 4-table join that reconstructs the full file path and print all
  columns from the join result.

  This is the join chain we observed for path reconstruction:
    Adobe_images.rootFile -> AgLibraryFile.id_local
    AgLibraryFile.folder  -> AgLibraryFolder.id_local
    AgLibraryFolder.rootFolder -> AgLibraryRootFolder.id_local

  The full path is assembled as:
    AgLibraryRootFolder.absolutePath + AgLibraryFolder.pathFromRoot + baseName + "." + extension

  This is a schema-discovery/verification helper, NOT the main migration tool.
  Use green2faststack.py for the actual green-label migration workflow.

WHEN TO USE:
  - You want to verify that the 4-table join produces the correct file path
    for a specific image in your catalog.
  - You're investigating how Lightroom stores paths and want to see all
    the intermediate column values (rootFile, folder, rootFolder, etc.).
  - You want to confirm the join chain before trusting the export in
    green2faststack.py.

HOW TO FIND AN IMAGE ID:
  Open the .lrcat file in a SQLite browser and query Adobe_images, or use
  lrcat_diff.py to find rows that changed after a known edit.

EXAMPLES:
  python test_lrcat_join.py catalog.lrcat 12345
  python test_lrcat_join.py "Alan Rockefeller-v13-3.lrcat" 99

OBSERVED SCHEMA NOTES:
  - AgLibraryFile.baseName is the filename without extension.
  - AgLibraryFile.extension is the file extension without a leading dot.
  - AgLibraryFile.originalFilename includes the extension.
  - AgLibraryRootFolder.absolutePath typically ends with a trailing slash.
  - AgLibraryFolder.pathFromRoot typically ends with a trailing slash.
  - These observations are from specific Lightroom Classic catalogs and may
    vary by Lightroom version.

NOTES:
  - The catalog is opened read-only; no changes are made.
  - LEFT JOINs are used so partial results are shown even if the join chain
    is incomplete (e.g., missing folder or root folder records).
  - Requires only the Python standard library (sqlite3).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys


def connect_ro(path: str) -> sqlite3.Connection:
    """Open a read-only SQLite connection to the given file."""
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="test_lrcat_join",
        description=(
            "Test the 4-table join that reconstructs file paths from a Lightroom\n"
            "Classic .lrcat catalog.\n"
            "\n"
            "Joins: Adobe_images -> AgLibraryFile -> AgLibraryFolder -> AgLibraryRootFolder\n"
            "Prints all intermediate columns and the reconstructed full path.\n"
            "\n"
            "This is a schema-discovery helper. For the actual green-label\n"
            "migration, use green2faststack.py instead."
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
        help="The Adobe_images.id_local value to look up.",
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
        # The 4-table join chain for path reconstruction.
        # LEFT JOINs are used so we still see partial results if some tables
        # are missing rows (which would indicate an unusual catalog state).
        sql = """
        SELECT
            i.id_local AS image_id,
            i.colorLabels,
            i.rootFile,
            f.id_local AS file_id,
            f.baseName,
            f.originalFilename,
            f.extension,
            f.folder AS folder_id,
            d.id_local AS agfolder_id,
            d.pathFromRoot,
            d.rootFolder AS rootfolder_id,
            r.id_local AS root_id,
            r.absolutePath,
            r.relativePathFromCatalog
        FROM Adobe_images i
        LEFT JOIN AgLibraryFile f
            ON i.rootFile = f.id_local
        LEFT JOIN AgLibraryFolder d
            ON f.folder = d.id_local
        LEFT JOIN AgLibraryRootFolder r
            ON d.rootFolder = r.id_local
        WHERE i.id_local = ?
        """
        row = conn.execute(sql, (args.image_id,)).fetchone()
        if not row:
            print(f"No row found for Adobe_images.id_local = {args.image_id}.")
            print("Check that the image_id is correct. You can find valid IDs by")
            print("querying Adobe_images in a SQLite browser or using lrcat_diff.py.")
            return 1

        print("=== Join result ===")
        for k in row.keys():
            print(f"{k} = {row[k]!r}")

        # Reconstruct the full path from the join components.
        # absolutePath and pathFromRoot typically include trailing slashes.
        abs_root = row["absolutePath"] or ""
        path_from_root = row["pathFromRoot"] or ""
        base_name = row["baseName"] or row["originalFilename"] or ""
        extension = row["extension"] or ""

        # Append the extension if present.
        # AgLibraryFile.extension is stored without a leading dot.
        if extension:
            filename = f"{base_name}.{extension}"
        else:
            filename = base_name

        full_path = os.path.normpath(abs_root + path_from_root + filename)
        print(f"\nfull_path_guess = {full_path!r}")

        # Show what each component contributed, for debugging.
        print(f"\n  absolutePath    = {abs_root!r}")
        print(f"  pathFromRoot    = {path_from_root!r}")
        print(f"  baseName        = {base_name!r}")
        print(f"  extension       = {extension!r}")
        print(f"  -> filename     = {filename!r}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
