"""Mapping between a photo tree with plain dated folders (``2026-11-05``) and
one that adds a year folder (``2026/2026-11-05``), in either direction."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from faststack.app import AppController

candidates = AppController._restack_relative_candidates


@pytest.mark.parametrize(
    "relative, expected",
    [
        ("2026-11-05", ["2026-11-05", "2026/2026-11-05"]),
        (
            "2026-11-05 Mushrooms/sub",
            ["2026-11-05 Mushrooms/sub", "2026/2026-11-05 Mushrooms/sub"],
        ),
        ("2026/2026-11-05", ["2026/2026-11-05", "2026-11-05"]),
        ("2026/2026-11-05/sub", ["2026/2026-11-05/sub", "2026-11-05/sub"]),
        # Not dated folders: only the identical path.
        ("misc", ["misc"]),
        ("2026/2026-archive", ["2026/2026-archive"]),
        ("2026/2026-11", ["2026/2026-11"]),
        ("2026-11-055", ["2026-11-055"]),
        # A year folder holding another year's date is not a year wrap.
        ("2026/2025-12-31", ["2026/2025-12-31"]),
        (".", ["."]),
    ],
)
def test_candidates(relative, expected):
    assert candidates(Path(relative)) == [Path(p) for p in expected]


def _fake_controller(image_dir: Path, main: Path, stack_root: Path):
    return SimpleNamespace(
        image_dir=image_dir,
        _main_photo_directory=lambda: main.resolve(),
        _configured_restack_raw_locations=lambda: [("Primary", stack_root)],
        _resolve_configured_directory=lambda p: Path(p).resolve(),
        _restack_relative_candidates=candidates,
    )


@pytest.mark.parametrize(
    "main_rel, stack_rel",
    [
        ("2026-11-05", "2026/2026-11-05"),  # stack tree adds the year
        ("2026/2026-11-05", "2026-11-05"),  # main tree adds the year
        ("2026/2026-archive", "2026/2026-archive"),  # identical, not dated
    ],
)
def test_main_folder_finds_stack_input(tmp_path, main_rel, stack_rel):
    main = tmp_path / "photos"
    stack_root = tmp_path / "stack"
    (main / main_rel).mkdir(parents=True)
    (stack_root / stack_rel).mkdir(parents=True)

    fake = _fake_controller(main / main_rel, main, stack_root)
    found = AppController._matching_stack_input_directory(fake)
    assert found == stack_root.resolve() / stack_rel


def test_identical_stack_input_folder_is_preferred(tmp_path):
    main = tmp_path / "photos"
    stack_root = tmp_path / "stack"
    (main / "2026-11-05").mkdir(parents=True)
    (stack_root / "2026-11-05").mkdir(parents=True)
    (stack_root / "2026" / "2026-11-05").mkdir(parents=True)

    fake = _fake_controller(main / "2026-11-05", main, stack_root)
    found = AppController._matching_stack_input_directory(fake)
    assert found == stack_root.resolve() / "2026-11-05"


def test_stack_input_context_keeps_relative_path(tmp_path):
    main = tmp_path / "photos"
    stack_root = tmp_path / "stack"
    (stack_root / "2026" / "2026-11-05").mkdir(parents=True)

    fake = _fake_controller(stack_root / "2026" / "2026-11-05", main, stack_root)
    root, relative = AppController._current_stack_input_context(fake)
    assert root == stack_root.resolve()
    assert relative == Path("2026/2026-11-05")
    # Switching back to main tries the plain folder next.
    assert candidates(relative)[1] == Path("2026-11-05")
