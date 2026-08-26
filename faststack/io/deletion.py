"""Deletion logic for FastStack."""

import logging
import os
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QMessageBox

from faststack.deletion_types import FileIdentity

log = logging.getLogger(__name__)


class SourceChangedError(OSError):
    """The path no longer names the file authorized for deletion."""


def capture_file_identity(path: Path) -> FileIdentity:
    """Capture portable metadata for a path, including expected absence."""
    try:
        stat = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return FileIdentity(exists=False)
    return FileIdentity(
        exists=True,
        device=stat.st_dev,
        file_id=stat.st_ino,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        ctime_ns=stat.st_ctime_ns,
    )


def validate_file_identity(path: Path, expected: FileIdentity) -> None:
    """Raise when *path* differs from its acceptance-time identity."""
    actual = capture_file_identity(path)
    if actual != expected:
        if not expected.exists and actual.exists:
            detail = "appeared after deletion was requested"
        elif expected.exists and not actual.exists:
            detail = "disappeared after deletion was requested"
        else:
            detail = "was replaced or modified after deletion was requested"
        raise SourceChangedError(f"Deletion cancelled because {path.name} {detail}.")


def _mkdir(path: Path) -> None:
    """Helper for mocking Path.mkdir safely."""
    path.mkdir(parents=True, exist_ok=True)


def _unlink(path: Path) -> None:
    """Helper for mocking Path.unlink safely."""
    path.unlink()


def ensure_recycle_bin_dir(recycle_bin_dir: Path) -> bool:
    """Try to create the recycle bin directory.

    Returns:
        True if recycle bin exists or was created successfully.
        False if creation failed (e.g., permission denied).
    """
    try:
        _mkdir(recycle_bin_dir)
        return True
    except (PermissionError, OSError) as e:
        log.exception("Failed to create recycle bin directory: %s", e)
        return False


def confirm_permanent_delete(image_file, reason: str = "") -> bool:
    """Show a confirmation dialog for permanent deletion of a single image.

    Args:
        image_file: The ImageFile to delete permanently.
        reason: Reason for permanent deletion (e.g., "Recycle bin unavailable").

    Returns:
        True if user confirms deletion, False if cancelled.
    """
    jpg_path = image_file.path
    raw_path = image_file.raw_pair

    # Build list of files that will be deleted
    files_to_delete = []

    # Handle primary JPG
    if jpg_path:
        files_to_delete.append(str(jpg_path.name))
    else:
        log.warning("confirm_permanent_delete called with image_file.path=None")

    # Handle RAW pair
    if raw_path and raw_path.exists():
        files_to_delete.append(str(raw_path.name))

    if not files_to_delete:
        log.warning("No files to delete found for confirmation.")
        return False

    file_list = "\n".join(f"  • {f}" for f in files_to_delete)

    msg_box = QMessageBox()
    msg_box.setIcon(QMessageBox.Icon.Warning)
    msg_box.setWindowTitle("Permanent Deletion")

    if reason:
        msg_box.setText(f"{reason}\n\nDeletion is permanent and cannot be undone.")
    else:
        msg_box.setText("Deletion is permanent and cannot be undone.")

    msg_box.setInformativeText(
        f"The following files will be permanently deleted:\n{file_list}"
    )

    delete_btn = msg_box.addButton(
        "Delete Permanently", QMessageBox.ButtonRole.DestructiveRole
    )
    cancel_btn = msg_box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    msg_box.setDefaultButton(cancel_btn)

    msg_box.exec()

    return msg_box.clickedButton() == delete_btn


def confirm_batch_permanent_delete(images: list, reason: str = "") -> bool:
    """Show a confirmation dialog for permanent deletion of multiple images.

    Args:
        images: List of ImageFile objects to delete permanently.
        reason: Reason for permanent deletion.

    Returns:
        True if user confirms deletion, False if cancelled.
    """
    # Count total files (JPG + RAW pairs)
    total_files = 0
    file_names = []
    for img in images:
        file_names.append(img.path.name)
        total_files += 1
        if img.raw_pair and img.raw_pair.exists():
            total_files += 1

    msg_box = QMessageBox()
    msg_box.setIcon(QMessageBox.Icon.Warning)
    msg_box.setWindowTitle("Permanent Deletion")

    if reason:
        msg_box.setText(
            f"{reason}\n\nThis will permanently delete {len(images)} image(s) ({total_files} files)."
        )
    else:
        msg_box.setText(
            f"This will permanently delete {len(images)} image(s) ({total_files} files)."
        )

    # Show first few file names, collapse if too many
    if len(file_names) <= 5:
        file_list = "\n".join(f"  • {f}" for f in file_names)
        msg_box.setInformativeText(
            f"Files to delete:\n{file_list}\n\nThis action cannot be undone."
        )
    else:
        first_few = "\n".join(f"  • {f}" for f in file_names[:3])
        msg_box.setInformativeText(
            f"Files to delete:\n{first_few}\n  ... and {len(file_names) - 3} more\n\nThis action cannot be undone."
        )

    delete_btn = msg_box.addButton(
        f"Delete {len(images)} Images", QMessageBox.ButtonRole.DestructiveRole
    )
    cancel_btn = msg_box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    msg_box.setDefaultButton(cancel_btn)

    msg_box.exec()

    return msg_box.clickedButton() == delete_btn


def permanently_delete_image_files(
    image_file,
    expected_identities: Optional[tuple[FileIdentity, Optional[FileIdentity]]] = None,
) -> bool:
    """Permanently delete an image and its RAW pair from disk.

    This does NOT add to undo history since deletion is permanent.

    Args:
        image_file: The ImageFile to delete.

    Returns:
        True if at least one file was deleted, False otherwise.
    """
    deleted_any = False
    jpg_path = image_file.path
    raw_path = image_file.raw_pair

    if expected_identities is not None:
        jpg_identity, raw_identity = expected_identities
        if jpg_path:
            validate_file_identity(jpg_path, jpg_identity)
        if raw_path and raw_identity is not None:
            validate_file_identity(raw_path, raw_identity)

    # Delete JPG
    if jpg_path and jpg_path.exists():
        try:
            if expected_identities is not None:
                validate_file_identity(jpg_path, expected_identities[0])
            _unlink(jpg_path)
            log.info("Permanently deleted: %s", jpg_path.name)
            deleted_any = True
        except SourceChangedError:
            raise
        except OSError as e:
            log.exception("Failed to permanently delete %s: %s", jpg_path.name, e)

    # Delete RAW if exists
    if raw_path and raw_path.exists():
        try:
            if expected_identities is not None and expected_identities[1] is not None:
                validate_file_identity(raw_path, expected_identities[1])
            _unlink(raw_path)
            log.info("Permanently deleted: %s", raw_path.name)
            deleted_any = True
        except SourceChangedError:
            raise
        except OSError as e:
            log.exception("Failed to permanently delete %s: %s", raw_path.name, e)

    return deleted_any
