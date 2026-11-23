"""Select the background by depth: a dodge & burn layer masked by estimated depth.

Mixed into AppController (faststack/app.py); every method runs on the
controller instance.  The mask maths is ``faststack.imaging.depth_select``;
the depth model is downloaded on first use (``faststack.ai.depth``), after
the owner agrees to a prompt that says what is downloaded, how big it is,
where it goes and what it does.

Flow:

* ``select_background`` (the "Bkgd" button) enters dodge & burn, adds a
  *depth layer* — an ordinary recipe layer with ``"depth": {"amount",
  "feather"}`` — and selects it for editing, so the sliders edit its recipe
  and brush strokes add to (or, erasing, cut from) its selection.  When the
  model is not installed it shows the download prompt first.
* ``set_depth_param`` (the Amount and Softness sliders) changes the selected
  depth layer's cutoff; the render only re-thresholds the cached depth map.
* ``_schedule_depth_job`` (called after every layer change and when an edit
  is restored) estimates the photo's depth on a single daemon worker when a
  depth layer needs it.  The result comes back through ``_depthResultReady``
  and is kept only if the image and load session are still the ones it was
  computed from.  A photo whose depth failed is not retried until another
  photo is loaded.
* Until the depth map arrives, and when it cannot be computed, a depth layer
  applies only where it was painted.  A save computes a missing map on the
  save worker (``ImageEditor._depth_map_for_export``).
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
from typing import Any, Optional

from PySide6.QtCore import Slot

from faststack.ai import depth as ai_depth
from faststack.imaging.depth_select import (
    DEFAULT_AMOUNT,
    DEFAULT_FEATHER,
    DEFAULT_KEEP_SHARP,
    compute_depth_map,
    depth_layers,
    is_depth_layer,
    normalize_depth_params,
)
from faststack.imaging.mask import MaskData

log = logging.getLogger(__name__)


def _mb(n: int) -> int:
    return int(round(n / 1_000_000))


class DepthSelectMixin:
    """Controller half of background selection by depth."""

    # ---- setup -----------------------------------------------------------

    def _init_depth_select(self) -> None:
        """Requires ``_init_halo_removal`` to have run (shared install worker)."""
        from faststack.util.executors import create_daemon_threadpool_executor

        self._depth_executor = create_daemon_threadpool_executor(
            max_workers=1, thread_name_prefix="DepthMap"
        )
        # Session id whose depth is being estimated / failed (with why).
        self._depth_job_session: Optional[str] = None
        self._depth_error: Optional[tuple[str, str]] = None
        self._depth_install_status = ai_depth.install_status()
        # Bumped on navigation; results for an older token are dropped.
        self._depth_token = 0
        self._depth_install_cancel = threading.Event()
        self._depth_install_running = False
        # The photo Bkgd was pressed on when it showed the download prompt.
        self._depth_install_requested_path = None
        self._depthResultReady.connect(self._on_depth_result_ready)
        self._depthInstallProgress.connect(self._on_depth_install_progress)
        self._depthInstallDone.connect(self._on_depth_install_done)
        self._set_depth_state()

    def _shutdown_depth_select(self) -> None:
        self._depth_install_cancel.set()
        executor = getattr(self, "_depth_executor", None)
        if executor is not None:
            self._safe_shutdown_executor(executor, "_depth_executor", wait=False)

    def _refresh_depth_install_status(self):
        self._depth_install_status = ai_depth.install_status()
        return self._depth_install_status

    def _editing_depth_layer(self) -> Optional[dict]:
        layer = self._local_layer(self._local_editing_layer_id)
        return layer if is_depth_layer(layer) else None

    def _set_depth_state(self, **changes: Any) -> None:
        """Merge *changes* into ``uiState.depthState`` (with the install
        figures and the selected depth layer); assigns only on a change."""
        old = self.ui_state.depthState or {}
        state = dict(old)
        status = self._depth_install_status
        editor = self.image_editor
        layer = self._editing_depth_layer() if editor is not None else None
        params = layer["depth"] if layer is not None else {}
        state.update(
            installed=status.ready,
            runtimeNeeded=not status.runtime_ready,
            modelNeeded=not status.model_ready,
            runtimeVersion=ai_depth._ai.ORT_VERSION,
            downloadMB=_mb(status.download_bytes),
            diskMB=_mb(status.disk_bytes),
            folder=status.folder,
            busy=self._depth_job_session is not None,
            ready=editor is not None and editor._depth_map is not None,
            failed=self._depth_error[1] if self._depth_error is not None else "",
            layerId=layer["id"] if layer is not None else "",
            amount=float(params.get("amount", DEFAULT_AMOUNT)),
            feather=float(params.get("feather", DEFAULT_FEATHER)),
            keep_sharp=float(params.get("keep_sharp", DEFAULT_KEEP_SHARP)),
        )
        state.setdefault("promptVisible", False)
        state.setdefault("downloading", False)
        state.setdefault("progress", 0.0)
        state.setdefault("progressText", "")
        state.setdefault("error", "")
        state.update(changes)
        if state != old:
            self.ui_state.depthState = state

    # ---- entering the tool / first-use download -------------------------

    @Slot()
    def select_background(self) -> None:
        """Add a layer that selects the background by depth (prompting to
        download the depth model the first time)."""
        if not self._refresh_depth_install_status().ready:
            # Remember which photo asked, so the finished download adds the
            # layer to it and not to whatever is open by then.
            self._depth_install_requested_path = self._key(self._current_image_path())
            self._set_depth_state(promptVisible=True, error="")
            return
        self._select_or_add_depth_layer()

    def _select_or_add_depth_layer(self) -> None:
        """Select the photo's background layer, or add one.

        The photo already has a background layer: select it (entering dodge
        & burn if needed) rather than pile up another, also when the photo
        was just reopened or brush mode was left. Pressed again while it is
        selected in dodge & burn, it adds one (e.g. a second, deeper
        selection with its own recipe).
        """
        was_local = self.ui_state.isBrushMode and self.ui_state.brushTool == "local"
        add_another = was_local and self._editing_depth_layer() is not None
        if not was_local:
            # Entering may (re)load the photo and reset the layer selection,
            # so the layers are read only after it.
            if not self._enter_brush_mode("local"):
                return
        elif not self._prepare_local_brush_image_state():
            return
        # A reload while preparing drops the selection: then select, not add.
        add_another = add_another and self._editing_depth_layer() is not None
        existing = depth_layers(self.image_editor.current_edits, include_hidden=True)
        if existing and not add_another:
            target = existing[-1]["id"]
            if self._local_editing_layer_id != target:
                # select_local_layer toggles, so only call it to change.
                self.select_local_layer(target)
            else:
                self._refresh_local_brush_ui()
                self._update_local_overlay()
            self.update_status_message(
                "Background layer selected. Press Bkgd again to add another."
            )
            return
        self._add_depth_layer()

    def _add_depth_layer(self) -> None:
        if not (self.ui_state.isBrushMode and self.ui_state.brushTool == "local"):
            if not self._enter_brush_mode("local"):
                return
        elif not self._prepare_local_brush_image_state():
            return
        if self._local_painting_layer_id is not None:
            self.finish_local_stroke()
        layer_id = self._new_local_layer_id()
        self.image_editor._mask_assets[layer_id] = MaskData()
        # It starts with the brush recipe, so sliders set before pressing the
        # button apply at once; otherwise the sliders now edit this layer.
        recipe = dict(self._local_brush_recipe)
        self._local_editing_layer_id = layer_id
        self._local_active_layer_id = layer_id
        if not recipe:
            # Nothing to see in the photo yet: show what is selected.
            self.ui_state.localOverlayVisible = True
        self._commit_local_layers(
            [
                *self._local_layers(),
                {
                    "id": layer_id,
                    "visible": True,
                    "recipe": recipe,
                    "depth": {
                        "amount": DEFAULT_AMOUNT,
                        "feather": DEFAULT_FEATHER,
                        "keep_sharp": DEFAULT_KEEP_SHARP,
                    },
                },
            ],
            "background selection added",
        )
        self._update_local_overlay()
        self.update_status_message(
            "Background selected: Amount sets how much, the sliders change it. "
            f"Paint adds to the selection, {self._erase_drag_hint()} removes. "
            "Press Brush dodge & burn to paint other changes on top.",
            10000,
        )
        if self.image_editor.current_filepath and recipe:
            from pathlib import Path

            self._auto_add_edited_to_batch_if_enabled(
                Path(self.image_editor.current_filepath)
            )

    @Slot(str, float)
    def set_depth_param(self, key: str, value: float) -> None:
        """Amount / Softness / Keep sharp slider of the selected depth layer."""
        layer = self._editing_depth_layer()
        if layer is None or key not in ("amount", "feather", "keep_sharp"):
            return
        params = normalize_depth_params({**layer["depth"], key: value})
        if params == layer["depth"]:
            return
        layers = [
            {**item, "depth": params} if item["id"] == layer["id"] else item
            for item in self._local_layers()
        ]
        self._commit_local_layers(layers, f"background selection {key} changed")
        self._update_local_overlay()
        self._auto_add_depth_edit_to_batch({**layer, "depth": params})

    def _auto_add_depth_edit_to_batch(self, layer: Optional[dict]) -> None:
        """A background selection changes the photo without any strokes, so
        the photo counts as edited once the layer has a recipe and a non-zero
        Amount (ordinary layers are added to the batch on their first
        stroke)."""
        if not (
            is_depth_layer(layer)
            and layer.get("visible", True)
            and layer.get("recipe")
            and layer["depth"].get("amount", 0.0) > 0.0
        ):
            return
        path = self.image_editor.current_filepath
        if path:
            from pathlib import Path

            self._auto_add_edited_to_batch_if_enabled(Path(path))

    @Slot()
    def paint_dodge_burn(self) -> None:
        """Leave the background layer for ordinary dodge & burn: the sliders
        set the recipe of new brush strokes again, and strokes go onto
        their own layers on top of the background's."""
        if self._editing_depth_layer() is None:
            return
        self.select_local_layer("")
        self.update_status_message(
            "Dodge & burn: set the sliders, then paint. "
            "Click the Background layer to change it again."
        )

    @Slot(bool)
    def set_depth_slider_active(self, active: bool) -> None:
        """While Amount, Softness or Keep sharp is dragged, show the mask: with
        no slider moved yet the layer changes nothing in the photo itself.
        Ctrl holds the mask separately, so letting go of the slider while
        Ctrl is still down keeps it shown."""
        self.set_editor_slider_drag_active(active)
        self._set_local_overlay_held(bool(active), source="slider")

    @Slot()
    def depth_install_accept(self) -> None:
        """The owner agreed to the download: fetch what is missing."""
        if self._depth_install_running or not self.ui_state.depthState.get(
            "promptVisible"
        ):
            return
        self._depth_install_running = True
        self._depth_install_cancel.clear()
        self._set_depth_state(
            promptVisible=True,
            downloading=True,
            progress=0.0,
            progressText="Starting download...",
            error="",
        )

        def progress(done: int, total: int, what: str) -> None:
            self._depthInstallProgress.emit({"done": done, "total": total, "what": what})

        def work() -> None:
            ai_depth.install(progress, self._depth_install_cancel.is_set)
            # Load the model now, on the worker, so the first selection does
            # not pay for it on top of its own run.
            ai_depth.get_estimator()

        def done(future: concurrent.futures.Future) -> None:
            try:
                future.result()
                error = None
            except ai_depth.DownloadCancelled:
                error = "cancelled"
            except Exception as exc:  # pylint: disable=broad-exception-caught
                log.warning("Depth model download failed: %s", exc)
                error = str(exc) or exc.__class__.__name__
            self._depthInstallDone.emit({"error": error})

        try:
            # The halo install worker, not one of our own: both installs may
            # fetch the shared ONNX Runtime and must not do so at once.
            future = self._halo_install_executor.submit(work)
        except RuntimeError:
            self._depth_install_running = False
            return
        future.add_done_callback(done)

    @Slot()
    def depth_install_decline(self) -> None:
        """Close the prompt without downloading (or cancel a download)."""
        if self._depth_install_running:
            self._depth_install_cancel.set()
            self._set_depth_state(progressText="Cancelling...")
            return
        self._set_depth_state(promptVisible=False, error="")

    @Slot(object)
    def _on_depth_install_progress(self, payload: dict) -> None:
        if not self._depth_install_running:
            return
        done, total = int(payload["done"]), max(1, int(payload["total"]))
        what = payload.get("what") or ""
        label = "Finishing..." if what == "done" else f"Downloading {what}"
        self._set_depth_state(
            progress=min(1.0, done / total),
            progressText=f"{label}: {_mb(done)} of {_mb(total)} MB",
        )

    @Slot(object)
    def _on_depth_install_done(self, payload: dict) -> None:
        self._depth_install_running = False
        self._refresh_depth_install_status()
        # The runtime may have come with this download.
        self._refresh_halo_install_status()
        self._set_halo_state()
        error = payload.get("error")
        if error == "cancelled":
            self._set_depth_state(
                promptVisible=False, downloading=False, progress=0.0, progressText=""
            )
            self.update_status_message("Depth model download cancelled")
            return
        if error:
            self._set_depth_state(
                downloading=False,
                progressText="",
                error=f"The download did not complete: {error}",
            )
            return
        self._set_depth_state(
            promptVisible=False, downloading=False, progress=1.0, progressText=""
        )
        # A restored depth layer that could not be computed gets its turn.
        self._depth_error = None
        # The photo on screen, not the editor's: the editor can still hold
        # the previous photo until brush mode reloads it.
        requested = getattr(self, "_depth_install_requested_path", None)
        self._depth_install_requested_path = None
        current = self._key(self._current_image_path())
        same_photo = requested is not None and requested == current
        if same_photo and not self._is_grid_view_active and self.image_files:
            # Finish what Bkgd was pressed for, on the photo it was pressed on.
            self._select_or_add_depth_layer()
        self._schedule_depth_job()

    # ---- depth map ---------------------------------------------------------

    def _depth_reset_for_navigation(self) -> None:
        """Forget the previous image's depth and any estimate in flight."""
        self._depth_token += 1
        self._depth_job_session = None
        self._depth_error = None
        self._set_depth_state()

    def _schedule_depth_job(self) -> None:
        """Estimate the photo's depth when a depth layer needs it."""
        editor = self.image_editor
        if editor is None or editor.session_id is None:
            return
        session_id = editor.session_id
        if not depth_layers(editor.current_edits, include_hidden=True):
            self._set_depth_state()
            return
        if editor._depth_map is not None or self._depth_job_session == session_id:
            self._set_depth_state()
            return
        if self._depth_error is not None and self._depth_error[0] == session_id:
            return
        if not self._depth_install_status.ready:
            self._depth_error = (
                session_id,
                "not installed: choose Bkgd to download the depth model",
            )
            self.update_status_message(
                "The depth model is not installed, so this photo's background "
                "selection is not applied. Choose Bkgd to download it.",
                8000,
            )
            self._set_depth_state()
            return

        token = self._depth_token
        editor_path = editor.current_filepath
        self._depth_job_session = session_id
        self._depth_error = None

        def cancelled() -> bool:
            return self._depth_token != token or editor.session_id != session_id

        def work():
            if cancelled():
                return None
            editor._ensure_float_image()
            base = editor.float_image
            if base is None or cancelled():
                return None
            return compute_depth_map(
                base, ai_depth.get_estimator().estimate, cancelled=cancelled
            )

        def done(future: concurrent.futures.Future) -> None:
            try:
                result, error = future.result(), None
            except Exception as exc:  # pylint: disable=broad-exception-caught
                result, error = None, exc
            self._depthResultReady.emit(
                {
                    "token": token,
                    "session_id": session_id,
                    "editor_path": editor_path,
                    "result": result,
                    "error": error,
                }
            )

        try:
            future = self._depth_executor.submit(work)
        except RuntimeError:
            self._depth_job_session = None
            return
        future.add_done_callback(done)
        self._set_depth_state()

    @Slot(object)
    def _on_depth_result_ready(self, payload: dict) -> None:
        if getattr(self, "_shutting_down", False) or payload["token"] != self._depth_token:
            return
        session_id = payload["session_id"]
        if self._depth_job_session == session_id:
            self._depth_job_session = None
        editor = self.image_editor
        if (
            editor.session_id != session_id
            or editor.current_filepath != payload["editor_path"]
        ):
            self._set_depth_state()
            return
        error = payload.get("error")
        result = payload.get("result")
        if error is not None or result is None:
            if isinstance(error, ai_depth.DepthUnavailable):
                # The model went missing since the status was read.
                self._refresh_depth_install_status()
            message = str(error) if error is not None else "cancelled"
            if error is not None:
                log.warning("Depth estimation failed: %r", error)
                self._depth_error = (session_id, message)
                self.update_status_message(f"Background selection: {message}", 8000)
            self._set_depth_state()
            self._refresh_local_brush_ui()
            return
        # Rebind, never mutate: the render worker may be reading the old one.
        editor._depth_map = result
        editor.invalidate_render_cache()
        self._kick_preview_worker()
        # Stack paint's hover preview shows the background layer too.
        self._schedule_stack_hover()
        self._set_depth_state()
        self._refresh_local_brush_ui()
        self._update_local_overlay()

    def _depth_layer_summary(self, layer: dict) -> str:
        amount = round(float(layer["depth"].get("amount", 0.0)) * 100)
        recipe = self._local_recipe_summary(layer["recipe"])
        if self._depth_job_session is not None:
            status = " · estimating depth..."
        elif self._depth_error is not None:
            status = f" · {self._depth_error[1]}"
        else:
            status = ""
        return f"Background {amount}% · {recipe}{status}"
