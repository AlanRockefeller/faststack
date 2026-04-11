#!/usr/bin/env python3
"""
lrcat_diff.py — Reverse-engineering helper for discovering Lightroom catalog changes

PURPOSE:
  Compare two Lightroom Classic .lrcat files (before and after a known edit)
  and report which rows and columns changed. This is how we discovered that
  green labels are stored in Adobe_images.colorLabels = 'Green'.

  This is a reverse-engineering/exploration helper, NOT the main migration tool.
  Use green2faststack.py for the actual green-label migration workflow.

WHEN TO USE:
  - You want to discover which table/column stores a specific piece of metadata
    (color labels, ratings, keywords, develop settings, etc.).
  - You want to verify that a known Lightroom edit changed what you expected.
  - You want to understand the Lightroom catalog schema by observing changes.

TYPICAL WORKFLOW:
  1. Close Lightroom Classic.
  2. Copy the catalog file to a backup:
       cp "My Catalog.lrcat" before.lrcat
  3. Open Lightroom, make one small known change (e.g., mark one photo green),
     then close Lightroom.
  4. Run the diff:
       python lrcat_diff.py before.lrcat "My Catalog.lrcat" --match "IMG_1234"

  The --match flag filters output to rows containing the given substring,
  which is very helpful for isolating the specific photo you changed.

EXAMPLES:
  # Compare two catalogs, show all changes
  python lrcat_diff.py before.lrcat after.lrcat

  # Show only changes related to a specific filename
  python lrcat_diff.py before.lrcat after.lrcat --match "DSC_0042"

  # Compare only specific tables
  python lrcat_diff.py before.lrcat after.lrcat --tables Adobe_images AgLibraryFile

  # Increase the per-table row limit
  python lrcat_diff.py before.lrcat after.lrcat --max-rows 100

MEMORY NOTE:
  This tool loads all rows for each compared table into memory (keyed by
  primary key) to compute diffs. For very large catalogs with hundreds of
  thousands of photos, this may use significant memory. If you hit memory
  limits, use --tables to compare specific tables one at a time.

NOTES:
  - Both catalogs are opened read-only; no changes are made.
  - Tables without a primary key are skipped (cannot reliably match rows).
  - BLOB values are shown as their SHA-1 hash and byte length.
  - Lightroom Classic catalogs are SQLite databases. This script requires
    only the Python standard library (sqlite3).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from typing import Iterable, Sequence


def quote_ident(name: str) -> str:
    """Quote a SQL identifier to prevent injection."""
    return '"' + name.replace('"', '""') + '"'


def connect_ro(path: str) -> sqlite3.Connection:
    """Open a read-only SQLite connection to the given file."""
    abs_path = os.path.abspath(path)
    uri = f"file:{abs_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_tables(conn: sqlite3.Connection) -> set[str]:
    """Return all user table names in the database."""
    rows = conn.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        """).fetchall()
    return {row["name"] for row in rows}


