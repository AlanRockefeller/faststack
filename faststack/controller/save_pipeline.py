"""Live edit session tracking and the save pipeline: pending edit state,
save requests, save tracking, close-time flushes and save completion.

Mixed into AppController (faststack/app.py); every method runs on the
controller instance.
"""

import concurrent.futures
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np
from PIL import Image
from PySide6.QtCore import (
    Slot,
)

from faststack.imaging.cache import (
    build_cache_key,
)
from faststack.imaging.stack_paint import (
    stack_layers,
)

log = logging.getLogger(__name__)


class SavePipelineMixin:
    def _build_pending_edit_state(self, request: dict[str, Any]) -> dict[str, Any]:
        snapshot = request.get("snapshot", {})
        context = request.get("context", {})
        session_token = context.get("session_token")
        if isinstance(session_token, tuple):
            session_token = list(session_token)
        source_path = (
            context.get("edit_source_path")
            or snapshot.get("source_filepath")
            or snapshot.get("filepath_snapshot")
            or ""
        )
        return {
            "version": 1,
            "status": "pending_save",
            "request_id": context.get("save_request_id"),
            "revision": context.get("save_revision"),
            "session_token": session_token,
            "source_path": str(source_path),
            "target_path": str(context.get("target") or ""),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "edits": self._serialize_editor_edits(snapshot.get("edits") or {}),
            "masks": self._serialize_mask_assets(snapshot.get("mask_assets") or {}),
            "auto_adjust": context.get("auto_adjust_state"),
        }

    def _build_saved_edit_state_for_result(
        self,
        save_result: dict[str, Any],
        *,
        saved_path: Path,
        backup_path: Optional[Path],
    ) -> Optional[dict[str, Any]]:
        request = save_result.get("request") or {}
        state = self._build_pending_edit_state(request)
        context = request.get("context", {})
        source_path = context.get("edit_source_path") or state.get("source_path")
        if backup_path is not None:
            try:
                source_matches_saved = source_path is None or self._key(
                    Path(source_path)
                ) == self._key(saved_path)
            except (OSError, TypeError, ValueError):
                source_matches_saved = False
            if source_matches_saved:
                source_path = str(backup_path)

        state["status"] = "saved"
        state["source_path"] = str(source_path or backup_path or saved_path)
        state["saved_at"] = datetime.now().isoformat(timespec="seconds")
        state["target_fingerprint"] = self._edit_target_fingerprint(saved_path)
        if not self._edit_state_has_replayable_source(state, saved_path):
            log.debug(
                "Not storing saved edit state for %s; source is not replayable",
                saved_path,
            )
            return None
        return state

    def _build_saved_edit_state_from_editor(
        self,
        *,
        saved_path: Path,
        backup_path: Optional[Path],
    ) -> Optional[dict[str, Any]]:
        with self.image_editor._lock:
            edits = dict(self.image_editor.current_edits)
            masks = dict(self.image_editor._mask_assets)
            source_path = self.image_editor.source_filepath or saved_path

        if backup_path is not None:
            try:
                if self._key(source_path) == self._key(saved_path):
                    source_path = backup_path
            except (OSError, TypeError, ValueError):
                pass

        state = {
            "version": 1,
            "status": "saved",
            "request_id": None,
            "revision": int(getattr(self.image_editor, "_edits_rev", 0)),
            "session_token": None,
            "source_path": str(source_path),
            "target_path": str(saved_path),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "target_fingerprint": self._edit_target_fingerprint(saved_path),
            "edits": self._serialize_editor_edits(edits),
            "masks": self._serialize_mask_assets(masks),
            "auto_adjust": self._serialize_current_auto_adjust_state(),
        }
        if not self._edit_state_has_replayable_source(state, saved_path):
            log.debug(
                "Not storing saved edit state for %s; source is not replayable",
                saved_path,
            )
            return None
        return state

    @staticmethod
    def _edit_target_fingerprint(path: Optional[Path]) -> Optional[list[int]]:
        """[size, mtime_ns] of the file a saved edit state produced.

        Captured after the save finished (saves restore the original mtime
        inside the write, so this is the final value). An external editor
        overwriting the target changes it. ctime/inode are deliberately left
        out so copying or moving the folder does not invalidate the state.
        """
        if path is None:
            return None
        try:
            st = os.stat(path)
        except (OSError, TypeError, ValueError):
            return None
        return [int(st.st_size), int(st.st_mtime_ns)]

    def _saved_edit_state_matches_target(
        self, edit_state: dict[str, Any], target_path: Path
    ) -> bool:
        """False when the target changed since FastStack saved this state.

        Replaying a stale "saved" state would reload the old source plus the
        old edits and let the next FastStack save overwrite a newer external
        result (Photoshop, GIMP, ...). States written before fingerprints
        existed cannot be verified and are accepted as before.
        """
        if edit_state.get("status") != "saved":
            return True
        expected = edit_state.get("target_fingerprint")
        if expected is None:
            return True
        current = self._edit_target_fingerprint(target_path)
        try:
            return current is not None and list(expected) == current
        except TypeError:
            return False

    def _edit_state_has_replayable_source(
        self, edit_state: dict[str, Any], target_fallback: Optional[Path]
    ) -> bool:
        """Return true when edit parameters can be safely replayed from source."""
        source_raw = edit_state.get("source_path")
        if not source_raw:
            return False

        target_raw = edit_state.get("target_path") or target_fallback
        if not target_raw:
            return False

        source_path = Path(source_raw)
        target_path = Path(target_raw)
        if not source_path.is_absolute():
            source_path = self.image_dir / source_path
        if not target_path.is_absolute():
            target_path = self.image_dir / target_path

        try:
            if not source_path.exists():
                return False
            source_key = self._key(source_path.resolve(strict=True))
            target_key = self._key(target_path.resolve(strict=False))
            return source_key != target_key
        except (OSError, TypeError, ValueError):
            return False

    def _write_pending_edit_state_for_request(self, request: dict[str, Any]) -> None:
        context = request.get("context", {})
        sidecar = context.get("save_sidecar") or self.sidecar
        metadata_path = context.get("save_metadata_path")
        if sidecar is None or not metadata_path:
            return
        edit_state = self._build_pending_edit_state(request)
        if not self._edit_state_has_replayable_source(edit_state, Path(metadata_path)):
            log.debug(
                "Skipping persisted pending edit state for %s; source is not a "
                "durable path distinct from the save target",
                metadata_path,
            )
            return
        try:
            sidecar.update_metadata(
                Path(metadata_path),
                {"edit_state": edit_state},
            )
        except Exception:
            log.exception("Failed to write pending edit state for %s", metadata_path)

    def _remember_pending_edit_save_request(self, request: dict[str, Any]) -> None:
        save_image_key = request.get("context", {}).get("save_image_key")
        if save_image_key:
            self._pending_edit_save_requests[save_image_key] = request

    def _forget_pending_edit_save_request(
        self, save_image_key: Optional[str], request_id: Optional[str]
    ) -> None:
        if not save_image_key or not request_id:
            return
        request = self._pending_edit_save_requests.get(save_image_key)
        if request is None:
            return
        if request.get("context", {}).get("save_request_id") == request_id:
            self._pending_edit_save_requests.pop(save_image_key, None)

    def _pending_edit_metadata_path(self, fallback: Optional[Path]) -> Optional[Path]:
        if 0 <= self.current_index < len(self.image_files):
            return self.image_files[self.current_index].path
        return fallback

    def _get_sidecar_edit_state(self, image_path: Optional[Path]) -> Optional[dict]:
        if image_path is None:
            return None
        meta = self.sidecar.get_metadata(image_path, create=False)
        edit_state = getattr(meta, "edit_state", None) if meta is not None else None
        return edit_state if isinstance(edit_state, dict) else None

    def _edit_source_path_from_state(
        self, edit_state: Optional[dict], fallback: Path
    ) -> Path:
        if isinstance(edit_state, dict):
            source_path = edit_state.get("source_path")
            if source_path:
                candidate = Path(source_path)
                if not candidate.is_absolute():
                    candidate = self.image_dir / candidate
                try:
                    if candidate.exists():
                        return candidate
                except OSError:
                    pass
        return fallback

    def _active_edit_load_path(
        self, active_path: Path
    ) -> tuple[Path, Optional[dict[str, Any]]]:
        edit_state = self._get_pending_edit_state_for_loaded_path(active_path)
        return self._edit_source_path_from_state(edit_state, active_path), edit_state

    def _bind_loaded_editor_output_path(self, active_path: Path) -> None:
        try:
            active_mtime = active_path.stat().st_mtime
        except OSError:
            active_mtime = 0.0
        with self.image_editor._lock:
            self.image_editor.current_filepath = active_path
            self.image_editor.current_mtime = active_mtime

    @staticmethod
    def _float_source_to_pil(source_arr: np.ndarray) -> Image.Image:
        # Round: this rebuilds original_image from the float master, so it
        # must be the exact inverse of the /255.0 conversion, not a truncation.
        arr_u8 = np.rint(np.clip(source_arr, 0.0, 1.0) * 255.0).astype(np.uint8)
        return Image.fromarray(arr_u8, mode="RGB")

    def _restore_source_buffer_from_pending_request(
        self, request: dict[str, Any], active_path: Path
    ) -> bool:
        snapshot = request.get("snapshot", {})
        source_arr = snapshot.get("source_arr")
        if not isinstance(source_arr, np.ndarray):
            return False

        restored_source = np.array(source_arr, dtype=np.float32, copy=True)
        restored_original = self._float_source_to_pil(restored_source)
        main_exif = snapshot.get("main_exif")
        if main_exif:
            restored_original.info["exif"] = main_exif
        source_icc_bytes = snapshot.get("source_icc_bytes")
        if source_icc_bytes:
            restored_original.info["icc_profile"] = source_icc_bytes

        thumb = restored_original.copy()
        thumb.thumbnail(self.image_editor.preview_master_box)
        # Editor preview masters stay in source space. The display-only ICC or
        # saturation transform is applied after edits during preview rendering.
        preview_u8 = np.asarray(thumb.convert("RGB"), dtype=np.uint8)
        restored_preview = preview_u8.astype(np.float32)
        restored_preview *= np.float32(1.0 / 255.0)

        with self.image_editor._lock:
            self.image_editor.current_filepath = Path(
                snapshot.get("filepath_snapshot") or active_path
            )
            self.image_editor.source_filepath = Path(
                snapshot.get("source_filepath")
                or snapshot.get("filepath_snapshot")
                or active_path
            )
            self.image_editor.original_image = restored_original
            self.image_editor.float_image = restored_source
            self.image_editor.float_preview = restored_preview
            self.image_editor.bit_depth = int(
                snapshot.get("bit_depth") or self.image_editor.bit_depth
            )
            self.image_editor.current_mtime = float(
                snapshot.get("current_mtime") or self.image_editor.current_mtime
            )
            self.image_editor._source_exif_bytes = snapshot.get("source_exif")
            self.image_editor._cached_preview = None
            self.image_editor._cached_rev = -1
        return True

    def _get_pending_edit_state_for_loaded_path(
        self, active_path: Path
    ) -> Optional[dict[str, Any]]:
        metadata_path = self._pending_edit_metadata_path(active_path)
        if metadata_path is None:
            return None

        try:
            metadata_key = self._key(metadata_path)
        except (OSError, TypeError, ValueError):
            metadata_key = None
        request = (
            self._pending_edit_save_requests.get(metadata_key)
            if metadata_key is not None
            else None
        )
        if request is not None:
            snapshot_path = request.get("snapshot", {}).get("filepath_snapshot")
            if snapshot_path:
                try:
                    if self._key(Path(snapshot_path)) != self._key(active_path):
                        request = None
                except (OSError, TypeError, ValueError):
                    request = None
        if request is not None:
            return self._build_pending_edit_state(request)

        meta = self.sidecar.get_metadata(metadata_path, create=False)
        edit_state = getattr(meta, "edit_state", None) if meta is not None else None
        if not isinstance(edit_state, dict):
            return None
        if edit_state.get("status") not in ("pending_save", "saved"):
            return None

        target_path = edit_state.get("target_path")
        if target_path:
            try:
                if self._key(Path(target_path)) != self._key(active_path):
                    return None
            except (OSError, TypeError, ValueError):
                return None
        if not self._edit_state_has_replayable_source(edit_state, active_path):
            log.info(
                "Ignoring edit state with non-replayable source for %s",
                active_path,
            )
            return None
        if not self._saved_edit_state_matches_target(edit_state, active_path):
            log.info(
                "Ignoring saved edit state for %s: the file changed after "
                "FastStack saved it (external editor?)",
                active_path,
            )
            return None
        return edit_state

    def _restore_pending_edit_state_for_loaded_path(self, active_path: Path) -> bool:
        metadata_path = self._pending_edit_metadata_path(active_path)
        request = None
        if metadata_path is not None:
            try:
                metadata_key = self._key(metadata_path)
            except (OSError, TypeError, ValueError):
                metadata_key = None
            if metadata_key is not None:
                request = self._pending_edit_save_requests.get(metadata_key)
        if request is not None:
            snapshot_path = request.get("snapshot", {}).get("filepath_snapshot")
            if snapshot_path:
                try:
                    if self._key(Path(snapshot_path)) != self._key(active_path):
                        request = None
                except (OSError, TypeError, ValueError):
                    request = None

        if request is not None:
            self._restore_source_buffer_from_pending_request(request, active_path)

        edit_state = self._get_pending_edit_state_for_loaded_path(active_path)
        if edit_state is None:
            return False

        edits = self._deserialize_editor_edits(edit_state.get("edits"))
        masks = self._deserialize_mask_assets(edit_state.get("masks"))
        with self.image_editor._lock:
            self.image_editor.current_edits = edits
            self.image_editor._mask_assets.clear()
            self.image_editor._mask_assets.update(masks)
            self.image_editor._local_density_cache.clear()
            self.image_editor._edits_rev += 1
            self.image_editor._cached_preview = None
            self.image_editor._cached_rev = -1
            self.image_editor._cached_highlight_analysis = None
            self.image_editor._cached_detail_bands = None
            self.image_editor._cached_u8_lut = None
            self.image_editor._cached_u8_wb_lut = None

        self._active_auto_adjust_state = self._deserialize_auto_adjust_state(
            edit_state.get("auto_adjust"),
            active_path=active_path,
        )
        self._reset_local_brush_session_state()
        self._refresh_local_brush_ui()
        self._update_local_overlay()

        log.debug(
            "Restored %s edit state for %s (request %s)",
            edit_state.get("status", "saved"),
            active_path,
            edit_state.get("request_id"),
        )
        return True

    def _should_render_live_preview_full_resolution(self) -> bool:
        """Use full-res live previews after committed geometry edits."""
        if self.ui_state is None:
            return False
        if self.ui_state.isEditorOpen or self.ui_state.isCropping:
            return False
        return self._current_live_session_has_geometry_edits()

    def _is_current_live_edit_session_dirty(self) -> bool:
        """Return True when the current session has unsaved edits beyond the latest submitted save."""
        state = self._live_edit_session_state
        info = self._get_current_preview_session_provenance()
        if state is None or info is None:
            return False

        active_path_key, session_id, revision = info
        if state.active_path_key != active_path_key or state.session_id != session_id:
            return False
        if revision <= max(state.persisted_revision, state.submitted_revision):
            return False
        baseline_has_edits = (
            state.submitted_has_meaningful_edits
            if state.submitted_revision >= state.persisted_revision
            else state.persisted_has_meaningful_edits
        )
        return self._current_live_session_has_meaningful_edits() or baseline_has_edits

    @Slot(result=bool)
    def has_unsaved_edits(self) -> bool:
        """QML-callable: True when the editor has unsaved meaningful edits."""
        return self._is_current_live_edit_session_dirty()

    def _mark_current_live_edit_session_submitted(
        self, revision: int, has_meaningful_edits: bool
    ) -> None:
        """Record that a save request has been queued for the current session revision."""
        state = self._live_edit_session_state
        info = self._get_current_preview_session_provenance()
        if state is None or info is None:
            return

        active_path_key, session_id, _ = info
        if state.active_path_key == active_path_key and state.session_id == session_id:
            if revision >= state.submitted_revision:
                state.submitted_revision = revision
                state.submitted_has_meaningful_edits = has_meaningful_edits

    def _mark_current_live_edit_session_persisted(
        self, revision: int, has_meaningful_edits: bool
    ) -> None:
        """Record that the current session revision has been written to disk."""
        state = self._live_edit_session_state
        info = self._get_current_preview_session_provenance()
        if state is None or info is None:
            return

        active_path_key, session_id, _ = info
        if state.active_path_key == active_path_key and state.session_id == session_id:
            if revision >= state.persisted_revision:
                state.persisted_revision = revision
                state.persisted_has_meaningful_edits = has_meaningful_edits
            if state.persisted_revision > state.submitted_revision:
                state.submitted_revision = state.persisted_revision
                state.submitted_has_meaningful_edits = (
                    state.persisted_has_meaningful_edits
                )

    def _mark_current_live_edit_session_clean(self) -> None:
        """Treat the current revision as clean without writing a file."""
        info = self._get_current_preview_session_provenance()
        if info is None:
            self._live_edit_session_state = None
            return

        _, _, revision = info
        self._ensure_live_edit_session_state()
        state = self._live_edit_session_state
        if state is not None:
            state.persisted_revision = revision
            state.submitted_revision = revision
            state.persisted_has_meaningful_edits = False
            state.submitted_has_meaningful_edits = False

    def _mark_current_live_edit_session_save_failed(self, revision: int) -> None:
        """Rollback the submitted revision watermark when the latest save fails."""
        state = self._live_edit_session_state
        info = self._get_current_preview_session_provenance()
        if state is None or info is None:
            return

        active_path_key, session_id, _ = info
        if (
            state.active_path_key == active_path_key
            and state.session_id == session_id
            and revision >= state.submitted_revision
        ):
            state.submitted_revision = state.persisted_revision
            state.submitted_has_meaningful_edits = state.persisted_has_meaningful_edits

    def _note_latest_save_token(
        self,
        *,
        target: Optional[str],
        session_token: Any,
    ) -> None:
        """Record the newest captured save token for a target path."""
        if target and session_token is not None:
            self._latest_save_tokens[target] = session_token

    def _save_target_is_in_flight(self, target: Optional[str]) -> bool:
        """Return True when a save is already active for the resolved target path."""
        return bool(target and self._saves_in_flight.get(target, 0) > 0)

    def _reject_save_while_target_busy(
        self,
        *,
        target: Optional[str],
        session_token: Any,
        status_message: Optional[str],
    ) -> bool:
        """Decline a save when another request for the same target is already active.

        A declined request is not recorded as the target's latest save token:
        nothing was queued for it, so the in-flight save must keep its retry
        and failure reporting.
        """
        if not self._save_target_is_in_flight(target):
            return False

        log.info(
            "Skipping save request for %s; another save is already in flight", target
        )
        if status_message:
            self.update_status_message(status_message, timeout=3000)
        return True

    def _increment_save_tracking(
        self,
        *,
        target: Optional[str],
        save_image_key: Optional[str],
    ) -> None:
        """Increment in-flight save counters for the target path and image key."""
        if target:
            self._saves_in_flight[target] = self._saves_in_flight.get(target, 0) + 1
            key = self._key(Path(target))
            if key:
                with self._suppressed_paths_lock:
                    self._saving_path_keys[key] = self._saving_path_keys.get(key, 0) + 1
        if save_image_key:
            self._saving_keys[save_image_key] = (
                self._saving_keys.get(save_image_key, 0) + 1
            )

    def _open_save_tracking_for_request(self, request: dict[str, Any]) -> None:
        """Count *request* as in flight; paired with the release below."""
        context = request.get("context", {})
        target = context.get("target")
        save_image_key = context.get("save_image_key")
        self._increment_save_tracking(target=target, save_image_key=save_image_key)
        self._open_save_tracking[id(request)] = {
            "request": request,  # keeps id(request) unique while open
            "target": target,
            "save_image_key": save_image_key,
            "future": None,
        }

    def _release_save_tracking_for_request(self, request: Optional[dict]) -> bool:
        """Release *request*'s in-flight counters exactly once."""
        if request is None:
            return False
        entry = self._open_save_tracking.get(id(request))
        if entry is None or entry["request"] is not request:
            return False
        del self._open_save_tracking[id(request)]
        self._decrement_save_tracking(
            target=entry["target"], save_image_key=entry["save_image_key"]
        )
        return True

    def _reconcile_save_tracking_for_shutdown(self) -> None:
        """Release tracking for saves that finished while Qt delivery was stopped.

        Runs after the save executor drained. Their _saveFinished completions
        are queued behind the shutdown (or dropped by its guard), so without
        this the counters stay populated and the exit watchdog would never
        fire for a later hung shutdown step. A save whose future is somehow
        not settled keeps its tracking (and keeps the watchdog waiting).
        """
        for entry in list(self._open_save_tracking.values()):
            future = entry.get("future")
            if future is not None and not future.done():
                log.error(
                    "Save for %s has not settled after executor shutdown; "
                    "keeping it tracked",
                    entry.get("target"),
                )
                continue
            self._release_save_tracking_for_request(entry["request"])

    def _decrement_save_tracking(
        self,
        *,
        target: Optional[str],
        save_image_key: Optional[str],
    ) -> None:
        """Decrement in-flight save counters for the target path and image key."""
        if target:
            remaining = self._saves_in_flight.get(target, 0) - 1
            if remaining > 0:
                self._saves_in_flight[target] = remaining
            else:
                self._saves_in_flight.pop(target, None)
            key = self._key(Path(target))
            if key:
                with self._suppressed_paths_lock:
                    key_remaining = self._saving_path_keys.get(key, 0) - 1
                    if key_remaining > 0:
                        self._saving_path_keys[key] = key_remaining
                    else:
                        self._saving_path_keys.pop(key, None)
                        # Hand off to a timed window with no gap: watcher events
                        # for the write are still in flight from the OS.
                        self._suppressed_paths[key] = time.monotonic() + 3.0
        if save_image_key:
            remaining = self._saving_keys.get(save_image_key, 0) - 1
            if remaining > 0:
                self._saving_keys[save_image_key] = remaining
            else:
                self._saving_keys.pop(save_image_key, None)

    def _get_save_target_path_for_current_view(self) -> Optional[Path]:
        """Determine the target path for saving edits based on current view.

        If we are viewing a variant (backup or developed) via override, we want
        to save changes "as" the main image so that a NEW backup of the main
        image is created (Policy A).
        """
        if not self.view_override_path:
            return None

        # Policy Change: When editing a "developed" variant (e.g. from RawTherapee),
        # we want to save IN-PLACE (overwrite the developed file) rather than
        # overwriting the Main source file. This prevents accidental data loss/confusion.
        # Editing a "backup" still targets the Main file (restore behavior).
        if self.view_override_kind == "developed":
            return None

        if self.current_index is not None and 0 <= self.current_index < len(
            self.image_files
        ):
            return self.image_files[self.current_index].path
        return None

    def _prepare_current_session_save_request(
        self,
        *,
        editor_was_open: bool,
        success_message: Optional[str],
    ) -> Optional[dict[str, Any]]:
        """Capture an immutable save request for the current live editor session."""
        self._last_save_prepare_error = None
        if self.ui_state and getattr(self.ui_state, "isCropping", False) is True:
            self._last_save_prepare_error = "Apply or cancel the crop before saving"
            self.update_status_message(self._last_save_prepare_error)
            return None
        if self._crop_mode_has_saved_geometry:
            try:
                self._restore_crop_mode_geometry()
            except Exception:
                log.exception("Failed to restore stale crop transaction before save")
                self._last_save_prepare_error = (
                    "Failed to restore crop state before saving"
                )
                self.update_status_message(
                    self._last_save_prepare_error,
                    timeout=5000,
                )
                return None
            finally:
                self._clear_crop_mode_snapshot()
        if not self.image_editor.original_image:
            return None

        self._ensure_live_edit_session_state()
        session_info = self._get_current_preview_session_provenance()
        if session_info is None:
            return None

        _, _, save_revision = session_info
        live_state = self._live_edit_session_state
        has_meaningful_edits = self._current_live_session_has_meaningful_edits()
        baseline_has_meaningful_edits = bool(
            live_state
            and (
                live_state.submitted_has_meaningful_edits
                if live_state.submitted_revision >= live_state.persisted_revision
                else live_state.persisted_has_meaningful_edits
            )
        )
        if not has_meaningful_edits and not baseline_has_meaningful_edits:
            self._mark_current_live_edit_session_clean()
            return None
        if live_state is not None and save_revision <= live_state.submitted_revision:
            return None

        save_target_path = self._get_save_target_path_for_current_view()
        raw_target = save_target_path or self.image_editor.current_filepath
        effective_target = str(Path(raw_target).resolve()) if raw_target else None

        write_sidecar = self.current_edit_source_mode == "raw"
        dev_path = None
        if write_sidecar and 0 <= self.current_index < len(self.image_files):
            dev_path = self.image_files[self.current_index].developed_jpg_path

        save_image_key = (
            self._key(self.image_files[self.current_index].path)
            if 0 <= self.current_index < len(self.image_files)
            else None
        )
        session_token = (
            save_image_key,
            getattr(self, "view_override_kind", None),
            self.image_editor.session_id if self.image_editor else None,
            save_revision,
        )
        try:
            export_snapshot = self.image_editor.snapshot_for_export(
                write_developed_jpg=write_sidecar,
                developed_path=dev_path,
                save_target_path=save_target_path,
            )
            current_image_path = self._current_image_path()
            if (
                current_image_path is not None
                and isinstance(export_snapshot, dict)
                and stack_layers(export_snapshot.get("edits") or {})
            ):
                export_snapshot["stack_source_resolver"] = (
                    self._stack_source_resolver_for(current_image_path)
                )
        except RuntimeError as e:
            self._last_save_prepare_error = f"Failed to prepare save: {e}"
            log.warning(
                "Failed to capture save snapshot for %s: %s", effective_target, e
            )
            self.update_status_message(self._last_save_prepare_error, timeout=5000)
            return None
        except Exception as e:
            self._last_save_prepare_error = "Failed to prepare save"
            log.exception(
                "Unexpected error capturing save snapshot for %s: %s",
                effective_target,
                e,
            )
            self.update_status_message(self._last_save_prepare_error, timeout=5000)
            return None

        save_metadata_path = (
            self.image_files[self.current_index].path
            if 0 <= self.current_index < len(self.image_files)
            else save_target_path or self.image_editor.current_filepath
        )
        started_from_restore_override = (
            bool(self.view_override_path)
            and getattr(self, "view_override_kind", None) != "developed"
        )

        return {
            "snapshot": export_snapshot,
            "context": {
                "target": effective_target,
                "editor_was_open": editor_was_open,
                "save_action_type": "save_edit",
                "save_image_key": save_image_key,
                "save_revision": save_revision,
                "save_has_meaningful_edits": has_meaningful_edits,
                "save_request_id": uuid.uuid4().hex,
                "session_token": session_token,
                "edit_source_path": str(
                    export_snapshot.get("source_filepath")
                    or export_snapshot.get("filepath_snapshot")
                    or ""
                ),
                "auto_adjust_state": self._serialize_current_auto_adjust_state(),
                "save_directory_key": self._key(self.image_dir),
                "save_metadata_path": (
                    str(save_metadata_path) if save_metadata_path else None
                ),
                "started_from_restore_override": started_from_restore_override,
                "save_sidecar": self.sidecar,
                "success_message": success_message,
            },
        }

    @contextmanager
    def _critical_user_data_operation(self, label: str):
        """Expose destructive commit activity to the shutdown watchdog."""
        with self._critical_io_lock:
            self._critical_io_labels[label] = self._critical_io_labels.get(label, 0) + 1
        try:
            yield
        finally:
            with self._critical_io_lock:
                remaining = self._critical_io_labels.get(label, 1) - 1
                if remaining > 0:
                    self._critical_io_labels[label] = remaining
                else:
                    self._critical_io_labels.pop(label, None)

    def _critical_user_data_state(self) -> dict[str, int]:
        with self._critical_io_lock:
            return dict(self._critical_io_labels)

    def _run_critical_user_data_operation(self, label: str, operation, *args):
        with self._critical_user_data_operation(label):
            return operation(*args)

    def _submit_save_request_async(
        self,
        request: dict[str, Any],
        *,
        saving_status: Optional[str] = None,
    ) -> bool:
        """Submit a captured save request to the background save executor."""
        context = request["context"]
        export_snapshot = request["snapshot"]
        target = context.get("target")
        session_token = context.get("session_token")

        if self._reject_save_while_target_busy(
            target=target,
            session_token=session_token,
            status_message="Save already in progress for this image.",
        ):
            return False

        # Accepted: from here on this is the newest save for the target.
        self._note_latest_save_token(target=target, session_token=session_token)
        self._suppress_watcher_paths(target)
        self._open_save_tracking_for_request(request)
        # One governor token per accepted save execution, carried on the
        # context so it travels into the result dict. Encoding and file I/O
        # compete with speculative decoding for the same cores, and overlapping
        # saves each hold their own token, so capacity returns only when the
        # last one completes -- navigating away from the image does not
        # release it, and a duplicate completion callback releases a token the
        # governor has already forgotten.
        context["prefetch_constraint_token"] = self._enter_prefetch_constraint("save")
        self._mark_current_live_edit_session_submitted(
            context["save_revision"],
            bool(context.get("save_has_meaningful_edits", True)),
        )
        self.ui_state.isSaving = True
        if saving_status:
            self.ui_state.statusMessage = saving_status

        def do_save():
            try:
                with self._critical_user_data_operation("save"):
                    result = self.image_editor.save_from_snapshot(export_snapshot)
                return {
                    "success": True,
                    "result": result,
                    "request": request,
                    **context,
                }
            except RuntimeError as e:
                return {
                    "success": False,
                    "error": str(e),
                    "request": request,
                    **context,
                }
            except Exception as e:
                log.exception("Unexpected error during save: %s", e)
                return {
                    "success": False,
                    "error": "Failed to save image",
                    "request": request,
                    **context,
                }

        def on_done(future):
            if self._shutting_down:
                # _on_save_finished will never run, so release this save's
                # token here instead of relying on the prefetcher teardown.
                self._leave_prefetch_constraint(
                    context.get("prefetch_constraint_token")
                )
                return
            try:
                result = future.result()
            except Exception as e:
                result = {
                    "success": False,
                    "error": str(e),
                    "request": request,
                    **context,
                }
            self._saveFinished.emit(result)

        try:
            future = self._save_executor.submit(do_save)
        except Exception as e:
            self._leave_prefetch_constraint(
                context.pop("prefetch_constraint_token", None)
            )
            self._release_save_tracking_for_request(request)
            if not self._saves_in_flight:
                self.ui_state.isSaving = False
            self._mark_current_live_edit_session_save_failed(context["save_revision"])
            self.update_status_message(f"Failed to start background save: {e}")
            return False

        tracking = self._open_save_tracking.get(id(request))
        if tracking is not None:
            tracking["future"] = future
        self._remember_pending_edit_save_request(request)
        self._write_pending_edit_state_for_request(request)

        try:
            future.add_done_callback(on_done)
        except Exception as e:
            log.error(
                "Failed to register save callback; using fallback watcher thread: %s", e
            )

            def _fallback_watcher(fut=future):
                concurrent.futures.wait([fut])
                on_done(fut)

            t = threading.Thread(
                target=_fallback_watcher, daemon=True, name="SaveCallbackFallback"
            )
            t.start()

        return True

    def _run_save_request_sync(
        self,
        request: dict[str, Any],
        *,
        saving_status: Optional[str] = None,
    ) -> bool:
        """Run a captured save request synchronously on the main thread."""
        context = request["context"]
        export_snapshot = request["snapshot"]
        target = context.get("target")
        session_token = context.get("session_token")

        if self._reject_save_while_target_busy(
            target=target,
            session_token=session_token,
            status_message=(
                "Save already in progress for this image."
                if saving_status is not None
                else None
            ),
        ):
            if saving_status is None and target:
                self._pending_save_recovery[target] = request
                # Deferred, not dropped: it supersedes the in-flight save.
                self._note_latest_save_token(target=target, session_token=session_token)
                # This deferred request IS the newest revision for the target,
                # so it must be published with the same ordering guarantees as
                # a normally accepted async save. Without this, the persisted
                # pending edit state still names the older in-flight request,
                # and shutdown recovery's _mark_image_edited_in_sidecar() then
                # mistakes that stale marker for a *newer* pending request and
                # preserves it over the state it just saved -- leaving pixels
                # from this request beside recovery metadata from the previous
                # one. See FS-P1-002.
                self._remember_pending_edit_save_request(request)
                self._write_pending_edit_state_for_request(request)
                log.info(
                    "Deferring latest save for %s until shutdown recovery; "
                    "an older save is still in flight",
                    target,
                )
            return False

        self._note_latest_save_token(target=target, session_token=session_token)
        self._suppress_watcher_paths(target)
        self._open_save_tracking_for_request(request)
        # Same per-execution token as the async path; _on_save_finished below
        # releases it from the result dict regardless of outcome.
        context["prefetch_constraint_token"] = self._enter_prefetch_constraint("save")
        self._mark_current_live_edit_session_submitted(
            context["save_revision"],
            bool(context.get("save_has_meaningful_edits", True)),
        )
        self.ui_state.isSaving = True
        if saving_status:
            self.update_status_message(saving_status)

        try:
            with self._critical_user_data_operation("save"):
                result = self.image_editor.save_from_snapshot(export_snapshot)
            save_result = {
                "success": True,
                "result": result,
                "request": request,
                **context,
            }
        except RuntimeError as e:
            save_result = {
                "success": False,
                "error": str(e),
                "request": request,
                **context,
            }
        except Exception as e:
            log.exception("Unexpected error during save: %s", e)
            save_result = {
                "success": False,
                "error": "Failed to save image",
                "request": request,
                **context,
            }

        self._on_save_finished(save_result)
        return bool(save_result.get("success"))

    def _seed_decode_cache_from_live_preview(self, path) -> None:
        """Keep the just-edited pixels visible while their save is in flight.

        Navigation clears the live edit session, so the provider falls back to
        the decode cache — which still holds the PRE-EDIT pixels until the
        background save completes and the path is invalidated. Seeding the
        cache with the last rendered live preview means navigating back shows
        the edited image immediately instead of flashing the original; the
        save-completion invalidation then replaces it with the real decode.
        """
        if path is None or self.is_zoomed:
            return
        path = Path(path)
        with self._preview_lock:
            decoded = self._last_rendered_preview
            session_key = self._last_rendered_preview_session_key
        if decoded is None or session_key is None:
            return
        try:
            if session_key[0] != self._key(path):
                return
        except (OSError, TypeError, ValueError):
            return
        self._mark_optimistic_decode_seed_epoch(path)
        self.prefetcher.invalidate_path(path)
        _, _, display_gen = self.get_display_info()
        seed_quality = getattr(decoded, "quality", "cover")
        if seed_quality not in {"fast", "cover"}:
            seed_quality = "cover"
        self.image_cache[build_cache_key(path, display_gen, seed_quality)] = decoded

    def _flush_current_live_edit_session_for_navigation(self) -> bool:
        """Queue a background save for the current dirty session before switching images."""
        request = self._prepare_current_session_save_request(
            editor_was_open=False,
            success_message=None,
        )
        if request is None:
            return self._last_save_prepare_error is None
        self._seed_decode_cache_from_live_preview(
            request.get("context", {}).get("target")
        )
        return self._submit_save_request_async(request)

    def _flush_current_live_edit_session_for_drag(
        self,
        *,
        saving_status: Optional[str] = "Preparing drag...",
    ) -> bool:
        """Synchronously save the current dirty session before drag-out begins."""
        request = self._prepare_current_session_save_request(
            editor_was_open=False,
            success_message=None,
        )
        if request is None:
            return self._last_save_prepare_error is None
        return self._run_save_request_sync(request, saving_status=saving_status)

    @Slot(result=bool)
    def prepare_for_app_close(self) -> bool:
        """Flush the live session before allowing the main window to close."""
        if self._shutting_down:
            return True
        confirmed_token = getattr(self, "_close_discard_token", None)
        self._close_discard_token = None  # One quit attempt only.
        if (
            confirmed_token is not None
            and confirmed_token == self._close_discard_state()
        ):
            # The user already chose to quit without the edits that could not
            # be saved, and nothing was edited since; do not retry (and veto)
            # the same failing save again. Any later edit changes the state,
            # so it is saved (or prompts again) rather than silently dropped.
            self._shutdown_flush_prepared = True
            return True

        self._shutdown_flush_prepared = False
        request = self._prepare_current_session_save_request(
            editor_was_open=False,
            success_message=None,
        )
        if request is None:
            return self._last_save_prepare_error is None

        target = request.get("context", {}).get("target")
        if self._run_save_request_sync(request, saving_status=None):
            self._shutdown_flush_prepared = True
            return True
        if target and self._pending_save_recovery.get(target) is request:
            self._shutdown_flush_prepared = True
            return True
        return False

    @Slot()
    def discard_unsaved_edits_for_close(self) -> None:
        """Let the main window close after the final save failed.

        prepare_for_app_close() vetoes a close whose save fails (read-only
        folder, full disk, active crop). Without this escape a persistent
        failure would make the app impossible to quit normally.
        """
        log.warning("User chose to quit and discard edits that could not be saved")
        try:
            self.discard_edit_parameters()
        except Exception:
            log.exception("Failed to reset edits while discarding for close")
        self._close_discard_token = self._close_discard_state()

    def _close_discard_state(self) -> tuple:
        """Identify the exact edit state a discard-and-quit choice applies to."""
        editor = self.image_editor
        current_path = (
            self._key(self.image_files[self.current_index].path)
            if 0 <= self.current_index < len(self.image_files)
            else None
        )
        return (
            current_path,
            getattr(editor, "session_id", None) if editor else None,
            getattr(editor, "_edits_rev", None) if editor else None,
        )

    def _save_current_session_async(
        self, *, saving_status: str = "Saving..."
    ) -> tuple[Optional[dict[str, Any]], str]:
        """Submit the live session's edits to the background save executor.

        Returns ``(request, outcome)``. ``outcome`` is "submitted", "nothing"
        (no edits to save), "error" (the status bar already says why) or
        "busy" (a save for this image is still running; already reported).
        """
        close_after = self.ui_state.isEditorOpen and self.ui_state.isEditorExpanded
        request = self._prepare_current_session_save_request(
            editor_was_open=close_after,
            success_message="Image saved",
        )
        if request is None:
            if self._last_save_prepare_error is not None:
                return None, "error"
            return None, "nothing"
        if not self._submit_save_request_async(request, saving_status=saving_status):
            return None, "busy"
        if close_after:
            self.ui_state.isEditorOpen = False
        return request, "submitted"

    @Slot()
    def save_edited_image(self):
        """Save the current live editor session in the background."""
        _, outcome = self._save_current_session_async()
        if outcome == "nothing":
            self.update_status_message("No unsaved edits to save", timeout=3000)

    @Slot(object)
    def _on_save_finished(self, save_result: dict):
        """Handle save completion on main thread (called via signal from background)."""
        try:
            self._handle_save_finished(save_result)
        finally:
            if isinstance(save_result, dict):
                self._continue_image_editor_after_save(save_result)

    def _handle_save_finished(self, save_result: dict):
        # Released first and unconditionally, before the shutdown guard: this
        # token belongs to THIS save execution only, so a duplicate or stale
        # completion cannot restore capacity another in-flight save still needs.
        self._leave_prefetch_constraint(save_result.get("prefetch_constraint_token"))
        # Tracking is also released before the shutdown guard (idempotently,
        # per request): a completion delivered during shutdown must not leave
        # _saves_in_flight populated, or the exit watchdog waits forever.
        tracked_request = save_result.get("request")
        if tracked_request is not None:
            self._release_save_tracking_for_request(tracked_request)
        else:
            # Request-less result (legacy/synthetic): release by target/key.
            self._decrement_save_tracking(
                target=save_result.get("target"),
                save_image_key=save_result.get("save_image_key"),
            )
        # Guard against callbacks during/after shutdown
        if self._shutting_down:
            return

        # The in-flight set was updated above; clear the saving indicator
        # only when no exports remain in progress.
        target = save_result.get("target")
        save_key = save_result.get("save_image_key")
        if not self._saves_in_flight:
            self.ui_state.isSaving = False

        save_session_token = save_result.get("session_token")
        current_image_key = (
            self._key(self.image_files[self.current_index].path)
            if 0 <= self.current_index < len(self.image_files)
            else None
        )
        current_session_token = (
            current_image_key,
            getattr(self, "view_override_kind", None),
            self.image_editor.session_id if self.image_editor else None,
            getattr(self.image_editor, "_edits_rev", None),
        )
        still_on_same_session = (
            save_session_token is not None
            and current_session_token is not None
            and len(save_session_token) >= 3
            and current_session_token[:3] == save_session_token[:3]
        )
        still_on_identical_revision = (
            still_on_same_session
            and len(save_session_token) >= 4
            and current_session_token[3] == save_session_token[3]
        )
        save_revision = save_result.get("save_revision")

        reported_result = save_result.get("result")
        valid_result = (
            isinstance(reported_result, tuple)
            and len(reported_result) == 2
            and reported_result[0] is not None
            and reported_result[1] is not None
        )
        if save_result.get("success") and not valid_result:
            # A save is not successful until both the output and its backup
            # exist as a valid result. Route malformed/empty payloads through
            # the regular failure path so submitted revisions are rolled back
            # and the captured snapshot can be retried safely.
            log.error(
                "Save reported success with an invalid result: %r", reported_result
            )
            save_result["success"] = False
            save_result["error"] = "Save did not produce a valid output and backup"

        if not save_result.get("success"):
            if still_on_same_session and save_revision is not None:
                self._mark_current_live_edit_session_save_failed(int(save_revision))
            error_msg = save_result.get("error", "Save failed")
            request = save_result.get("request")
            target = save_result.get("target")
            attempts = (
                int(request.get("attempts", 1)) if isinstance(request, dict) else 1
            )
            latest_save_token = (
                self._latest_save_tokens.get(target)
                if target and save_session_token is not None
                else None
            )
            if (
                latest_save_token is not None
                and latest_save_token != save_session_token
            ):
                log.info(
                    "Skipping retry for stale save request %s (rev=%s); newer save token exists",
                    target,
                    save_revision,
                )
                return
            # The captured snapshot is fully self-contained — auto-retry once
            # before declaring permanent failure. If the user has navigated away
            # we still need to preserve the snapshot so edits aren't lost.
            if isinstance(request, dict) and attempts < 2:
                request["attempts"] = attempts + 1
                log.warning(
                    "Background save failed for %s (attempt %d): %s — retrying",
                    target,
                    attempts,
                    error_msg,
                )
                self._submit_save_request_async(request)
                return
            if isinstance(request, dict) and target and not still_on_same_session:
                self._pending_save_recovery[target] = request
                log.error(
                    "Background save for %s failed permanently after %d attempts: %s — "
                    "queued for shutdown flush",
                    target,
                    attempts,
                    error_msg,
                )
                self.update_status_message(
                    f"Background save failed for {Path(target).name}: {error_msg}. "
                    "Will retry on quit.",
                    timeout=10000,
                )
            else:
                self.update_status_message(f"Save failed: {error_msg}", timeout=5000)
            # Drop the optimistic live-preview cache seed (if any) so the
            # display falls back to the file's actual pixels.
            if target:
                self._invalidate_decoded_path(target, force=True)
            return

        result = save_result.get("result")
        if isinstance(result, tuple) and len(result) == 2:
            saved_path, backup_path = result
            # Success — drop any prior recovery snapshot for this target and
            # keep the watcher quiet for the save-generated file churn.
            target = save_result.get("target")
            if target and target in self._pending_save_recovery:
                self._pending_save_recovery.pop(target, None)
            self._suppress_watcher_paths(target, saved_path)

            # --- Post-Save Cleanup ---

            # Read frozen save context — these were captured at save-initiation
            # time and are immune to editor/navigation changes during the save.
            editor_was_open = save_result.get("editor_was_open", False)
            if still_on_same_session and save_revision is not None:
                self._mark_current_live_edit_session_persisted(
                    int(save_revision),
                    bool(save_result.get("save_has_meaningful_edits", True)),
                )

            if still_on_same_session:
                # 1. Editor Cleanup (only if revision is unchanged)
                if still_on_identical_revision:
                    if editor_was_open:
                        if self.ui_state.isEditorOpen:
                            self.ui_state.isEditorOpen = False
                        # Closing triggers _on_editor_open_changed -> image_editor.clear()
                        # but we call it explicitly here just in case they closed it manually.
                        self._clear_active_auto_adjust_state(
                            "background save replaced the live editor session",
                            clear_editor=False,
                        )
                        self.image_editor.clear()

                    # Also clear variant override if we started from one
                    if save_result.get("started_from_restore_override"):
                        self._clear_variant_override()

            # Record current path to stay on it after refresh (since index may shift)
            preserved_path = None
            if 0 <= self.current_index < len(self.image_files):
                preserved_path = self.image_files[self.current_index].path

            # 1. Update sidecar metadata FIRST so all following refreshes see it
            if saved_path:
                save_sidecar = save_result.get("save_sidecar") or self.sidecar
                metadata_path = (
                    Path(save_result["save_metadata_path"])
                    if save_result.get("save_metadata_path")
                    else saved_path
                )
                saved_edit_state = self._build_saved_edit_state_for_result(
                    save_result,
                    saved_path=saved_path,
                    backup_path=backup_path,
                )
                metadata_before = self._mark_image_edited_in_sidecar(
                    save_sidecar,
                    metadata_path,
                    completed_edit_state_request_id=save_result.get("save_request_id"),
                    saved_edit_state=saved_edit_state,
                    clear_saved_edit_state=not bool(
                        save_result.get("save_has_meaningful_edits", True)
                    ),
                )
                save_directory_key = save_result.get("save_directory_key")
                current_directory_key = self._key(self.image_dir)
                should_record_undo = backup_path and (
                    save_directory_key is None
                    or save_directory_key == current_directory_key
                )
                if should_record_undo:
                    action_type = save_result.get("save_action_type", "save_edit")
                    self.undo_history.append(
                        (
                            action_type,
                            self._build_edit_undo_data(
                                saved_path,
                                backup_path,
                                metadata_path=metadata_path,
                                metadata_before=metadata_before,
                                sidecar=save_sidecar,
                            ),
                            time.time(),
                        )
                    )
                elif backup_path:
                    log.info(
                        "Skipping save_edit undo for %s after directory change: %s -> %s",
                        saved_path,
                        save_directory_key,
                        current_directory_key,
                    )

            # 2. Update variants and re-select index. A save can finish while
            # navigation is decoding/presenting another target, so invalidate
            # that paced meaning before rebuilding the visible list.
            self._begin_direct_image_transition("save completion rebuilt the list")
            self.refresh_image_list()

            if still_on_same_session:
                # Still viewing the saved image — pin index and force sync.
                # Only the saved file's pixels changed; invalidate it alone
                # instead of clearing the whole decode cache.
                self._reindex_after_save(saved_path)
                self._invalidate_decoded_path(saved_path, force=True)
                self.prefetcher.update_prefetch(self.current_index)
                self.sync_ui_state()
            else:
                # User navigated away — re-find new index for preserved_path and drop stale cache
                if preserved_path:
                    self._reindex_after_save(preserved_path)
                if saved_path:
                    self._invalidate_decoded_path(saved_path, force=True)

            self._restart_quality_decode_timer()

            # Auto-add to batch if enabled
            if saved_path:
                auto_add_path = (
                    Path(save_result["save_metadata_path"])
                    if save_result.get("save_metadata_path")
                    else Path(saved_path)
                )
                self._auto_add_edited_to_batch_if_enabled(auto_add_path)

            if self.ui_state:
                self.ui_state.variantBadgesChanged.emit()

            self._forget_pending_edit_save_request(
                save_key, save_result.get("save_request_id")
            )

            success_message = save_result.get("success_message")
            metadata_sidecar = save_result.get("save_sidecar") or self.sidecar
            metadata_dirty = getattr(metadata_sidecar, "dirty", False) is True
            if success_message and not metadata_dirty:
                self.update_status_message(success_message)
            elif success_message:
                self.update_status_message(
                    "Image saved; metadata is retained for retry.",
                    timeout=8000,
                    color="#FFD54F",
                )
        else:
            # Success reported but result shape unexpected
            log.warning("Save finished with unexpected result shape: %r", result)
            self.update_status_message(
                "Save finished, but the result payload was malformed.", timeout=5000
            )
