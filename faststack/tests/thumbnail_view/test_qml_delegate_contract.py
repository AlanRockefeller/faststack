"""Contract tests between ThumbnailModel roles and ThumbnailTile QML."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication

from faststack.thumbnail_view.folder_stats import FolderStats
from faststack.thumbnail_view.model import ThumbnailEntry, ThumbnailModel


@pytest.fixture(scope="module")
def qapp():
    """Ensure a Qt core app exists for model instances."""
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication(sys.argv)
    yield app


def _thumbnail_tile_required_properties() -> set[str]:
    qml_path = Path(__file__).resolve().parents[2] / "qml" / "ThumbnailTile.qml"
    qml_text = qml_path.read_text(encoding="utf-8")
    sanitized_text = _sanitize_qml(qml_text)
    root_body = _extract_tile_root_body(qml_text, sanitized_text)
    sanitized_root_body = _extract_tile_root_body(sanitized_text, sanitized_text)

    required_props: set[str] = set()
    depth = 0
    for raw_line, sanitized_line in zip(
        root_body.splitlines(), sanitized_root_body.splitlines()
    ):
        if depth == 0:
            match = re.fullmatch(r"\s*required property \w+ (\w+)\s*", raw_line)
            if match:
                required_props.add(match.group(1))
        depth += sanitized_line.count("{") - sanitized_line.count("}")

    return required_props


def _sanitize_qml(qml_text: str) -> str:
    """Strip strings and comments while preserving braces and newlines."""
    out: list[str] = []
    i = 0
    in_line_comment = False
    in_block_comment = False
    in_string: str | None = None

    while i < len(qml_text):
        ch = qml_text[i]
        nxt = qml_text[i + 1] if i + 1 < len(qml_text) else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                out.append(ch)
            else:
                out.append(" ")
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                # Replace the two closing-comment characters one-for-one so
                # brace positions still line up with the original source.
                out.extend("  ")
                in_block_comment = False
                i += 2
            else:
                out.append("\n" if ch == "\n" else " ")
                i += 1
            continue

        if in_string is not None:
            if ch == "\\" and nxt:
                # Preserve character count for escaped pairs as well.
                out.extend("  ")
                i += 2
            elif ch == in_string:
                out.append(" ")
                in_string = None
                i += 1
            else:
                out.append("\n" if ch == "\n" else " ")
                i += 1
            continue

        if ch == "/" and nxt == "/":
            # Replace both comment opener chars to keep indices aligned.
            out.extend("  ")
            in_line_comment = True
            i += 2
            continue

        if ch == "/" and nxt == "*":
            # Replace both comment opener chars to keep indices aligned.
            out.extend("  ")
            in_block_comment = True
            i += 2
            continue

        if ch in {"'", '"'}:
            out.append(" ")
            in_string = ch
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _extract_tile_root_body(qml_text: str, sanitized_text: str) -> str:
    """Extract the body of the root `Item { ... }` with `id: tile`.

    This relies on the current ThumbnailTile.qml structure and normal QML
    convention that `id: tile` is declared directly in the root Item body
    before any nested child blocks.
    """
    tile_id_pos = sanitized_text.find("id: tile")
    assert tile_id_pos != -1, "Could not find `id: tile` in ThumbnailTile.qml"

    # Find the Item body that owns `id: tile`. For the current file, the
    # nearest preceding '{' is the root Item opening brace.
    open_brace_pos = sanitized_text.rfind("{", 0, tile_id_pos)
    assert open_brace_pos != -1, "Could not find root Item opening brace"

    item_pos = sanitized_text.rfind("Item", 0, open_brace_pos)
    assert item_pos != -1, "Could not find root Item declaration"

    depth = 1
    close_brace_pos = open_brace_pos + 1
    while close_brace_pos < len(sanitized_text) and depth > 0:
        if sanitized_text[close_brace_pos] == "{":
            depth += 1
        elif sanitized_text[close_brace_pos] == "}":
            depth -= 1
        close_brace_pos += 1

    assert depth == 0, "Could not find matching root Item closing brace"
    return qml_text[open_brace_pos + 1 : close_brace_pos - 1]


def _role_ids_by_name(model: ThumbnailModel) -> dict[str, int]:
    return {name.decode("utf-8"): role for role, name in model.roleNames().items()}


def test_thumbnail_tile_required_roles_exist_on_model(tmp_path, qapp):
    """Every required ThumbnailTile delegate role should be defined by the model."""
    model = ThumbnailModel(
        base_directory=tmp_path,
        current_directory=tmp_path,
        get_metadata_callback=None,
    )

    required_props = _thumbnail_tile_required_properties()
    # `index` is injected by GridView itself, not by ThumbnailModel.roleNames().
    required_model_roles = required_props - {"index"}

    model_role_names = set(_role_ids_by_name(model))
    missing_roles = required_model_roles - model_role_names

    assert missing_roles == set()


def test_thumbnail_tile_required_roles_have_values_for_all_entry_kinds(tmp_path, qapp):
    """Image, folder, and synthetic parent rows should all satisfy delegate requirements."""
    model = ThumbnailModel(
        base_directory=tmp_path,
        current_directory=tmp_path,
        get_metadata_callback=None,
    )

    folder_stats = FolderStats(
        total_images=4,
        stacked_count=1,
        uploaded_count=2,
        edited_count=1,
        jpg_count=3,
        raw_count=1,
        coverage_buckets=[(1.0, 0.5, 0.25, 0.0)],
    )
    parent_entry = ThumbnailEntry(
        path=tmp_path.parent,
        name="..",
        is_folder=True,
        mtime_ns=0,
    )
    folder_entry = ThumbnailEntry(
        path=tmp_path / "child",
        name="child",
        is_folder=True,
        folder_stats=folder_stats,
        mtime_ns=123,
    )
    image_entry = ThumbnailEntry(
        path=tmp_path / "photo.jpg",
        name="photo.jpg",
        is_folder=False,
        is_stacked=True,
        is_uploaded=True,
        is_edited=True,
        is_restacked=True,
        is_favorite=True,
        is_todo=True,
        has_backups=True,
        has_developed=True,
        mtime_ns=456,
    )
    model._entries = [parent_entry, folder_entry, image_entry]

    role_ids = _role_ids_by_name(model)
    required_props = _thumbnail_tile_required_properties() - {"index"}

    for row in range(len(model._entries)):
        index = model.index(row, 0)
        for role_name in required_props:
            value = model.data(index, role_ids[role_name])
            assert value is not None, f"row {row} missing value for role {role_name}"

    image_folder_stats = model.data(model.index(2, 0), role_ids["folderStats"])
    assert image_folder_stats == {
        "total_images": 0,
        "stacked_count": 0,
        "uploaded_count": 0,
        "edited_count": 0,
        "jpg_count": 0,
        "raw_count": 0,
        "batch_count": 0,
        "coverage_buckets": [],
        "batch_coverage_buckets": [],
    }
