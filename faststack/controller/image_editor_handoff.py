"""Handing the current image to the external image editor (Photoshop, GIMP)
and the unsaved-edits prompt around it.

Mixed into AppController (faststack/app.py); every method runs on the
controller instance.
"""

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import (
    Slot,
)
from PySide6.QtWidgets import (
    QApplication,
)

from faststack.config import DEFAULT_CONFIG, config, detect_image_editor_path
from faststack.io.arguments import parse_external_arguments
from faststack.io.child_process import child_popen_kwargs, reap_in_background
from faststack.io.executable_validator import validate_executable_path
from faststack.io.indexer import RAW_EXTENSIONS

log = logging.getLogger(__name__)


_PHOTOSHOP_HANDOFF_MAX_AGE_SECONDS = 24 * 60 * 60


@dataclass
class PhotoshopHandoffState:
    jpg_path: Path
    baseline_fingerprint: Optional[tuple[int, int, int, int, int]]
    launched_at: float


class ImageEditorHandoffMixin:
    def _track_photoshop_handoff(
        self,
        jpg_path: Path,
    ) -> None:
        """Remember the JPG that Photoshop is expected to overwrite."""
        key = self._key(jpg_path)
        if not key:
            return
        self._photoshop_handoffs[key] = PhotoshopHandoffState(
            jpg_path=jpg_path,
            baseline_fingerprint=self._file_state_fingerprint(jpg_path),
            launched_at=time.monotonic(),
        )

    def _consume_photoshop_overwrites(self, changed_paths: set) -> list[Path]:
        """Return Photoshop handoffs whose JPG now differs from their baseline."""
        if not changed_paths:
            return []

        cutoff = time.monotonic() - _PHOTOSHOP_HANDOFF_MAX_AGE_SECONDS
        self._photoshop_handoffs = {
            key: state
            for key, state in self._photoshop_handoffs.items()
            if state.launched_at >= cutoff
        }

        overwritten: list[Path] = []
        for changed_path in changed_paths:
            if changed_path is None:
                continue
            key = self._key(changed_path)
            state = self._photoshop_handoffs.get(key)
            if state is None:
                continue
            current = self._file_state_fingerprint(state.jpg_path)
            if current is None or current == state.baseline_fingerprint:
                continue
            overwritten.append(state.jpg_path)
            self._photoshop_handoffs.pop(key, None)
        return overwritten

    def _persist_photoshop_overwrite_batches(self, paths: list[Path]) -> int:
        """Persist auto-batch flags before the watcher rebuilds runtime ranges."""
        if not paths or not self.ui_state.autoAddEditedToBatch:
            return 0

        added = 0
        for path in paths:
            meta = self.sidecar.get_metadata(path)
            if not meta.batch:
                meta.batch = True
                added += 1
        if added:
            if not self._persist_sidecar():
                return 0
        return added

    def _retire_pending_saves_for_photoshop(
        self,
        image_path: Path,
        handoff_path: Path,
        *,
        dry_run: bool = False,
    ) -> bool:
        """Drop deferred FastStack saves when Photoshop takes ownership.

        With ``dry_run`` nothing is changed; the return value says whether
        there is anything that would be dropped.
        """
        image_key = self._key(image_path)
        handoff_key = self._key(handoff_path)
        handoff_keys = {key for key in (image_key, handoff_key) if key is not None}

        def request_matches(request: Any, fallback_target: Any = None) -> bool:
            if not isinstance(request, dict):
                return False
            context = request.get("context", {})
            if image_key is not None and context.get("save_image_key") == image_key:
                return True
            target = context.get("target") or fallback_target
            try:
                return bool(target and self._key(Path(target)) in handoff_keys)
            except (OSError, TypeError, ValueError):
                return False

        retired_requests = 0
        for save_image_key, request in list(self._pending_edit_save_requests.items()):
            if save_image_key in handoff_keys or request_matches(request):
                if not dry_run:
                    self._pending_edit_save_requests.pop(save_image_key, None)
                retired_requests += 1

        for target, request in list(self._pending_save_recovery.items()):
            if request_matches(request, target):
                if not dry_run:
                    self._pending_save_recovery.pop(target, None)
                retired_requests += 1

        cleared_persisted_state = False
        cleanup_saved = True
        metadata_paths: dict[str, Path] = {}
        for path in (image_path, handoff_path):
            metadata_key = self.sidecar.metadata_key_for_path(path)
            if metadata_key:
                metadata_paths.setdefault(metadata_key, path)
        for path in metadata_paths.values():
            meta = self.sidecar.get_metadata(path, create=False)
            edit_state = getattr(meta, "edit_state", None) if meta is not None else None
            if (
                isinstance(edit_state, dict)
                and edit_state.get("status") == "pending_save"
            ):
                if not dry_run:
                    meta.edit_state = None
                cleared_persisted_state = True
        if dry_run:
            return bool(retired_requests or cleared_persisted_state)
        if cleared_persisted_state:
            cleanup_saved = self._persist_sidecar()

        if retired_requests or cleared_persisted_state:
            log.info(
                "Image editor handoff retired %d deferred save request(s)%s",
                retired_requests,
                " and persisted pending edit state" if cleared_persisted_state else "",
            )
        return bool(retired_requests or cleared_persisted_state) and cleanup_saved

    def _discard_current_live_edits_for_photoshop(self) -> bool:
        """Discard dirty FastStack state after handing the image to Photoshop."""
        if not self._is_current_live_edit_session_dirty():
            return False

        if self.ui_state.isCropping:
            self.ui_state.isCropRotating = False
            self.ui_state.isCropping = False
            self._clear_crop_mode_snapshot()

        self._clear_active_auto_adjust_state(
            "Image editor handoff discarded unsaved FastStack edits",
            clear_editor=False,
        )
        if self.image_editor:
            self.image_editor.reset_edits()
        if hasattr(self.ui_state, "reset_editor_state"):
            self.ui_state.reset_editor_state()

        self._clear_live_edit_session_state()
        self._hq_preview_timer.stop()
        with self._preview_lock:
            self._preview_token += 1
            self._preview_pending = False
            cancel_event = self._preview_refinement_cancel
            if cancel_event is not None:
                cancel_event.set()
            self._clear_last_rendered_preview_locked()

        if self.ui_state.isEditorOpen:
            self.ui_state.isEditorOpen = False
        elif self.image_editor:
            self.image_editor.clear()

        if 0 <= self.current_index < len(self.image_files):
            current_path = self.image_files[self.current_index].path
            self._invalidate_decoded_path(current_path, force=True)
        self.prefetcher.update_prefetch(self.current_index)
        self.sync_ui_state()
        if self.ui_state.isHistogramVisible:
            self.update_histogram()

        log.info("Discarded unsaved FastStack edits for image editor handoff")
        return True

    def _resolve_image_editor_path(self) -> Optional[str]:
        """Return the configured image editor, or detect one if it is missing.

        A configured path that exists is always used. Otherwise look for
        Photoshop, then GIMP. Remember the detected path only when the
        configured value is empty or the bundled default placeholder, so a
        temporarily unavailable custom path remains saved.
        """
        configured = config.get("photoshop", "exe")
        if configured:
            expanded = os.path.expanduser(os.path.expandvars(configured.strip('" ')))
            if os.path.isfile(expanded):
                return expanded

        detected = detect_image_editor_path()
        if detected:
            log.info(
                "Image editor path %r not found; using detected %s",
                configured,
                detected,
            )
            if not configured or configured == DEFAULT_CONFIG["photoshop"]["exe"]:
                config.set("photoshop", "exe", detected)
                config.save()
        return detected

    @Slot()
    def edit_in_photoshop(self):
        self._edit_in_image_editor(confirm_unsaved=True)

    def _image_editor_prompt_image_key(self) -> Optional[str]:
        if not (0 <= self.current_index < len(self.image_files)):
            return None
        return self._key(self.image_files[self.current_index].path)

    def _prompt_before_image_editor_handoff(
        self,
        image_path: Path,
        jpg_path: Path,
        editor_path: Path,
    ) -> bool:
        """Ask before a handoff would discard FastStack edits. True = asked."""
        live_dirty = self._is_current_live_edit_session_dirty()
        other_pending = self._retire_pending_saves_for_photoshop(
            image_path,
            jpg_path,
            dry_run=True,
        )
        if not (live_dirty or other_pending):
            return False

        self._image_editor_prompt = {
            "image_key": self._key(image_path),
            "can_save": live_dirty,
        }
        message = (
            "This image has FastStack edits that are not saved. The image "
            "editor saves over the JPG, so FastStack has to drop unsaved "
            "edits when it hands the image over."
        )
        if live_dirty and editor_path != jpg_path:
            message += (
                f"\n\nThe editor will open {editor_path.name}, so saved "
                "FastStack edits will not show up there."
            )
        self.ui_state.imageEditorUnsavedPromptRequested.emit(message, live_dirty)
        return True

    @Slot(str)
    def resolve_image_editor_unsaved_prompt(self, choice: str) -> None:
        """QML: the user's answer to the unsaved-edits handoff prompt.

        ``choice`` is "save", "discard" or "cancel".
        """
        prompt = getattr(self, "_image_editor_prompt", None)
        self._image_editor_prompt = None
        if prompt is None or choice not in ("save", "discard"):
            return
        if self._image_editor_prompt_image_key() != prompt.get("image_key"):
            self.update_status_message(
                "The current image changed, so the image editor was not opened.",
                timeout=4000,
            )
            return
        if choice == "discard":
            self._edit_in_image_editor(confirm_unsaved=False)
            return
        if not prompt.get("can_save"):
            return

        request, outcome = self._save_current_session_async(
            saving_status="Saving before opening the image editor..."
        )
        if request is None:
            if outcome == "nothing":
                # Nothing left to save (the edits were neutral); hand over.
                self._edit_in_image_editor(confirm_unsaved=False)
            return
        context = request["context"]
        self._image_editor_after_save = {
            "request_id": context.get("save_request_id"),
            "image_keys": {
                key
                for key in (
                    prompt.get("image_key"),
                    (
                        self._key(Path(context["target"]))
                        if context.get("target")
                        else None
                    ),
                )
                if key
            },
        }

    def _continue_image_editor_after_save(self, save_result: dict) -> None:
        """Launch the image editor once its "Save first" save has finished."""
        pending = getattr(self, "_image_editor_after_save", None)
        if pending is None or save_result.get("save_request_id") != pending.get(
            "request_id"
        ):
            return
        if self._shutting_down:
            self._image_editor_after_save = None
            return
        if not save_result.get("success"):
            if self._save_target_is_in_flight(save_result.get("target")):
                return  # The save handler queued its automatic retry.
            self._image_editor_after_save = None
            self.update_status_message(
                "Save failed, so the image editor was not opened.",
                timeout=5000,
            )
            return
        self._image_editor_after_save = None
        if self._image_editor_prompt_image_key() not in pending.get("image_keys", ()):
            self.update_status_message(
                "Image saved. The image editor was not opened because the "
                "current image changed.",
                timeout=5000,
            )
            return
        # Ask again if new edits were made while the save ran.
        self._edit_in_image_editor(confirm_unsaved=True)

    def _edit_in_image_editor(self, *, confirm_unsaved: bool) -> None:
        if not self.image_files:
            self.update_status_message("No image to edit.")
            return

        current_image_path = self.image_files[self.current_index].path
        if self._block_if_saving(current_image_path):
            return

        # Prefer RAW file if it exists, otherwise use JPG
        image_file = self.image_files[self.current_index]
        jpg_path = image_file.path
        if jpg_path.suffix.lower() in RAW_EXTENSIONS:
            # RAW-only entries have no JPEG path in the index yet. Photoshop's
            # handoff target (also copied to the clipboard) is the same-stem
            # JPEG that will replace the RAW-only entry after the watcher scan.
            jpg_path = jpg_path.with_suffix(".jpg")

        # Handle backup images: strip -backup, -backup2, -backup-1, etc. to find original RAW
        original_stem = jpg_path.stem
        # Remove -backup with optional digits or -backup-digits (handles both formats)
        original_stem = re.sub(
            r"-backup(-?\d+)?$", "", original_stem, flags=re.IGNORECASE
        )

        # Look for RAW file with the original stem
        raw_path = None
        if image_file.raw_pair and image_file.raw_pair.exists():
            # Use the paired RAW if it exists
            raw_path = image_file.raw_pair
        else:
            # Search for RAW file manually by original stem. Cameras usually
            # write upper-case extensions (.ORF, .CR2), which a case-sensitive
            # filesystem will not match against the lower-case table.
            for ext in RAW_EXTENSIONS:
                for candidate_ext in dict.fromkeys((ext, ext.upper())):
                    potential_raw = jpg_path.parent / f"{original_stem}{candidate_ext}"
                    if potential_raw.exists():
                        raw_path = potential_raw
                        break
                if raw_path is not None:
                    break

        if raw_path and raw_path.exists():
            current_image_path = raw_path
            log.info("Using RAW file for image editor: %s", raw_path)
        else:
            current_image_path = jpg_path
            log.info(
                "Using JPG file for image editor (no RAW found): %s", current_image_path
            )

        photoshop_exe = self._resolve_image_editor_path()
        photoshop_args = config.get("photoshop", "args")
        if not photoshop_exe:
            self.update_status_message(
                "No image editor found. Set the Image Editor path in Settings."
            )
            log.error("No image editor configured or detected")
            return

        # GIMP has no native camera-RAW support (it only redirects to a RAW
        # developer plug-in when one is installed), so hand it the JPG. Keep the
        # RAW for RAW-only entries, whose same-stem JPG does not exist yet.
        editor_name = Path(photoshop_exe).name.lower()
        is_gimp = editor_name.startswith("gimp") or editor_name.startswith("org.gimp.")
        if is_gimp and current_image_path != jpg_path and jpg_path.is_file():
            current_image_path = jpg_path
            log.info("Using JPG file for GIMP: %s", current_image_path)

        # Validate executable path securely
        is_valid, error_msg = validate_executable_path(
            photoshop_exe, app_type="image_editor", allow_custom_paths=True
        )

        if not is_valid:
            self.update_status_message(f"Image editor validation failed: {error_msg}")
            log.error("Image editor executable validation failed: %s", error_msg)
            return

        # Validate that the file path exists and is a file
        if not current_image_path.exists() or not current_image_path.is_file():
            self.update_status_message(
                f"Image file not found: {current_image_path.name}"
            )
            log.error("Image file not found or not a file: %s", current_image_path)
            return

        if confirm_unsaved and self._prompt_before_image_editor_handoff(
            image_file.path,
            jpg_path,
            current_image_path,
        ):
            return

        try:
            # Build command list safely
            command = [photoshop_exe]

            # Parse additional args safely using shlex (handles quotes and escapes properly)
            if photoshop_args:
                try:
                    parsed_args = parse_external_arguments(photoshop_args)
                    command.extend(parsed_args)
                except ValueError as e:
                    log.error("Invalid image editor args format: %s", e)
                    self.update_status_message(
                        "Invalid image editor arguments configured"
                    )
                    return

            # Add the file path as the last argument
            # Convert to string but keep it as a list element (not shell-interpolated)
            command.append(str(current_image_path.resolve()))

            # SECURITY: Explicitly disable shell execution
            process = subprocess.Popen(
                command,
                shell=False,  # CRITICAL: Never use shell=True with user input
                stdin=subprocess.DEVNULL,
                close_fds=True,  # Close unused file descriptors
                **child_popen_kwargs(),
            )
            reap_in_background(process, Path(photoshop_exe).name)

            # Photoshop owns the image after handoff. Do not queue a competing
            # FastStack save; retire dirty in-memory edits so navigation or app
            # shutdown cannot write them over Photoshop's later JPEG output.
            self._track_photoshop_handoff(jpg_path)
            retired_pending_saves = self._retire_pending_saves_for_photoshop(
                image_file.path,
                jpg_path,
            )
            discarded_live_edits = self._discard_current_live_edits_for_photoshop()
            discarded_faststack_edits = retired_pending_saves or discarded_live_edits

            jpg_clipboard_path = str(jpg_path)
            QApplication.clipboard().setText(jpg_clipboard_path)
            log.info(
                "Copied JPG path to clipboard after image editor launch: %s",
                jpg_clipboard_path,
            )

            # Mark as edited on successful launch
            today = datetime.now().strftime("%Y-%m-%d")
            meta = self.sidecar.get_metadata(image_file.path)
            meta.edited = True
            meta.edited_date = today
            metadata_saved = self._persist_sidecar()
            self._metadata_cache_index = (-1, -1)
            self.dataChanged.emit()
            self.sync_ui_state()

            if discarded_faststack_edits:
                status = (
                    f"Opened {current_image_path.name} in image editor. "
                    "Discarded unsaved FastStack edits. Copied JPG path."
                )
            else:
                status = f"Opened {current_image_path.name} in image editor. Copied JPG path."
            if metadata_saved:
                self.update_status_message(status)
            log.info("Launched image editor with: %s", command)
        except FileNotFoundError as e:
            self.update_status_message(f"Image editor executable not found: {e}")
            log.exception("Image editor executable not found")
            # Don't mark as edited if launch failed
            return
        except (OSError, subprocess.SubprocessError) as e:
            self.update_status_message(f"Failed to open in image editor: {e}")
            log.exception("Error launching image editor")
            # Don't mark as edited if launch failed
            return
