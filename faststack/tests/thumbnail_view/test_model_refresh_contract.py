"""Contracts for ThumbnailModel.refresh_from_controller path semantics."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from faststack.thumbnail_view.model import ThumbnailModel


@pytest.fixture(scope="module")
def qapp():
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication(sys.argv)
    yield app


def test_refresh_from_controller_image_entries_use_input_paths(tmp_path, qapp):
    _ = qapp
    keep_path = tmp_path / "keep.jpg"
    skip_path = tmp_path / "skip.jpg"
    keep_path.touch()
    skip_path.touch()
    images = [
        SimpleNamespace(
            path=keep_path,
            timestamp=1.0,
            has_backups=False,
            has_developed=False,
        ),
        SimpleNamespace(
            path=skip_path,
            timestamp=2.0,
            has_backups=False,
            has_developed=False,
        ),
    ]
    model = ThumbnailModel(
        base_directory=tmp_path,
        current_directory=tmp_path,
        get_metadata_callback=None,
    )
    model.set_filter("keep", refresh=False)

    model.refresh_from_controller(images, metadata_map={})

    image_entries = [entry for entry in model._entries if not entry.is_folder]
    assert [entry.path for entry in image_entries] == [keep_path]


def test_refresh_from_controller_preserves_surviving_selection_by_path(tmp_path, qapp):
    _ = qapp
    paths = [tmp_path / name for name in ("a.jpg", "b.jpg", "c.jpg")]
    for path in paths:
        path.touch()

    def image(path, timestamp):
        return SimpleNamespace(
            path=path,
            timestamp=timestamp,
            has_backups=False,
            has_developed=False,
        )

    model = ThumbnailModel(tmp_path, tmp_path)
    model.refresh_from_controller(
        [image(path, index) for index, path in enumerate(paths)], metadata_map={}
    )
    model.select_index(model.find_image_index(paths[1]))

    model.refresh_from_controller(
        [image(paths[2], 2), image(paths[1], 1)], metadata_map={}
    )

    assert model.get_selected_paths() == [paths[1]]
