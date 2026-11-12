"""Halo removal brush: paint over a focus-stacking halo, FastStack removes it.

Mixed into AppController (faststack/app.py); every method runs on the
controller instance.  The algorithm is ``faststack.imaging.halo_fix``; the
inpainting runtime and model are downloaded on first use
(``faststack.ai.inpaint``), after the owner agrees to a prompt that says
what is downloaded, how big it is, where it goes and what it does.

Flow:

* ``toggle_halo_brush_mode`` enters brush mode with ``brushTool == "halo"``,
  or shows the download prompt first when the model is not installed.
* Strokes go onto one halo layer (``{"halo": True}`` in
  ``current_edits["local_layers"]``) through the shared brush code in
  app.py; erasing (right/Alt-drag) erases from every visible halo layer.
* After every committed change ``_schedule_halo_jobs`` (called from
  ``_after_local_edit``) submits a correction for each halo layer whose
  strokes, or the stack paint beneath them, changed to a single daemon
  worker.  Corrections are computed from the stack-painted photo, so they
  keep stack paint under a halo stroke.  Results come back through
  ``_haloResultReady`` and are kept only if the image, load session and the
  layer's signature are still the ones they were computed from.  A layer
  that failed is not retried until its signature changes, and shows no
  correction (as a save would leave it out).
* The render path only blends cached results; a save computes anything
  still missing on the save worker (``ImageEditor._halo_results_for_export``)
  and leaves out a layer it cannot compute.
"""

from __future__ import annotations

import concurrent.futures
import copy
import logging
import threading
from typing import Any, Optional

from PySide6.QtCore import Slot

from faststack.ai import inpaint as ai_inpaint
from faststack.imaging.halo_fix import (
    HaloFixError,
    compute_halo_fix,
    halo_base,
    halo_layers,
    halo_signature,
    is_halo_layer,
)
from faststack.imaging.stack_paint import stack_layers
from faststack.imaging.mask import MaskData

log = logging.getLogger(__name__)


def _mb(n: int) -> int:
    return int(round(n / 1_000_000))