def get_columns(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    """Return PRAGMA table_info rows for the given table."""
    return conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()


def get_pk_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return primary key column names for the given table, in PK order."""
    cols = get_columns(conn, table)
    pk_cols = [
        row["name"] for row in sorted(cols, key=lambda r: r["pk"]) if row["pk"] > 0
    ]
    return pk_cols


def get_all_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return all column names for the given table."""
    return [row["name"] for row in get_columns(conn, table)]


def row_to_key(row: sqlite3.Row, pk_cols: Sequence[str]) -> tuple:
    """Extract primary key values from a row as a hashable tuple."""
    return tuple(row[col] for col in pk_cols)


def stable_repr(value: object) -> str:
    """Human-readable representation of a value, with stable BLOB hashing."""
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        h = hashlib.sha1(value).hexdigest()
        return f"<BLOB {len(value)} bytes sha1={h}>"
    return repr(value)


def row_matches(row: sqlite3.Row, needle: str | None) -> bool:
    """Return True if any non-BLOB column value contains the needle substring."""
    if not needle:
        return True
    needle = needle.lower()
    for value in row:
        if value is None:
            continue
        if isinstance(value, bytes):
            continue
        if needle in str(value).lower():
            return True
    return False


def fetch_rows_by_pk(
    conn: sqlite3.Connection,
    table: str,
    pk_cols: Sequence[str],
) -> dict[tuple, sqlite3.Row]:
    """Fetch all rows from a table, indexed by primary key tuple."""
    pk_expr = ", ".join(quote_ident(c) for c in pk_cols)
    sql = f"SELECT * FROM {quote_ident(table)} ORDER BY {pk_expr}"
    rows = conn.execute(sql)
    return {row_to_key(row, pk_cols): row for row in rows}


def compare_rows(
    before: sqlite3.Row,
    after: sqlite3.Row,
    columns: Sequence[str],
) -> dict[str, tuple[object, object]]:
    """Return a dict of {column: (old_value, new_value)} for columns that differ."""
    diffs: dict[str, tuple[object, object]] = {}
    for col in columns:
        if before[col] != after[col]:
            diffs[col] = (before[col], after[col])
    return diffs


def summarize_table_counts(
    conn_before: sqlite3.Connection,
    conn_after: sqlite3.Connection,
    tables: Iterable[str],
) -> list[tuple[str, int, int]]:
    """Return (table, before_count, after_count) for tables with differing row counts."""
    out = []
    for table in sorted(tables):
        try:
            b = conn_before.execute(
                f"SELECT COUNT(*) FROM {quote_ident(table)}"
            ).fetchone()[0]
            a = conn_after.execute(
                f"SELECT COUNT(*) FROM {quote_ident(table)}"
            ).fetchone()[0]
            if b != a:
                out.append((table, b, a))
        except sqlite3.DatabaseError as exc:
            print(f"[warn] could not count table {table}: {exc}", file=sys.stderr)
    return out


def compare_table(
    conn_before: sqlite3.Connection,
    conn_after: sqlite3.Connection,
    table: str,
    match: str | None,
    max_rows: int,
) -> dict[str, list]:
    """Compare a single table between two catalogs. Returns a result dict."""
    pk_cols = get_pk_columns(conn_before, table)
    if not pk_cols:
        return {"skipped": [f"{table}: no primary key"]}

    cols_before = get_all_columns(conn_before, table)
    cols_after = get_all_columns(conn_after, table)
    if cols_before != cols_after:
        return {
            "schema_changed": [
                {
                    "table": table,
                    "before_columns": cols_before,
                    "after_columns": cols_after,
                }
            ]
        }

    # NOTE: This loads all rows for both catalogs into memory.
    # For very large tables this may be expensive; use --tables to limit scope.
    before_rows = fetch_rows_by_pk(conn_before, table, pk_cols)
    after_rows = fetch_rows_by_pk(conn_after, table, pk_cols)

    before_keys = set(before_rows)
    after_keys = set(after_rows)

    inserted_keys = sorted(after_keys - before_keys)
    if match:
        inserted_keys = [k for k in inserted_keys if row_matches(after_rows[k], match)]
    inserted_keys = inserted_keys[:max_rows]

    deleted_keys = sorted(before_keys - after_keys)
    if match:
        deleted_keys = [k for k in deleted_keys if row_matches(before_rows[k], match)]
    deleted_keys = deleted_keys[:max_rows]
    common_keys = before_keys & after_keys

    changed = []
    for key in sorted(common_keys):
        b = before_rows[key]
        a = after_rows[key]
        if match and not (row_matches(b, match) or row_matches(a, match)):
            continue
        diffs = compare_rows(b, a, cols_before)
        if diffs:
            changed.append(
                {
                    "pk": dict(zip(pk_cols, key, strict=True)),
                    "diffs": diffs,
                }
            )
            if len(changed) >= max_rows:
                break

    inserted = []
    for key in inserted_keys:
        row = after_rows[key]
        inserted.append({"pk": dict(zip(pk_cols, key, strict=True)), "row": row})

    deleted = []
    for key in deleted_keys:
        row = before_rows[key]
        deleted.append({"pk": dict(zip(pk_cols, key, strict=True)), "row": row})

    return {
        "table": table,
        "pk_cols": pk_cols,
        "changed": changed,
        "inserted": inserted,
        "deleted": deleted,
    }


def print_row(row: sqlite3.Row, prefix: str = "    ") -> None:
    """Print all columns of a row with a prefix indent."""
    for key in row.keys():
        print(f"{prefix}{key} = {stable_repr(row[key])}")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="lrcat_diff",
        description=(
            "Compare two Lightroom Classic .lrcat files and report changed rows/columns.\n"
            "\n"
            "This is a reverse-engineering helper for discovering which tables and\n"
            "columns store specific Lightroom metadata. It is NOT the main migration\n"
            "tool — use green2faststack.py for that.\n"
            "\n"
            "Typical workflow:\n"
            "  1. Close Lightroom Classic.\n"
            "  2. Copy the catalog: cp catalog.lrcat before.lrcat\n"
            "  3. Open Lightroom, make one known change, close Lightroom.\n"
            "  4. Run: %(prog)s before.lrcat catalog.lrcat --match 'IMG_1234'"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  %(prog)s before.lrcat after.lrcat\n"
            '  %(prog)s before.lrcat after.lrcat --match "DSC_0042"\n'
            "  %(prog)s before.lrcat after.lrcat --tables Adobe_images\n"
            "  %(prog)s before.lrcat after.lrcat --max-rows 100\n"
            "\n"
            "memory note:\n"
            "  This tool loads all rows per table into memory. For very large\n"
            "  catalogs, use --tables to compare specific tables one at a time.\n"
            "\n"
            "Both catalogs are opened read-only; no changes are made.\n"
            "Requires only the Python standard library."
        ),
    )
    parser.add_argument(
        "before",
        help="Path to the backup/original .lrcat file.",
    )
    parser.add_argument(
        "after",
        help="Path to the modified/current .lrcat file.",
    )
    parser.add_argument(
        "--match",
        help=(
            "Only show rows where any text column contains this substring. "
            "Useful for isolating changes to a specific photo (e.g. a filename "
            "like IMG_1234 or DSC_0042)."
        ),
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=20,
        help="Maximum inserted/deleted/changed rows to print per table (default: 20).",
    )
    parser.add_argument(
        "--tables",
        nargs="*",
        help="Compare only these specific tables (default: all common tables).",
    )

    if len(sys.argv) == 1:
        parser.print_help()
        return 1

    args = parser.parse_args()

    if not os.path.exists(args.before):
        print(f"error: not found: {args.before}", file=sys.stderr)
        return 2
    if not os.path.exists(args.after):
        print(f"error: not found: {args.after}", file=sys.stderr)
        return 2

    conn_before = connect_ro(args.before)
    conn_after = connect_ro(args.after)

    try:
        tables_before = get_tables(conn_before)
        tables_after = get_tables(conn_after)

        only_before = sorted(tables_before - tables_after)
        only_after = sorted(tables_after - tables_before)
        common = sorted(tables_before & tables_after)

        if args.tables:
            wanted = set(args.tables)
            common = [t for t in common if t in wanted]

        print("=== Catalog table overview ===")
        print(f"Tables in BEFORE only: {len(only_before)}")
        for t in only_before:
            print(f"  - {t}")
        print(f"Tables in AFTER only: {len(only_after)}")
        for t in only_after:
            print(f"  - {t}")
        print(f"Common tables: {len(common)}")

        print("\n=== Tables with changed row counts ===")
        changed_counts = summarize_table_counts(conn_before, conn_after, common)
        if not changed_counts:
            print("  none")
        else:
            for table, b, a in changed_counts:
                print(f"  {table}: {b} -> {a}")

        print("\n=== Row/column diffs ===")
        any_output = False

        for table in common:
            try:
                result = compare_table(
                    conn_before=conn_before,
                    conn_after=conn_after,
                    table=table,
                    match=args.match,
                    max_rows=args.max_rows,
                )
            except sqlite3.DatabaseError as exc:
                print(f"\n--- {table} ---")
                print(f"[warn] could not compare table: {exc}")
                continue

            if "skipped" in result:
                continue

            if "schema_changed" in result:
                print(f"\n--- {table} ---")
                print("Schema changed between catalogs.")
                any_output = True
                continue

            changed = result["changed"]
            inserted = result["inserted"]
            deleted = result["deleted"]

            if not changed and not inserted and not deleted:
                continue

            any_output = True
            print(f"\n--- {table} ---")
            print(f"Primary key columns: {', '.join(result['pk_cols'])}")

            if changed:
                print(f"Changed rows: {len(changed)}")
                for item in changed:
                    print(f"  PK: {item['pk']}")
                    for col, (old, new) in item["diffs"].items():
                        print(f"    {col}: {stable_repr(old)} -> {stable_repr(new)}")

            if inserted:
                print(f"Inserted rows: {len(inserted)}")
                for item in inserted:
                    print(f"  PK: {item['pk']}")
                    print_row(item["row"])

            if deleted:
                print(f"Deleted rows: {len(deleted)}")
                for item in deleted:
                    print(f"  PK: {item['pk']}")
                    print_row(item["row"])

        if not any_output:
            print("No matching row-level diffs found.")
            if args.match:
                print(
                    "Try rerunning without --match, or with a different filename/path fragment."
                )

        return 0
    finally:
        conn_before.close()
        conn_after.close()


if __name__ == "__main__":
    raise SystemExit(main())
