"""Typed dataclasses for the deletion pipeline.

Replaces ad-hoc dicts with structured types for clarity, typo-safety,
and self-documenting field names.
"""

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple


from enum import Enum


class DeletionErrorCodes(str, Enum):
    """Standardized error codes for deletion failures."""

    RECYCLE_FAILED = "recycle_failed"
    PERMISSION_DENIED = "permission_denied"
    TRASH_FULL = "trash_full"
    ROLLBACK_FAILED = "raw_recycle_failed_rollback_failed"
    RAW_RECYCLE_FAILED = "raw_recycle_failed"
    ROLLBACK_DEST_EXISTS = "rollback_dest_exists"
    INVALID_WORK_ITEM = "invalid_work_item"
    CANCELLED = "cancelled"  # Added standardized code
    UNKNOWN = "unknown"


@dataclass
class DeleteJob:
    """In-flight delete job tracked in _pending_delete_jobs.

    Created by _delete_indices, consumed by _on_delete_finished / undo_delete.
    """

    job_id: int
    removed_items: List[Tuple[int, Any]]  # (original_index, ImageFile)
    action_type: str  # 'loupe', 'grid_selection', 'grid_cursor', 'batch'
    timestamp: float
    cancel_event: threading.Event
    previous_index: int
    images_to_delete: List[Any]  # List[ImageFile] objects removed from UI
    user_undone: bool = False
    undo_requested: bool = False  # Policy 1: auto-restore files on completion
    saved_batches: Optional[list] = None
    saved_batch_start_index: Optional[int] = None


@dataclass
class DeleteRecord:
    """Single file-pair result from the delete worker."""

    jpg: Optional[Path] = None
    recycled_jpg: Optional[Path] = None
    raw: Optional[Path] = None
    recycled_raw: Optional[Path] = None


@dataclass
class DeleteWarning:
    """Partial success: JPG recycled but RAW move failed."""

    jpg: Optional[Path] = None
    raw: Optional[Path] = None
    message: str = ""


@dataclass
class DeleteFailure:
    """Failed deletion attempt."""

    jpg: Optional[Path] = None
    raw: Optional[Path] = None
    code: str = ""
    message: str = ""


@dataclass
class DeleteResult:
    """Parsed worker result, used on the UI thread side only.

    The worker still returns a plain dict over the Qt signal boundary.
    _on_delete_finished converts it into this immediately.
    """

    job_id: int = 0
    successes: List[DeleteRecord] = field(default_factory=list)
    warnings: List[DeleteWarning] = field(default_factory=list)
    failures: List[DeleteFailure] = field(default_factory=list)
    cancelled: bool = False

    # Permanent delete result (unified into same type)
    is_perm_result: bool = False
    perm_success: list = field(default_factory=list)  # List[(idx, ImageFile)]
    perm_fail: list = field(default_factory=list)  # List[(idx, ImageFile)]

    @classmethod
    def from_worker_dict(cls, raw: dict) -> "DeleteResult":
        """Parse a raw worker dict into a typed DeleteResult.

        Handles both recycle results and permanent delete results.
        Converts all path strings back to Path objects.
        """
        if raw.get("_perm_result"):
            return cls(
                job_id=raw.get("job_id", 0),
                is_perm_result=True,
                perm_success=raw.get("perm_success", []),
                perm_fail=raw.get("perm_fail", []),
            )

        def _to_path(v):
            return Path(v) if v is not None else None

        successes = []
        for s in raw.get("successes", []):
            successes.append(
                DeleteRecord(
                    jpg=_to_path(s.get("jpg")),
                    recycled_jpg=_to_path(s.get("recycled_jpg")),
                    raw=_to_path(s.get("raw")),
                    recycled_raw=_to_path(s.get("recycled_raw")),
                )
            )

        warnings = []
        for w in raw.get("warnings", []):
            warnings.append(
                DeleteWarning(
                    jpg=_to_path(w.get("jpg")),
                    raw=_to_path(w.get("raw")),
                    message=w.get("message", ""),
                )
            )

        failures = []
        for f in raw.get("failures", []):
            failures.append(
                DeleteFailure(
                    jpg=_to_path(f.get("jpg")),
                    raw=_to_path(f.get("raw")),
                    code=f.get("code", ""),
                    message=f.get("message", ""),
                )
            )

        return cls(
            job_id=raw.get("job_id", 0),
            successes=successes,
            warnings=warnings,
            failures=failures,
            cancelled=raw.get("cancelled", False),
        )
