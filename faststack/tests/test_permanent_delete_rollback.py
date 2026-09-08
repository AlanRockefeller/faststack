"""Rollback-safety tests for permanent deletion.

Permanent deletion stages files with ``os.replace`` before unlinking them, so
it must be able to put them back. These tests cover the preflight that refuses
the transaction when no atomic no-replace restore exists, and the rollback
paths themselves.
"""

import errno
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from faststack.io.deletion import (
    SourceChangedError,
    permanently_delete_image_files,
)


class MockImageFile:
    """Simple mock for ImageFile."""

    def __init__(self, jpg_path: Path | None, raw_path: Path | None = None):
        self.path = jpg_path
        self.raw_pair = raw_path
        self.is_video = False


def _transaction_dirs(directory: Path) -> list[Path]:
    return sorted(directory.glob(".faststack-delete-*"))


def _probe_leftovers(directory: Path) -> list[Path]:
    return sorted(directory.glob(".faststack-linkprobe-*"))


def _require_symlinks(tmp_path: Path) -> None:
    try:
        os.symlink(tmp_path / "missing", tmp_path / ".symlink-probe")
    except (OSError, NotImplementedError):
        pytest.skip("Filesystem or platform does not support symlinks")
    (tmp_path / ".symlink-probe").unlink()


@pytest.mark.parametrize(
    "failure",
    [
        OSError(errno.EPERM, "hard links not supported"),
        NotImplementedError("follow_symlinks unavailable"),
    ],
    ids=["oserror", "notimplemented"],
)
class TestRollbackCapabilityPreflight:
    def test_refuses_delete_when_hard_links_unavailable(self, tmp_path, failure):
        """A filesystem without hard links must not lose the original file."""
        jpg = tmp_path / "img.jpg"
        jpg.write_text("jpg-bytes")
        img = MockImageFile(jpg, None)

        with patch(
            "faststack.io.deletion._atomic_no_replace_link", side_effect=failure
        ):
            result = permanently_delete_image_files(img)

        assert result is False
        assert jpg.exists()
        assert jpg.read_text() == "jpg-bytes"
        assert _transaction_dirs(tmp_path) == []
        assert _probe_leftovers(tmp_path) == []

    def test_no_source_is_staged_when_rollback_unavailable(self, tmp_path, failure):
        """The preflight must run before the first os.replace of a source."""
        jpg = tmp_path / "img.jpg"
        raw = tmp_path / "img.orf"
        jpg.write_text("jpg-bytes")
        raw.write_text("raw-bytes")
        img = MockImageFile(jpg, raw)

        with patch(
            "faststack.io.deletion._atomic_no_replace_link", side_effect=failure
        ):
            with patch("faststack.io.deletion.os.replace") as mock_replace:
                result = permanently_delete_image_files(img)

        assert result is False
        assert mock_replace.call_count == 0
        assert jpg.read_text() == "jpg-bytes"
        assert raw.read_text() == "raw-bytes"
        assert _transaction_dirs(tmp_path) == []


class TestRollbackOnSupportedFilesystem:
    def test_staging_failure_restores_every_member(self, tmp_path):
        """JPG/RAW deletion stays all-or-nothing when staging fails midway."""
        jpg = tmp_path / "img.jpg"
        raw = tmp_path / "img.orf"
        jpg.write_text("jpg-bytes")
        raw.write_text("raw-bytes")
        jpg_inode = jpg.stat().st_ino
        img = MockImageFile(jpg, raw)

        real_replace = os.replace
        calls = []

        def fake_replace(src, dst):
            calls.append(Path(src))
            if len(calls) == 2:
                raise OSError(errno.EIO, "simulated staging failure")
            real_replace(src, dst)

        with patch("faststack.io.deletion.os.replace", side_effect=fake_replace):
            result = permanently_delete_image_files(img)

        assert result is False
        assert jpg.read_text() == "jpg-bytes"
        assert raw.read_text() == "raw-bytes"
        # Restored through the same filesystem object, not a copy.
        assert jpg.stat().st_ino == jpg_inode
        assert _transaction_dirs(tmp_path) == []

    def test_unlink_failure_restores_from_recovery_copy(self, tmp_path):
        """A failed unlink rolls back to the authorized bytes."""
        jpg = tmp_path / "img.jpg"
        jpg.write_text("jpg-bytes")
        img = MockImageFile(jpg, None)

        with patch("faststack.io.deletion._unlink", side_effect=OSError("Protected")):
            result = permanently_delete_image_files(img)

        assert result is False
        assert jpg.read_text() == "jpg-bytes"

    def test_successful_delete_leaves_no_probe_debris(self, tmp_path):
        """The link probe must not survive an ordinary deletion."""
        jpg = tmp_path / "img.jpg"
        raw = tmp_path / "img.orf"
        jpg.write_text("jpg-bytes")
        raw.write_text("raw-bytes")
        img = MockImageFile(jpg, raw)

        assert permanently_delete_image_files(img) is True

        assert list(tmp_path.iterdir()) == []


class TestReplacementIsNeverOverwritten:
    def test_rollback_refuses_to_clobber_a_new_file(self, tmp_path):
        """A file that appeared at the original pathname must survive."""
        jpg = tmp_path / "img.jpg"
        raw = tmp_path / "img.orf"
        jpg.write_text("jpg-bytes")
        raw.write_text("raw-bytes")
        img = MockImageFile(jpg, raw)

        real_replace = os.replace
        calls = []

        def fake_replace(src, dst):
            calls.append(Path(src))
            if len(calls) == 2:
                # A concurrent writer recreates the first pathname before the
                # transaction rolls back.
                jpg.write_text("replacement")
                raise OSError(errno.EIO, "simulated staging failure")
            real_replace(src, dst)

        with patch("faststack.io.deletion.os.replace", side_effect=fake_replace):
            with pytest.raises(SourceChangedError):
                permanently_delete_image_files(img)

        assert jpg.read_text() == "replacement"
        assert raw.read_text() == "raw-bytes"
        # The authorized bytes are retained for recovery, never discarded.
        retained = _transaction_dirs(tmp_path)
        assert len(retained) == 1
        staged = list(retained[0].iterdir())
        assert [path.read_text() for path in staged] == ["jpg-bytes"]


class TestSymlinkPreservation:
    def test_symlink_stays_a_symlink_after_forced_rollback(self, tmp_path):
        """Rollback must restore the link, not a copy of its target."""
        _require_symlinks(tmp_path)
        target = tmp_path / "target.bin"
        target.write_text("target-bytes")
        link = tmp_path / "img.jpg"
        # An absolute target keeps the link resolvable from the transaction
        # directory, so the rollback goes through the recovery copy rather
        # than short-circuiting on a dangling staged link.
        link.symlink_to(target)
        img = MockImageFile(link, None)

        with patch("faststack.io.deletion._unlink", side_effect=OSError("Protected")):
            result = permanently_delete_image_files(img)

        assert result is False
        assert link.is_symlink()
        assert os.readlink(link) == str(target)
        assert not link.is_dir()
        assert target.read_text() == "target-bytes"
