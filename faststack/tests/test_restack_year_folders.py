"""Year-wrapped stack-input folders (``2026/2026-11-05``) vs. plain dated
main-photo folders (``2026-11-05``)."""

from pathlib import Path
from types import SimpleNamespace

from faststack.app import AppController

candidates = AppController._restack_relative_candidates
unwrap = AppController._unwrap_restack_year


def test_dated_folder_gets_year_wrapped_candidate():
    assert candidates(Path("2026-11-05")) == [
        Path("2026-11-05"),
        Path("2026/2026-11-05"),
    ]


def test_dated_folder_with_suffix_and_subfolder():
    assert candidates(Path("2026-11-05 Mushrooms/sub")) == [
        Path("2026-11-05 Mushrooms/sub"),
        Path("2026/2026-11-05 Mushrooms/sub"),
    ]


def test_undated_or_already_wrapped_folder_is_left_alone():
    assert candidates(Path("misc")) == [Path("misc")]
    assert candidates(Path("2026/2026-11-05")) == [Path("2026/2026-11-05")]
    assert candidates(Path(".")) == [Path(".")]


def test_unwrap_year():
    assert unwrap(Path("2026/2026-11-05")) == Path("2026-11-05")
    assert unwrap(Path("2026/2026-11-05/sub")) == Path("2026-11-05/sub")
    assert unwrap(Path("2026-11-05")) == Path("2026-11-05")
    # A year folder whose child is from a different year is not a wrap.
    assert unwrap(Path("2026/2025-12-31")) == Path("2026/2025-12-31")


def _fake_controller(image_dir: Path, main: Path, stack_root: Path):
    fake = SimpleNamespace(
        image_dir=image_dir,
        _main_photo_directory=lambda: main.resolve(),
        _configured_restack_raw_locations=lambda: [("Primary", stack_root)],
        _resolve_configured_directory=lambda p: Path(p).resolve(),
        _restack_relative_candidates=candidates,
        _unwrap_restack_year=unwrap,
    )
    return fake


def test_main_folder_finds_year_wrapped_stack_input(tmp_path):
    main = tmp_path / "photos"
    stack_root = tmp_path / "stack"
    (main / "2026-11-05").mkdir(parents=True)
    (stack_root / "2026" / "2026-11-05").mkdir(parents=True)

    fake = _fake_controller(main / "2026-11-05", main, stack_root)
    found = AppController._matching_stack_input_directory(fake)
    assert found == stack_root.resolve() / "2026" / "2026-11-05"


def test_plain_stack_input_folder_is_preferred(tmp_path):
    main = tmp_path / "photos"
    stack_root = tmp_path / "stack"
    (main / "2026-11-05").mkdir(parents=True)
    (stack_root / "2026-11-05").mkdir(parents=True)
    (stack_root / "2026" / "2026-11-05").mkdir(parents=True)

    fake = _fake_controller(main / "2026-11-05", main, stack_root)
    found = AppController._matching_stack_input_directory(fake)
    assert found == stack_root.resolve() / "2026-11-05"


def test_stack_input_context_unwraps_year(tmp_path):
    main = tmp_path / "photos"
    stack_root = tmp_path / "stack"
    (stack_root / "2026" / "2026-11-05").mkdir(parents=True)

    fake = _fake_controller(stack_root / "2026" / "2026-11-05", main, stack_root)
    root, relative = AppController._current_stack_input_context(fake)
    assert root == stack_root.resolve()
    assert relative == Path("2026-11-05")
