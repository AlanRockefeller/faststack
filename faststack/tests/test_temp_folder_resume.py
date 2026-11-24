"""Startup never offers to reopen a folder in the system temp directory.

A FastStack run on a throwaway folder there (test fixtures, an extracted
archive) that was killed rather than closed left a crash-recovery record, so
the next plain launch offered to reopen that long-gone scratch folder. Temp
folders are now skipped by the recovery prompt (their records are pruned),
are never resumed as the last folder, and never overwrite the remembered one.
Opening one explicitly on the command line still works.
"""

from __future__ import annotations

import inspect
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from faststack import app as app_mod
from faststack.app import AppController, _is_temporary_directory


@pytest.fixture
def temp_folder():
    folder = Path(tempfile.mkdtemp(prefix="faststack-scratch-"))
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def test_folders_in_the_temp_dir_are_temporary(temp_folder):
    assert _is_temporary_directory(temp_folder)
    assert _is_temporary_directory(str(temp_folder / "nested" / "photos"))


def test_other_folders_are_not_temporary(tmp_path):
    # The filesystem root is never inside the temp dir: checked before the
    # skip below, so a check that took every folder for temporary still fails.
    assert not _is_temporary_directory(Path(tmp_path.anchor))
    assert not _is_temporary_directory("")
    assert not _is_temporary_directory(None)
    # pytest's basetemp is ./.pytest-temp in the repo, not the system temp
    # dir, unless the checkout lives there or --basetemp points there.
    if _is_temporary_directory(tmp_path):
        pytest.skip("pytest's temp folder is inside the system temp directory")
    assert not _is_temporary_directory(tmp_path)


def _remember(image_dir):
    fake = SimpleNamespace(image_dir=image_dir, _last_persisted_dir=None)
    with patch("faststack.app.config") as config:
        AppController._remember_last_directory(fake)
    return config


def test_a_temp_folder_does_not_replace_the_remembered_last_folder(temp_folder):
    config = _remember(temp_folder)
    config.set.assert_not_called()
    config.save.assert_not_called()


def test_an_ordinary_folder_is_still_remembered(tmp_path):
    if _is_temporary_directory(tmp_path):
        pytest.skip("pytest's temp folder is inside the system temp directory")
    config = _remember(tmp_path)
    config.set.assert_called_once_with("core", "last_directory", str(tmp_path.resolve()))


def test_main_filters_temp_folders_before_the_recovery_prompt():
    source = inspect.getsource(app_mod.main)
    filter_at = source.index('_is_temporary_directory(r.get("dir", ""))')
    prompt_at = source.index("_prompt_reopen_sessions(stale_records)")
    prune_at = source.index("SessionRegistry.prune(prunable)")
    assert filter_at < prompt_at < prune_at
    # The last-folder fallback skips temp folders too.
    assert "not _is_temporary_directory(last_dir_path)" in source