class HaloRemovalMixin:
    """Controller half of the halo removal brush."""

    # ---- setup -----------------------------------------------------------

    def _init_halo_removal(self) -> None:
        from faststack.util.executors import create_daemon_threadpool_executor

        # One worker: corrections are CPU-heavy and LaMa is not re-entrant.
        self._halo_executor = create_daemon_threadpool_executor(
            max_workers=1, thread_name_prefix="HaloFix"
        )
        self._halo_install_executor = create_daemon_threadpool_executor(
            max_workers=1, thread_name_prefix="HaloInstall"
        )
        # layer id -> signature currently being computed
        self._halo_jobs: dict[str, tuple] = {}
        # layer id -> (signature that failed, message); not retried until the
        # layer's signature changes.
        self._halo_errors: dict[str, tuple[tuple, str]] = {}
        # Filesystem probes; only an install (or a model that turns out to be
        # missing) changes this, so it is not re-read on every edit.
        self._halo_install_status = ai_inpaint.install_status()
        # Bumped on navigation; results for an older token are dropped.
        self._halo_token = 0
        self._halo_install_cancel = threading.Event()
        self._halo_install_running = False
        self._haloResultReady.connect(self._on_halo_result_ready)
        self._haloInstallProgress.connect(self._on_halo_install_progress)
        self._haloInstallDone.connect(self._on_halo_install_done)
        self._set_halo_state()
        if not self._halo_install_status.ready:
            # A download the app quit in the middle of left files behind.
            try:
                self._halo_install_executor.submit(ai_inpaint.sweep_leftovers)
            except RuntimeError:
                pass

    def _shutdown_halo_removal(self) -> None:
        self._halo_install_cancel.set()
        for name in ("_halo_executor", "_halo_install_executor"):
            executor = getattr(self, name, None)
            if executor is not None:
                self._safe_shutdown_executor(executor, name, wait=False)

    def _refresh_halo_install_status(self) -> ai_inpaint.InstallStatus:
        self._halo_install_status = ai_inpaint.install_status()
        return self._halo_install_status

    def _set_halo_state(self, **changes: Any) -> None:
        """Merge *changes* into ``uiState.haloState`` (with the cached install
        figures); assigns only when something changed."""
        old = self.ui_state.haloState or {}
        state = dict(old)
        status = self._halo_install_status
        state.update(
            installed=status.ready,
            runtimeNeeded=not status.runtime_ready,
            modelNeeded=not status.model_ready,
            runtimeVersion=ai_inpaint.ORT_VERSION,
            downloadMB=_mb(status.download_bytes),
            diskMB=_mb(status.disk_bytes),
            folder=status.folder,
            busy=bool(self._halo_jobs),
        )
        state.setdefault("promptVisible", False)
        state.setdefault("downloading", False)
        state.setdefault("progress", 0.0)
        state.setdefault("progressText", "")
        state.setdefault("error", "")
        state.update(changes)
        if state != old:
            self.ui_state.haloState = state

    # ---- entering the tool / first-use download -------------------------

    @Slot()
    def toggle_halo_brush_mode(self) -> None:
        """Enter or leave the halo removal brush (prompting to download the
        model the first time)."""
        if self.ui_state.isBrushMode and self.ui_state.brushTool == "halo":
            self.exit_local_brush_mode()
            return
        if not self._refresh_halo_install_status().ready:
            self._set_halo_state(promptVisible=True, error="")
            return
        self._enter_halo_brush_mode()

    def _enter_halo_brush_mode(self) -> None:
        if not self._enter_brush_mode("halo"):
            return
        self.update_status_message(
            "Remove halo: paint over the halo around the subject (a little onto "
            "the subject is fine). Right/Alt-drag erases.",
            8000,
        )

    @Slot()
    def halo_install_accept(self) -> None:
        """The owner agreed to the download: fetch what is missing."""
        if self._halo_install_running:
            return
        self._halo_install_running = True
        self._halo_install_cancel.clear()
        self._set_halo_state(
            promptVisible=True,
            downloading=True,
            progress=0.0,
            progressText="Starting download...",
            error="",
        )

        def progress(done: int, total: int, what: str) -> None:
            self._haloInstallProgress.emit({"done": done, "total": total, "what": what})

        def work() -> None:
            ai_inpaint.install(progress, self._halo_install_cancel.is_set)
            # Load the model now, on the worker, so the first correction does
            # not pay for it on top of its own run.
            ai_inpaint.get_inpainter()

        def done(future: concurrent.futures.Future) -> None:
            try:
                future.result()
                error = None
            except ai_inpaint.DownloadCancelled:
                error = "cancelled"
            except Exception as exc:  # pylint: disable=broad-exception-caught
                log.warning("Halo removal download failed: %s", exc)
                error = str(exc) or exc.__class__.__name__
            self._haloInstallDone.emit({"error": error})

        try:
            future = self._halo_install_executor.submit(work)
        except RuntimeError:
            self._halo_install_running = False
            return
        future.add_done_callback(done)

    @Slot()
    def halo_install_decline(self) -> None:
        """Close the prompt without downloading (or cancel a download)."""
        if self._halo_install_running:
            self._halo_install_cancel.set()
            self._set_halo_state(progressText="Cancelling...")
            return
        self._set_halo_state(promptVisible=False, error="")

    @Slot(object)
    def _on_halo_install_progress(self, payload: dict) -> None:
        if not self._halo_install_running:
            return
        done, total = int(payload["done"]), max(1, int(payload["total"]))
        what = payload.get("what") or ""
        label = "Finishing..." if what == "done" else f"Downloading {what}"
        self._set_halo_state(
            progress=min(1.0, done / total),
            progressText=f"{label}: {_mb(done)} of {_mb(total)} MB",
        )

    @Slot(object)
    def _on_halo_install_done(self, payload: dict) -> None:
        self._halo_install_running = False
        self._refresh_halo_install_status()
        error = payload.get("error")
        if error == "cancelled":
            self._set_halo_state(
                promptVisible=False, downloading=False, progress=0.0, progressText=""
            )
            self.update_status_message("Halo removal download cancelled")
            return
        if error:
            self._set_halo_state(
                downloading=False,
                progressText="",
                error=f"The download did not complete: {error}",
            )
            return
        self._set_halo_state(
            promptVisible=False, downloading=False, progress=1.0, progressText=""
        )
        # Layers that could not be computed without the model get their turn.
        self._halo_errors = {}
        self._schedule_halo_jobs()
        if not self._is_grid_view_active and self.image_files:
            self._enter_halo_brush_mode()

    # ---- strokes -----------------------------------------------------------

    def _halo_stroke_targets(self, erase: bool) -> list[str]:
        """Halo layers the next stroke goes onto.

        Painting goes onto the top halo layer (one layer holds every halo on
        the photo; each stroke is analysed with the others), creating it on
        first use.  Erasing goes onto every visible halo layer.
        """
        assets = self.image_editor._mask_assets
        layers = [layer for layer in self._local_layers() if is_halo_layer(layer)]
        if erase:
            ids = [
                layer["id"]
                for layer in layers
                if layer.get("visible", True)
                and assets.get(layer["id"]) is not None
                and assets[layer["id"]].has_strokes()
            ]
            if not ids:
                self.update_status_message("No halo removal to erase")
            return ids
        visible = [layer for layer in layers if layer.get("visible", True)]
        if visible:
            return [visible[-1]["id"]]
        layer_id = self._new_local_layer_id()
        assets[layer_id] = MaskData()
        self.image_editor.set_local_layers(
            [
                *self._local_layers(),
                {"id": layer_id, "visible": True, "recipe": {}, "halo": True},
            ]
        )
        return [layer_id]

    # ---- corrections -------------------------------------------------------

    def _halo_reset_for_navigation(self) -> None:
        """Forget in-flight corrections for the previous image."""
        self._halo_token += 1
        self._halo_jobs = {}
        self._halo_errors = {}
        self._set_halo_state()

    def _halo_signature(self, layer_id: str) -> Optional[tuple]:
        """The live signature of a halo layer (see ``halo_signature``)."""
        editor = self.image_editor
        return halo_signature(
            editor._mask_assets.get(layer_id),
            editor.current_edits,
            editor._mask_assets,
            editor._stack_sources,
        )

    def _drop_halo_result(self, layer_id: str) -> None:
        editor = self.image_editor
        if layer_id not in editor._halo_results:
            return
        # Rebind, never mutate: the render worker may be iterating the old dict.
        editor._halo_results = {
            k: v for k, v in editor._halo_results.items() if k != layer_id
        }
        editor.invalidate_render_cache()
        self._kick_preview_worker()

    def _schedule_halo_jobs(self, refresh_ui: bool = True) -> None:
        """Compute corrections for halo layers whose signature changed.

        *refresh_ui* False when the caller refreshes the brush UI itself
        right after; otherwise it is refreshed only if a layer's status
        changed.
        """
        editor = self.image_editor
        if editor is None or editor.session_id is None:
            return
        before = (dict(self._halo_jobs), dict(self._halo_errors))
        layers = halo_layers(editor.current_edits, include_hidden=True)
        live_ids = {layer["id"] for layer in layers}
        for layer_id in [lid for lid in editor._halo_results if lid not in live_ids]:
            self._drop_halo_result(layer_id)
        self._halo_jobs = {k: v for k, v in self._halo_jobs.items() if k in live_ids}
        self._halo_errors = {
            k: v for k, v in self._halo_errors.items() if k in live_ids
        }
        for layer in layers:
            layer_id = layer["id"]
            signature = self._halo_signature(layer_id)
            if signature is None:
                self._drop_halo_result(layer_id)
                self._halo_jobs.pop(layer_id, None)
                self._halo_errors.pop(layer_id, None)
                continue
            have = editor._halo_results.get(layer_id)
            if have is not None and have.signature == signature:
                continue
            if self._halo_jobs.get(layer_id) == signature:
                continue
            failed = self._halo_errors.get(layer_id)
            if failed is not None and failed[0] == signature:
                continue
            if not self._halo_install_status.ready:
                self._halo_errors[layer_id] = (
                    signature,
                    "not installed: choose Remove halo to download it",
                )
                if failed is None:
                    self.update_status_message(
                        "Halo removal is not installed, so this photo's halo "
                        "removal is not applied. Choose Remove halo to download it.",
                        8000,
                    )
                continue
            self._submit_halo_job(layer_id, signature)
        self._set_halo_state()
        if refresh_ui and (dict(self._halo_jobs), dict(self._halo_errors)) != before:
            self._refresh_local_brush_ui()

    def _submit_halo_job(self, layer_id: str, signature: tuple) -> None:
        editor = self.image_editor
        # Snapshot everything the worker reads: the GUI thread keeps editing.
        edits = copy.deepcopy(editor.current_edits)
        wanted = {layer_id} | {layer["id"] for layer in stack_layers(edits)}
        masks = {}
        for key in wanted:
            mask = editor._mask_assets.get(key)
            if mask is None:
                continue
            mask_copy = copy.deepcopy(mask)
            mask_copy.live_stroke = None
            masks[key] = mask_copy
        sources = dict(editor._stack_sources)
        token = self._halo_token
        session_id = editor.session_id
        editor_path = editor.current_filepath
        self._halo_jobs[layer_id] = signature
        self._halo_errors.pop(layer_id, None)
        self.update_status_message("Removing halo...", 30000)

        def cancelled() -> bool:
            return (
                self._halo_token != token
                or self._halo_jobs.get(layer_id) != signature
            )

        def work():
            if cancelled():
                return None
            editor._ensure_float_image()
            base = editor.float_image
            if base is None or editor.session_id != session_id:
                return None
            inpainter = ai_inpaint.get_inpainter()
            base = halo_base(base, edits, masks, sources)
            if cancelled():
                return None
            return compute_halo_fix(
                base, masks[layer_id], signature, inpainter.inpaint, cancelled=cancelled
            )

        def done(future: concurrent.futures.Future) -> None:
            try:
                result, error = future.result(), None
            except Exception as exc:  # pylint: disable=broad-exception-caught
                result, error = None, exc
            self._haloResultReady.emit(
                {
                    "token": token,
                    "layer_id": layer_id,
                    "signature": signature,
                    "session_id": session_id,
                    "editor_path": editor_path,
                    "result": result,
                    "error": error,
                }
            )

        try:
            future = self._halo_executor.submit(work)
        except RuntimeError:
            self._halo_jobs.pop(layer_id, None)
            return
        future.add_done_callback(done)

    @Slot(object)
    def _on_halo_result_ready(self, payload: dict) -> None:
        if getattr(self, "_shutting_down", False) or payload["token"] != self._halo_token:
            return
        layer_id = payload["layer_id"]
        signature = payload["signature"]
        if self._halo_jobs.get(layer_id) == signature:
            self._halo_jobs.pop(layer_id, None)
        editor = self.image_editor
        if (
            editor.session_id != payload["session_id"]
            or editor.current_filepath != payload["editor_path"]
            or self._halo_signature(layer_id) != signature
        ):
            # Superseded by a newer stroke (already scheduled) or another photo.
            self._set_halo_state()
            return
        error = payload.get("error")
        if error is not None:
            if isinstance(error, HaloFixError) and str(error) == "cancelled":
                self._set_halo_state()
                return
            if isinstance(error, ai_inpaint.InpaintUnavailable):
                # The model went missing since the status was read.
                self._refresh_halo_install_status()
            message = str(error) if isinstance(error, HaloFixError) else repr(error)
            log.warning("Halo removal failed: %s", message)
            self._halo_errors[layer_id] = (signature, message)
            # Show what a save would write: no correction for this layer.
            self._drop_halo_result(layer_id)
            self.update_status_message(f"Halo removal: {message}", 8000)
            self._set_halo_state()
            self._refresh_local_brush_ui()
            return
        results = {k: v for k, v in editor._halo_results.items() if k != layer_id}
        if payload["result"] is not None:
            results[layer_id] = payload["result"]
        # Rebind, never mutate: the render worker may be iterating the old dict.
        editor._halo_results = results
        editor.invalidate_render_cache()
        self._kick_preview_worker()
        self._set_halo_state()
        self._refresh_local_brush_ui()
        if not self._halo_jobs:
            self.update_status_message("Halo removed", 3000)

    def _halo_layer_summary(self, layer: dict) -> str:
        layer_id = layer["id"]
        if layer_id in self._halo_jobs:
            return "Halo removal · working..."
        if layer_id in self._halo_errors:
            return f"Halo removal · {self._halo_errors[layer_id][1]}"
        return "Halo removal"
