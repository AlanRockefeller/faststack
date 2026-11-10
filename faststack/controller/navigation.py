"""Paced arrow-key navigation and the navigation/presentation tracing that
--debug-cache reports.

Mixed into AppController (faststack/app.py); every method runs on the
controller instance.
"""

import logging
import math
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QObject,
    QTimer,
    Slot,
)

from faststack.controller.common import _worker_unaccounted_ms
from faststack.imaging.cache import (
    build_cache_key,
)
from faststack.models import (
    DecodedImage,
)

log = logging.getLogger(__name__)


class NavigationMixin:
    def _fast_buffer_depth(self, index: int, direction: int) -> int:
        """Count consecutive cache-ready fast frames ahead of ``index``."""
        if direction not in (-1, 1) or not self.image_files:
            return 0
        _, _, display_generation = self.get_display_info()
        depth = 0
        candidate = index + direction
        max_probe = max(1, int(getattr(self.prefetcher, "prefetch_radius", 20)))
        while 0 <= candidate < len(self.image_files) and depth < max_probe:
            path = self.image_files[candidate].path
            key = build_cache_key(path, display_generation, "fast")
            # Telemetry must not reorder the LRU: otherwise probing nearest to
            # farthest would make the least urgent frame the most recent one.
            if key not in self.image_cache:
                break
            depth += 1
            candidate += direction
        return depth

    def _record_navigation_underflow(
        self,
        target_index: int,
        direction: int,
        buffer_depth: int,
    ) -> None:
        """Record a cadence tick whose exact next fast frame was not ready."""
        self._navigation_underflow_count += 1
        if not self.debug_cache:
            return
        with self._nav_trace_lock:
            if self._burst is None:
                self._burst = self._new_burst(time.perf_counter())
            self._burst["underflow_count"] += 1
        self._restart_nav_settle_timer()
        if self.debug_cache_trace:
            log.info(
                "[NAVTRACE] underflow target=%d direction=%d fast_buffer=%d total=%d",
                target_index,
                direction,
                buffer_depth,
                self._navigation_underflow_count,
            )

    def _seed_nav_record(
        self,
        index: int,
        *,
        direction: int = 0,
    ) -> int:
        """Mint an exact navigation id and seed its expected-file provenance."""
        # Paced navigation may mint this before changing current_index so an
        # underflow wait remains part of request-to-present latency. Direct
        # jumps still mint immediately after changing current_index.
        now = time.perf_counter()
        depth_direction = (
            direction if direction in (-1, 1) else self._last_navigation_direction
        )
        depth_origin = index - direction if direction in (-1, 1) else index
        fast_buffer_depth = self._fast_buffer_depth(depth_origin, depth_direction)
        try:
            path = (
                Path(self.view_override_path)
                if self.view_override_path and index == self.current_index
                else self.image_files[index].path
            )
        except (IndexError, AttributeError):
            path = None
        with self._nav_trace_lock:
            previous = self._nav_records.get(self._current_nav_seq)
            if previous is not None and previous.get("summary_complete"):
                self._nav_records.pop(self._current_nav_seq, None)
            self._nav_seq += 1
            seq = self._nav_seq
            self._current_nav_seq = seq
            self._nav_target_index = index
            self._nav_target_path_key = self._key(path)
            self._nav_records[seq] = {
                "seq": seq,
                "index": index,
                "path_key": self._key(path),
                "path": str(path) if path is not None else None,
                "filename": path.name if path is not None else "n/a",
                "t_mint": now,
                "fast_buffer_depth": fast_buffer_depth,
                "requests": {},
                "first_source_presented": False,
                "first_real_presented": False,
                "first_placeholder_presented": False,
                "settled_presented": False,
                "terminal": False,
            }
            while len(self._nav_records) > self._NAV_RECORDS_MAX:
                self._nav_records.popitem(last=False)

            if self._burst is None:
                self._burst = self._new_burst(now)
            self._burst["nav_count"] += 1
            self._burst["t_last"] = now
            self._burst["seqs"].append(seq)
            self._burst["seq_set"].add(seq)
            self._burst["input_settled"] = False
            self._burst["deadline_reached"] = False
            self._burst["buffer_depths"].append(fast_buffer_depth)

        self._restart_nav_settle_timer()
        return seq

    def _ensure_nav_record_for_current_target(self) -> None:
        """Mint a seq for displayed target changes outside _set_current_index."""
        if (
            not self.debug_cache
            or self._is_grid_view_active
            or not self.image_files
            or not (0 <= self.current_index < len(self.image_files))
        ):
            return
        path = (
            Path(self.view_override_path)
            if self.view_override_path
            else self.image_files[self.current_index].path
        )
        path_key = self._key(path)
        with self._nav_trace_lock:
            target_matches = (
                self._nav_target_index == self.current_index
                and self._nav_target_path_key == path_key
            )
        if not target_matches:
            self._seed_nav_record(self.current_index)

    @staticmethod
    def _nav_request_key(generation: Optional[int]) -> int:
        return generation if isinstance(generation, int) else -1

    def _nav_request_record(self, record: dict, generation: Optional[int]) -> dict:
        """Return the per-source-request record. Caller holds _nav_trace_lock."""
        key = self._nav_request_key(generation)
        requests = record.setdefault("requests", {})
        return requests.setdefault(key, {"generation": generation})

    def _note_nav_source_requested(self, seq: int, generation: int) -> None:
        """Record a bound QML source before its asynchronous Ready callback."""
        if not self.debug_cache:
            return
        with self._nav_trace_lock:
            record = self._nav_records.get(seq)
            if record is None and seq == 0:
                expected_path = None
                if self.image_files and 0 <= self.current_index < len(self.image_files):
                    expected_path = str(self.image_files[self.current_index].path)
                record = self._ensure_startup_nav_record(
                    seq,
                    self.current_index,
                    expected_path,
                )
            if record is None:
                return
            request = self._nav_request_record(record, generation)
            request.setdefault("requested_t", time.perf_counter())

    def _new_burst(self, now: float) -> dict:
        return {
            "t_first": now,
            "t_last": now,
            "nav_count": 0,
            "seqs": [],
            "seq_set": set(),
            "input_settled": False,
            "deadline_reached": False,
            "presented_count": 0,
            "coalesced_count": 0,
            "placeholder_count": 0,
            "t_first_present": None,
            "t_last_present": None,
            "present_latencies": [],
            "frame_intervals": [],
            "image_dwells": [],
            "t_last_frame_present": None,
            "t_last_image_present": None,
            "queue_waits": [],
            "decode_times": [],
            "cache_hits": 0,
            "cache_misses": 0,
            "late_cache_hits": 0,
            "canceled": 0,
            "wasted_ms": 0.0,
            "fast_to_full": [],
            "buffer_depths": [],
            "underflow_count": 0,
            "pace_intervals": [],
        }

    def _restart_nav_settle_timer(self) -> None:
        if self._nav_settle_timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(self._NAV_SETTLE_MS)
            timer.timeout.connect(self._on_nav_input_settled)
            self._nav_settle_timer = timer
        if self._nav_summary_deadline_timer is None:
            deadline = QTimer(self)
            deadline.setSingleShot(True)
            deadline.setInterval(self._NAV_SUMMARY_DEADLINE_MS)
            deadline.timeout.connect(self._on_nav_summary_deadline)
            self._nav_summary_deadline_timer = deadline
        self._nav_summary_deadline_timer.stop()
        self._nav_settle_timer.start()

    def _on_nav_input_settled(self) -> None:
        if (
            self._held_navigation_direction
            or self._paced_navigation_pending is not None
            or self._paced_navigation_queue
            or self._paced_present_gate is not None
        ):
            self._restart_nav_settle_timer()
            return
        with self._nav_trace_lock:
            if self._burst is None:
                return
            self._burst["input_settled"] = True
        if self._nav_summary_deadline_timer is not None:
            self._nav_summary_deadline_timer.start()
        self._maybe_emit_burst_summary()

    def _on_nav_summary_deadline(self) -> None:
        with self._nav_trace_lock:
            if self._burst is None:
                return
            self._burst["deadline_reached"] = True
        self._maybe_emit_burst_summary(force=True)

    def _record_nav_cache_outcome(
        self,
        seq: Optional[int],
        generation: Optional[int],
        outcome: str,
        cache_key: Optional[str] = None,
    ) -> None:
        if not self.debug_cache or seq is None:
            return
        with self._nav_trace_lock:
            record = self._nav_records.get(seq)
            if record is None and seq == 0:
                try:
                    expected = str(self.image_files[self.current_index].path)
                except (IndexError, AttributeError):
                    expected = None
                record = self._ensure_startup_nav_record(
                    seq,
                    self.current_index,
                    expected,
                )
            if record is None:
                return
            request = self._nav_request_record(record, generation)
            if not (outcome == "hit" and request.get("cache_outcome") == "miss-async"):
                # The held loupe source decoded this target in the background;
                # the provider's later lookup is not a cache hit of its own.
                request["cache_outcome"] = outcome
            if cache_key is not None:
                request["cache_key"] = cache_key

    def _note_burst_canceled(
        self,
        wasted_ms: float,
        *,
        seq: Optional[int],
    ) -> None:
        with self._nav_trace_lock:
            if (
                self._burst is not None
                and seq is not None
                and seq in self._burst["seq_set"]
            ):
                self._burst["canceled"] += 1
                self._burst["wasted_ms"] += max(0.0, wasted_ms)

    def _note_cache_reject(
        self,
        reason: str,
        cache_key,
        *,
        decoded: Optional[DecodedImage] = None,
    ) -> None:
        if not self.debug_cache:
            return
        trace = getattr(decoded, "decode_trace", None)
        seq = trace.get("seq") if isinstance(trace, dict) else None
        worker_start = trace.get("worker_start_t") if isinstance(trace, dict) else None
        wasted_ms = (
            (time.perf_counter() - worker_start) * 1000.0
            if isinstance(worker_start, (int, float))
            else 0.0
        )
        self._note_burst_canceled(wasted_ms, seq=seq)
        log.info(
            "[NAVTRACE] cache_reject seq=%s task=%s reason=%s key=%s wasted=%.1fms",
            seq,
            trace.get("task_id", "?") if isinstance(trace, dict) else "?",
            reason,
            cache_key,
            wasted_ms,
        )

    @staticmethod
    def _percentile(values: list, pct: float) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        rank = math.ceil((pct / 100.0) * len(ordered)) - 1
        return ordered[max(0, min(len(ordered) - 1, rank))]

    def _maybe_emit_burst_summary(self, *, force: bool = False) -> None:
        with self._nav_trace_lock:
            burst = self._burst
            if burst is None or not burst["input_settled"]:
                return
            final_seq = burst["seqs"][-1] if burst["seqs"] else None
            final_record = self._nav_records.get(final_seq)
            final_done = bool(
                final_record
                and (
                    final_record.get("settled_presented")
                    or (
                        final_record.get("terminal")
                        and final_record.get("first_source_presented")
                    )
                )
            )
            if not force and not final_done:
                return
        self._emit_burst_summary()

    def _emit_burst_summary(self) -> None:
        single_unpresented = None
        single_pending = None
        with self._nav_trace_lock:
            burst = self._burst
            self._burst = None
            if burst is None:
                return
            seqs = burst["seq_set"]
            final_seq = burst["seqs"][-1] if burst["seqs"] else None
            final_record = self._nav_records.get(final_seq)
            upgrade_pending = bool(
                final_record
                and final_record.get("first_real_presented")
                and not final_record.get("settled_presented")
                and not final_record.get("terminal")
            )
            image_pending = bool(
                final_record
                and not final_record.get("first_real_presented")
                and not final_record.get("terminal")
            )
            pending_work = upgrade_pending or image_pending
            if burst["nav_count"] == 1 and final_record is not None:
                requests = list(final_record.get("requests", {}).values())
                latest = requests[-1] if requests else {}
                if not final_record.get("first_source_presented"):
                    if final_record.get("abandoned"):
                        reason = (
                            "abandoned: "
                            f"{final_record.get('abandoned_reason', 'target invalidated')}"
                        )
                    elif latest.get("ready_t") is not None:
                        reason = "no frame swap after Image.Ready"
                    elif latest.get("outcome"):
                        reason = "provider completed but QML did not become Ready"
                    else:
                        reason = "provider was not called"
                    single_unpresented = {
                        "seq": final_seq,
                        "index": final_record.get("index"),
                        "filename": final_record.get("filename", "n/a"),
                        "generation": latest.get("generation"),
                        "outcome": latest.get("outcome", "n/a"),
                        "reason": reason,
                    }
                elif pending_work:
                    single_pending = {
                        "seq": final_seq,
                        "index": final_record.get("index"),
                        "filename": final_record.get("filename", "n/a"),
                        "generation": latest.get("generation"),
                        "frame": (
                            "upgrade_pending" if upgrade_pending else "image_pending"
                        ),
                        "initial_quality": final_record.get("initial_quality", "n/a"),
                    }
            if final_record is not None:
                final_record["summary_complete"] = True
                final_record["summary_emitted_pending"] = pending_work
            for seq in seqs:
                # Keep the current target's record after the summary so later
                # same-target refreshes and grid/loupe round trips retain exact
                # provenance. _seed_nav_record removes it on the next target.
                if seq != final_seq:
                    self._nav_records.pop(seq, None)
            self._nav_pending_present = [
                pending
                for pending in self._nav_pending_present
                if pending[0] not in seqs or (pending_work and pending[0] == final_seq)
            ]
        if self._nav_summary_deadline_timer is not None:
            self._nav_summary_deadline_timer.stop()
        if single_unpresented is not None:
            log.warning(
                "[NAVTRACE] seq=%s gen=%s index=%s file=%r frame=not_presented "
                "outcome=%s reason=%s",
                single_unpresented["seq"],
                single_unpresented["generation"],
                single_unpresented["index"],
                single_unpresented["filename"],
                single_unpresented["outcome"],
                single_unpresented["reason"],
            )
        if single_pending is not None:
            log.warning(
                "[NAVTRACE] seq=%s gen=%s index=%s file=%r frame=%s "
                "initial_quality=%s reason=summary deadline reached",
                single_pending["seq"],
                single_pending["generation"],
                single_pending["index"],
                single_pending["filename"],
                single_pending["frame"],
                single_pending["initial_quality"],
            )
        if not self.debug_cache or burst["nav_count"] < 2:
            return

        presented = burst["presented_count"]
        first_p = burst["t_first_present"]
        last_p = burst["t_last_present"]
        if (
            presented >= 2
            and first_p is not None
            and last_p is not None
            and last_p > first_p
        ):
            # N samples span N-1 presentation intervals.
            imgs_per_s = f"{(presented - 1) / (last_p - first_p):.1f} img/s"
        else:
            imgs_per_s = "n/a"
        input_span = burst["t_last"] - burst["t_first"]
        input_rate = (
            f"{(burst['nav_count'] - 1) / input_span:.1f} nav/s"
            if burst["nav_count"] >= 2 and input_span > 0
            else "n/a"
        )

        cache_total = burst["cache_hits"] + burst["cache_misses"]
        hit_rate = (
            f"{burst['cache_hits'] / cache_total * 100.0:.0f}%"
            if cache_total
            else "n/a"
        )

        def pp(values: list) -> str:
            p50 = self._percentile(values, 50)
            p95 = self._percentile(values, 95)
            if p50 is None:
                return "p50=n/a/p95=n/a"
            return f"p50={p50:.1f}ms/p95={p95:.1f}ms"

        def pp_count(values: list) -> str:
            p50 = self._percentile(values, 50)
            p95 = self._percentile(values, 95)
            if p50 is None:
                return "p50=n/a/p95=n/a"
            return f"p50={p50:.0f}/p95={p95:.0f}"

        f2f = self._percentile(burst["fast_to_full"], 50)
        # A placeholder is a displayed source, but it is not the requested
        # image. Keep it visible in its own count and in `not_presented`.
        not_presented = max(0, burst["nav_count"] - burst["presented_count"])
        log.info(
            "[NAVTRACE] burst navs=%d presented=%d not_presented=%d "
            "coalesced=%d placeholders=%d "
            "input_rate=%s present_rate=%s hit_rate=%s "
            "target_to_present(%s) frame_interval(%s) image_dwell(%s) "
            "fast_buffer(%s) underflows=%d underflows_total=%d "
            "pace_interval(%s) demand_queue(%s) "
            "demand_decode(%s) late_hits=%d canceled=%d wasted=%.0fms "
            "fast_to_full=%s image_pending=%d upgrade_pending=%d",
            burst["nav_count"],
            presented,
            not_presented,
            burst["coalesced_count"],
            burst["placeholder_count"],
            input_rate,
            imgs_per_s,
            hit_rate,
            pp(burst["present_latencies"]),
            pp(burst["frame_intervals"]),
            pp(burst["image_dwells"]),
            pp_count(burst["buffer_depths"]),
            burst["underflow_count"],
            self._navigation_underflow_count,
            pp(burst["pace_intervals"]),
            pp(burst["queue_waits"]),
            pp(burst["decode_times"]),
            burst["late_cache_hits"],
            burst["canceled"],
            burst["wasted_ms"],
            f"{f2f:.1f}ms" if f2f is not None else "n/a",
            int(image_pending),
            int(upgrade_pending),
        )

    def _record_nav_worker_timing(self, payload: dict) -> None:
        """Receive task telemetry; never infer a displayed frame by index."""
        event = payload.get("event")
        if event in {"adopted", "migrated"}:
            if self.debug_cache_trace:
                log.info(
                    "[NAVTRACE] task_%s task=%s index=%s file=%r prior_seq=%s "
                    "seq=%s state=%s priority=%s bumped=%d",
                    event,
                    payload.get("task_id", "?"),
                    payload.get("index"),
                    (
                        Path(payload["source_path"]).name
                        if payload.get("source_path")
                        else "n/a"
                    ),
                    payload.get("prior_seq"),
                    payload.get("seq"),
                    payload.get("state"),
                    payload.get("priority"),
                    int(bool(payload.get("priority_bumped"))),
                )
            return
        if payload.get("canceled"):
            wasted = float(payload.get("wasted_ms", 0.0) or 0.0)
            seq = payload.get("seq")
            self._note_burst_canceled(wasted, seq=seq)
            with self._nav_trace_lock:
                record = self._nav_records.get(seq)
                post_summary_pending = bool(
                    record and record.get("summary_emitted_pending")
                )
            if self.debug_cache_trace or post_summary_pending:
                log.info(
                    "[NAVTRACE] task_canceled task=%s seq=%s index=%s file=%r "
                    "quality=%s stage=%s wasted=%.1fms",
                    payload.get("task_id", "?"),
                    seq,
                    payload.get("index"),
                    (
                        Path(payload["source_path"]).name
                        if payload.get("source_path")
                        else "n/a"
                    ),
                    payload.get("quality"),
                    payload.get("stage"),
                    wasted,
                )
            if post_summary_pending:
                with self._nav_trace_lock:
                    record = self._nav_records.get(seq)
                    if record is not None:
                        record["summary_emitted_pending"] = False
            return
        if self.debug_cache_trace:
            log.info("[NAVTRACE] worker %s", self._format_worker_stats(payload))

    def _ensure_startup_nav_record(
        self,
        seq: int,
        index: int,
        expected_path: Optional[str],
    ) -> dict:
        """Create seq 0 lazily so startup placeholders cannot masquerade as pixels."""
        record = self._nav_records.get(seq)
        if record is not None:
            return record
        path = Path(expected_path) if expected_path else None
        record = {
            "seq": seq,
            "index": index,
            "path_key": self._key(path),
            "path": expected_path,
            "filename": path.name if path is not None else "n/a",
            "t_mint": self._app_start_t,
            "requests": {},
            "first_source_presented": False,
            "first_real_presented": False,
            "first_placeholder_presented": False,
            "settled_presented": False,
            "terminal": False,
        }
        self._nav_records[seq] = record
        return record

    def _record_provider_timing(
        self,
        seq: Optional[int],
        index: int,
        generation: Optional[int],
        provider_ms: float,
        *,
        outcome: str,
        expected_path: Optional[str] = None,
        provenance_kind: str = "display",
        served_quality: Optional[str] = None,
        served_path: Optional[str] = None,
        placeholder_reason: Optional[str] = None,
        future_active: bool = False,
        worker: Optional[dict] = None,
    ) -> None:
        if not self.debug_cache or seq is None:
            return
        anomaly = None
        with self._nav_trace_lock:
            record = self._nav_records.get(seq)
            if record is None and seq == 0:
                record = self._ensure_startup_nav_record(seq, index, expected_path)
            if record is None:
                return
            request = self._nav_request_record(record, generation)
            if record.get("abandoned") or request.get("abandoned"):
                return
            request.pop("provenance_error", None)
            request.pop("provenance_unknown", None)
            request.update(
                {
                    "provider_ms": provider_ms,
                    "outcome": outcome,
                    "expected_path": expected_path,
                    "provenance_kind": provenance_kind,
                    "served_quality": served_quality,
                    "served_path": served_path,
                    "placeholder_reason": placeholder_reason,
                    "future_active": future_active,
                    "worker": worker or {},
                    "provider_t": time.perf_counter(),
                }
            )
            # Until the navigation's first frame is actually presented, the
            # immutable path captured at mint is authoritative. This catches a
            # delete/sort/filter shifting index N to different pixels while its
            # request is in flight. Later same-seq requests may intentionally
            # target a variant/editor source and use their explicit path.
            preview_provenance = provenance_kind in {
                "editor-preview",
                "original-compare-preview",
            }
            if preview_provenance and expected_path is not None:
                expected_key = self._key(Path(expected_path))
                expected_display = expected_path
            elif not record.get("first_real_presented"):
                expected_key = record.get("path_key")
                expected_display = record.get("path")
            else:
                expected_key = (
                    self._key(Path(expected_path))
                    if expected_path is not None
                    else record.get("path_key")
                )
                expected_display = expected_path or record.get("path")
            served_key = self._key(Path(served_path)) if served_path else None
            if index != record.get("index"):
                anomaly = f"index requested={record.get('index')} served={index}"
            elif outcome == "wrong-image-blocked":
                anomaly = f"path requested={expected_display} " f"served={served_path}"
            elif outcome == "image" and served_key is None:
                request["provenance_unknown"] = True
            elif outcome == "image" and expected_key and served_key != expected_key:
                anomaly = f"path requested={expected_display} " f"served={served_path}"
            if anomaly:
                request["provenance_error"] = anomaly
            if outcome == "image" or future_active:
                record["terminal"] = False
            elif outcome != "image":
                record["terminal"] = True
        if anomaly:
            log.error(
                "[NAVTRACE] WRONG_IMAGE_BLOCKED seq=%s gen=%s index=%s %s",
                seq,
                generation,
                index,
                anomaly,
            )
        if future_active:
            log.error(
                "[NAVTRACE] PLACEHOLDER_WHILE_DECODING seq=%s gen=%s index=%s reason=%s",
                seq,
                generation,
                index,
                placeholder_reason,
            )

    def _nav_expected_path(
        self,
        seq: Optional[int],
        fallback: object = None,
    ) -> object:
        """Immutable target path for an unpresented navigation frame."""
        if not self.debug_cache or seq is None:
            return fallback
        with self._nav_trace_lock:
            record = self._nav_records.get(seq)
            if record is not None and not record.get("first_real_presented"):
                return record.get("path") or fallback
        return fallback

    @Slot(int, int)
    def _on_frame_ready(self, seq: int, generation: int) -> None:
        # The first decoded frame to reach Image.Ready is the usable current
        # image the startup constraint was protecting, so capacity is restored
        # here rather than at the later frame swap -- even if a newer
        # navigation already superseded this seq below.
        if self._startup_prefetch_constraint_token is not None:
            self._release_startup_prefetch_constraint("first-frame-ready")
        # QML may finish an asynchronous source after a direct jump has minted
        # a newer navigation sequence. Such a Ready belongs to pixels that are
        # no longer eligible to release pacing or enter presentation tracing.
        if seq != self._current_nav_seq:
            return
        if self.debug_cache:
            with self._nav_trace_lock:
                record = self._nav_records.get(seq)
                if record is not None:
                    request = record.get("requests", {}).get(
                        self._nav_request_key(generation)
                    )
                    if record.get("abandoned") or (
                        request is not None and request.get("abandoned")
                    ):
                        return
        now = time.perf_counter()
        fallback_due_t = None
        with self._nav_trace_lock:
            gate = self._paced_present_gate
            if gate is not None and gate.get("seq") == seq:
                self._paced_present_ready = True
                if not self._frame_sync_connected:
                    gate["sync_t"] = now
                if not self._frame_swapped_connected:
                    fallback_due_t = gate.get("due_t")
                    self._paced_present_gate = None
                    self._paced_present_ready = False
        if fallback_due_t is not None:
            self._navigation_gate_watchdog_timer.stop()
            if self._paced_navigation_queue or self._held_navigation_direction:
                if (
                    self._held_navigation_direction
                    and not self._navigation_repeat_started
                ):
                    remaining_ms = self._navigation_repeat_delay_ms
                    self._navigation_repeat_started = True
                else:
                    remaining_ms = max(
                        1,
                        round((fallback_due_t - now) * 1000.0),
                    )
                self._navigation_hold_timer.setInterval(remaining_ms)
                self._navigation_hold_timer.start()
            else:
                self._restart_quality_decode_timer()
        if not self.debug_cache:
            return
        with self._nav_trace_lock:
            record = self._nav_records.get(seq)
            if record is None:
                return
            request = self._nav_request_record(record, generation)
            # QML should emit Ready once per source. Ignore a duplicate rather
            # than replacing the timestamp while retaining the original item
            # in _nav_pending_present, which would make ready/present fields
            # describe two different Ready events.
            if request.get("ready_t") is not None:
                return
            request["ready_t"] = now
            request["ready_ms"] = (now - record["t_mint"]) * 1000.0
            marker = (seq, generation)
            if not any(
                (item[0], item[1]) == marker for item in self._nav_pending_present
            ):
                sync_t = None if self._frame_sync_connected else now
                self._nav_pending_present.append((seq, generation, now, sync_t))
        if not self._frame_swapped_connected:
            with self._nav_trace_lock:
                self._nav_pending_present = [
                    item
                    for item in self._nav_pending_present
                    if (item[0], item[1]) != (seq, generation)
                ]
            self._present_nav_request(seq, generation, now, rendered=False)

    @Slot()
    def _capture_frame_sync(self) -> None:
        """Mark sources included in this render-thread scene-graph sync."""
        if not self.debug_cache and not self._paced_present_ready:
            return
        sync_t = time.perf_counter()
        with self._nav_trace_lock:
            gate = self._paced_present_gate
            if (
                self._paced_present_ready
                and gate is not None
                and gate.get("sync_t") is None
            ):
                gate["sync_t"] = sync_t
            if not self.debug_cache:
                return
            if not self._nav_pending_present:
                return
            self._nav_pending_present = [
                (
                    seq,
                    generation,
                    ready_t,
                    (
                        sync_t
                        if prior_sync_t is None and ready_t <= sync_t
                        else prior_sync_t
                    ),
                )
                for seq, generation, ready_t, prior_sync_t in self._nav_pending_present
            ]

    @Slot()
    def _capture_frame_swap(self) -> None:
        """Capture the swap timestamp on the emitting/render thread."""
        if not self.debug_cache and not self._paced_present_ready:
            return
        with self._nav_trace_lock:
            gate = self._paced_present_gate
            paced_synced = bool(
                self._paced_present_ready
                and gate is not None
                and gate.get("sync_t") is not None
            )
            if not self._nav_pending_present and not paced_synced:
                return
        self._frameSwapObserved.emit(time.perf_counter())

    @Slot(float)
    def _on_frame_swapped(self, swap_t: float) -> None:
        """Present only the newest Ready source eligible for this exact swap."""
        pace_due_t = None
        with self._nav_trace_lock:
            gate = self._paced_present_gate
            gate_sync_t = gate.get("sync_t") if gate is not None else None
            if (
                self._paced_present_ready
                and gate is not None
                and gate_sync_t is not None
                and gate_sync_t <= swap_t
            ):
                pace_due_t = gate.get("due_t")
                self._paced_present_gate = None
                self._paced_present_ready = False
        if pace_due_t is not None:
            self._navigation_gate_watchdog_timer.stop()
            if self._paced_navigation_queue or self._held_navigation_direction:
                if (
                    self._held_navigation_direction
                    and not self._navigation_repeat_started
                ):
                    # Match normal keyboard behavior: one immediate action,
                    # then the OS repeat delay before continuous movement.
                    remaining_ms = self._navigation_repeat_delay_ms
                    self._navigation_repeat_started = True
                else:
                    # There is never more than one published frame in flight,
                    # so a late swap cannot create a catch-up burst.
                    remaining_ms = max(
                        1,
                        round((pace_due_t - swap_t) * 1000.0),
                    )
                self._navigation_hold_timer.setInterval(remaining_ms)
                self._navigation_hold_timer.start()
            else:
                self._restart_quality_decode_timer()

        if not self.debug_cache:
            return
        with self._nav_trace_lock:
            eligible = [
                item
                for item in self._nav_pending_present
                if item[3] is not None and item[3] <= swap_t
            ]
            self._nav_pending_present = [
                item
                for item in self._nav_pending_present
                if item[3] is None or item[3] > swap_t
            ]
        if not eligible:
            return
        presented = eligible[-1]
        for seq, generation, _ready_t, _sync_t in eligible[:-1]:
            self._mark_nav_request_coalesced(
                seq,
                generation,
                superseded_by=(presented[0], presented[1]),
            )
        self._present_nav_request(
            presented[0],
            presented[1],
            swap_t,
            rendered=True,
        )

    def _mark_nav_request_coalesced(
        self,
        seq: int,
        generation: int,
        *,
        superseded_by: tuple[int, int],
    ) -> None:
        warning = None
        with self._nav_trace_lock:
            record = self._nav_records.get(seq)
            if record is None:
                return
            request = self._nav_request_record(record, generation)
            if (
                record.get("abandoned")
                or request.get("abandoned")
                or request.get("presented")
                or request.get("coalesced")
            ):
                return
            request["coalesced"] = True
            is_initial = not record.get("first_source_presented")
            if is_initial and self._burst is not None and seq in self._burst["seq_set"]:
                self._burst["coalesced_count"] += 1
                warning = (
                    record.get("index"),
                    record.get("filename", "n/a"),
                    request.get("ready_ms"),
                )
        if warning is not None:
            log.warning(
                "[NAVTRACE] seq=%s gen=%s index=%s file=%r NOT_PRESENTED "
                "superseded_by=%s/%s ready=%s",
                seq,
                generation,
                warning[0],
                warning[1],
                superseded_by[0],
                superseded_by[1],
                (
                    f"{warning[2]:.1f}ms"
                    if isinstance(warning[2], (int, float))
                    else "n/a"
                ),
            )

    def _present_nav_request(
        self,
        seq: int,
        generation: int,
        present_t: float,
        *,
        rendered: bool,
    ) -> None:
        log_data = None
        first_milestone = False
        settled_milestone = False
        became_settled = False
        with self._nav_trace_lock:
            record = self._nav_records.get(seq)
            if record is None:
                return
            request = self._nav_request_record(record, generation)
            if (
                record.get("abandoned")
                or request.get("abandoned")
                or request.get("presented")
                or request.get("coalesced")
            ):
                return
            request["presented"] = True
            request["rendered"] = rendered
            request["present_t"] = present_t
            present_ms = (present_t - record["t_mint"]) * 1000.0
            outcome = request.get("outcome", "unknown")
            quality = request.get("served_quality") or "unknown"
            is_real = outcome == "image" and not request.get("provenance_error")
            is_first_source = not record.get("first_source_presented")
            is_initial_real = is_real and not record.get("first_real_presented")
            is_first_placeholder = not is_real and not record.get(
                "first_placeholder_presented"
            )
            worker = request.get("worker") or {}

            burst_for_frame = (
                self._burst
                if self._burst is not None and seq in self._burst["seq_set"]
                else None
            )
            if is_real and burst_for_frame is not None:
                prior_frame_t = burst_for_frame["t_last_frame_present"]
                if isinstance(prior_frame_t, (int, float)):
                    burst_for_frame["frame_intervals"].append(
                        (present_t - prior_frame_t) * 1000.0
                    )
                burst_for_frame["t_last_frame_present"] = present_t

            if is_first_source:
                record["first_source_presented"] = True

            if is_initial_real:
                record["first_real_presented"] = True
                record["terminal"] = False
                record["initial_quality"] = quality
                record["first_present_t"] = present_t
                if is_real and quality == "cover":
                    record["settled_presented"] = True
                first_milestone = not self._nav_first_fast_logged
                if first_milestone:
                    self._nav_first_fast_logged = True

                if self._burst is not None and seq in self._burst["seq_set"]:
                    burst = self._burst
                    prior_image_t = burst["t_last_image_present"]
                    if isinstance(prior_image_t, (int, float)):
                        burst["image_dwells"].append(
                            (present_t - prior_image_t) * 1000.0
                        )
                    burst["t_last_image_present"] = present_t
                    burst["presented_count"] += 1
                    burst["present_latencies"].append(present_ms)
                    if burst["t_first_present"] is None:
                        burst["t_first_present"] = present_t
                    burst["t_last_present"] = present_t
                    cache_outcome = request.get("cache_outcome", "unknown")
                    if cache_outcome in {"hit", "late-hit"}:
                        burst["cache_hits"] += 1
                    elif cache_outcome.startswith("miss"):
                        burst["cache_misses"] += 1
                    if cache_outcome == "late-hit":
                        burst["late_cache_hits"] += 1
                    if worker.get("seq") == seq and worker.get("role") in {
                        "demand",
                        "demand-reused",
                        "demand-migrated",
                    }:
                        queue_ms = worker.get("queue_ms")
                        decode_ms = worker.get("decode_ms")
                        if isinstance(queue_ms, (int, float)):
                            burst["queue_waits"].append(queue_ms)
                        if isinstance(decode_ms, (int, float)):
                            burst["decode_times"].append(decode_ms)
            elif not is_real:
                if is_first_placeholder:
                    record["first_placeholder_presented"] = True
                    if self._burst is not None and seq in self._burst["seq_set"]:
                        self._burst["placeholder_count"] += 1
                if not bool(request.get("future_active")):
                    record["terminal"] = True
            elif is_real and quality == "cover" and not record.get("settled_presented"):
                record["settled_presented"] = True
                became_settled = True
                first_t = record.get("first_present_t")
                fast_to_full = (
                    (present_t - first_t) * 1000.0
                    if isinstance(first_t, (int, float))
                    else None
                )
                request["fast_to_full_ms"] = fast_to_full
                if (
                    self._burst is not None
                    and seq in self._burst["seq_set"]
                    and isinstance(fast_to_full, (int, float))
                ):
                    self._burst["fast_to_full"].append(fast_to_full)

            settled_milestone = (
                is_real and quality == "cover" and not self._nav_first_settled_logged
            )
            if settled_milestone:
                self._nav_first_settled_logged = True
            log_data = {
                "index": record.get("index"),
                "filename": record.get("filename", "n/a"),
                "is_initial_real": is_initial_real,
                "is_real": is_real,
                "outcome": outcome,
                "quality": quality,
                "present_ms": present_ms,
                "ready_ms": request.get("ready_ms"),
                "ready_to_present_ms": (
                    (present_t - request["ready_t"]) * 1000.0
                    if rendered and isinstance(request.get("ready_t"), (int, float))
                    else None
                ),
                "provider_ms": request.get("provider_ms"),
                "cache_outcome": request.get("cache_outcome", "unknown"),
                "worker": worker,
                "placeholder_reason": request.get("placeholder_reason"),
                "future_active": bool(request.get("future_active")),
                "fast_to_full_ms": request.get("fast_to_full_ms"),
                "rendered": rendered,
                "sync_barrier": self._frame_sync_connected,
                "post_summary_pending": bool(record.get("summary_emitted_pending")),
                "terminal": bool(record.get("terminal")),
                "became_settled": became_settled,
                "fast_buffer_depth": record.get("fast_buffer_depth"),
            }

        if first_milestone:
            log.info(
                "[NAVTRACE] startup first_image_presented=%.1fms",
                (present_t - self._app_start_t) * 1000.0,
            )
        if settled_milestone:
            log.info(
                "[NAVTRACE] startup first_cover_presented=%.1fms",
                (present_t - self._app_start_t) * 1000.0,
            )
        if log_data is None:
            return
        if not log_data["is_real"]:
            log.warning(
                "[NAVTRACE] seq=%s gen=%s index=%s file=%r frame=placeholder "
                "outcome=%s reason=%s active_future=%d ready=%s %s",
                seq,
                generation,
                log_data["index"],
                log_data["filename"],
                log_data["outcome"],
                log_data["placeholder_reason"],
                int(log_data["future_active"]),
                self._format_ms(log_data["ready_ms"]),
                self._present_field(log_data),
            )
        elif (
            log_data["is_initial_real"]
            and log_data["cache_outcome"] == "hit"
            and not self.debug_cache_trace
        ):
            # A frame decoded ahead of time: its queue/read/decode timings
            # describe an earlier prefetch, not this navigation, and made the
            # commonest line the longest one. --debugcache-trace still prints
            # them; here the frame's size is enough to tell what was shown.
            worker = log_data["worker"]
            output = worker.get("output")
            dct = worker.get("dct")
            log.info(
                "[NAVTRACE] seq=%s gen=%s index=%s file=%r "
                "frame=initial quality=%s cache=hit %s%s"
                "fast_buffer=%s provider=%s ready=%s ready_to_present=%s %s",
                seq,
                generation,
                log_data["index"],
                log_data["filename"],
                log_data["quality"],
                f"output={output[0]}x{output[1]} " if output else "",
                f"dct={dct[0]}/{dct[1]} " if dct else "",
                log_data["fast_buffer_depth"],
                self._format_ms(log_data["provider_ms"]),
                self._format_ms(log_data["ready_ms"]),
                self._format_ms(log_data["ready_to_present_ms"]),
                self._present_field(log_data),
            )
        elif log_data["is_initial_real"]:
            worker = log_data["worker"]
            log.info(
                "[NAVTRACE] seq=%s gen=%s index=%s file=%r "
                "frame=initial quality=%s "
                "cache=%s task=%s producer_seq=%s role=%s priority=%s %s "
                "fast_buffer=%s provider=%s ready=%s ready_to_present=%s %s",
                seq,
                generation,
                log_data["index"],
                log_data["filename"],
                log_data["quality"],
                log_data["cache_outcome"],
                worker.get("task_id", "n/a"),
                worker.get("seq") if worker.get("seq") is not None else "n/a",
                worker.get("role", "n/a"),
                worker.get("priority", "n/a"),
                self._format_worker_stats(
                    worker,
                    include_index=False,
                    include_identity=False,
                ),
                log_data["fast_buffer_depth"],
                self._format_ms(log_data["provider_ms"]),
                self._format_ms(log_data["ready_ms"]),
                self._format_ms(log_data["ready_to_present_ms"]),
                self._present_field(log_data),
            )
        elif self.debug_cache_trace or log_data["became_settled"]:
            frame_kind = (
                "upgrade_late"
                if log_data["post_summary_pending"] and log_data["became_settled"]
                else "upgrade" if log_data["became_settled"] else "refresh"
            )
            log.info(
                "[NAVTRACE] seq=%s gen=%s index=%s file=%r frame=%s quality=%s "
                "fast_to_full=%s provider=%s ready=%s %s",
                seq,
                generation,
                log_data["index"],
                log_data["filename"],
                frame_kind,
                log_data["quality"],
                self._format_ms(log_data["fast_to_full_ms"]),
                self._format_ms(log_data["provider_ms"]),
                self._format_ms(log_data["ready_ms"]),
                self._present_field(log_data),
            )
        if log_data["post_summary_pending"] and (
            log_data["quality"] == "cover"
            or (not log_data["is_real"] and log_data["terminal"])
        ):
            with self._nav_trace_lock:
                record = self._nav_records.get(seq)
                if record is not None:
                    record["summary_emitted_pending"] = False
                self._nav_pending_present = [
                    pending
                    for pending in self._nav_pending_present
                    if pending[0] != seq
                ]
        self._maybe_emit_burst_summary()

    @staticmethod
    def _format_ms(value: object) -> str:
        return f"{value:.1f}ms" if isinstance(value, (int, float)) else "n/a"

    @staticmethod
    def _present_field(data: dict) -> str:
        if data.get("rendered") and data.get("sync_barrier"):
            name = "presented"
        elif data.get("rendered"):
            name = "swap_after_ready"
        else:
            name = "ready_fallback"
        return f"{name}={data['present_ms']:.1f}ms"

    @staticmethod
    def _format_worker_stats(
        stats: dict,
        *,
        include_index: bool = True,
        include_identity: bool = True,
    ) -> str:
        if not stats:
            return "worker=n/a"

        def ms(key: str) -> str:
            value = stats.get(key)
            return f"{value:.1f}ms" if isinstance(value, (int, float)) else "n/a"

        parts = []
        if include_index and "index" in stats:
            parts.append(f"index={stats['index']}")
        if include_identity:
            if stats.get("task_id") is not None:
                parts.append(f"task={stats['task_id']}")
            parts.append(
                f"seq={stats['seq'] if stats.get('seq') is not None else 'n/a'}"
            )
            if stats.get("role"):
                parts.append(f"role={stats['role']}")
            if stats.get("quality"):
                parts.append(f"quality={stats['quality']}")
            if stats.get("priority") is not None:
                parts.append(f"priority={stats['priority']}")
            if stats.get("display_generation") is not None:
                parts.append(f"display_gen={stats['display_generation']}")
            if stats.get("source_path"):
                parts.append(f"path={stats['source_path']!r}")
        parts.append(f"queue={ms('queue_ms')}")
        if stats.get("setup_ms") is not None:
            # Pre-read worker time: config + get_monitor_profile() work.
            parts.append(f"setup={ms('setup_ms')}")
        if stats.get("read_ms") is not None:
            # Diagnostic page-fault pass: file I/O + antivirus cost, split out
            # of jpeg_ms (decode_ms still contains both).
            parts.append(f"read={ms('read_ms')}")
        parts.extend(
            [
                f"decode={ms('decode_ms')}",
                f"jpeg={ms('jpeg_ms')}",
                f"resize={ms('resize_ms')}",
                f"meta={ms('metadata_ms')}",
                f"icc={ms('icc_ms')}",
                f"finalize={ms('finalize_ms')}",
                f"cache={ms('cache_ms')}",
                f"total={ms('total_ms')}",
            ]
        )
        # Wall time inside the worker that no phase claims. Normally a few ms of
        # buffer copies; a large value means the thread was stalled on something
        # unmeasured (constraint gate, contended lock, page-fault storm) and is
        # the difference between "the decode was slow" and "the decode waited".
        unaccounted = _worker_unaccounted_ms(stats)
        if unaccounted is not None:
            parts.append(f"other={unaccounted:.1f}ms")
        if stats.get("running_before_demand_ms", 0.0) > 0:
            parts.append(f"running_before_demand={ms('running_before_demand_ms')}")
        if stats.get("dependency_ms") is not None:
            parts.append(f"dependency={ms('dependency_ms')}")
        source = stats.get("source")
        if source:
            parts.append(f"source={source[0]}x{source[1]}")
        target = stats.get("target")
        if target:
            parts.append(f"target={target[0]}x{target[1]}")
        decode_target = stats.get("decode_target")
        if decode_target and decode_target != target:
            parts.append(f"decode_target={decode_target[0]}x{decode_target[1]}")
        source_bytes = stats.get("source_bytes")
        if isinstance(source_bytes, (int, float)):
            parts.append(f"input={source_bytes / 1024**2:.1f}MB")
        if stats.get("decoder"):
            parts.append(f"decoder={stats['decoder']}")
        dct = stats.get("dct")
        if dct:
            parts.append(f"dct={dct[0]}/{dct[1]}")
        output = stats.get("output")
        if output:
            parts.append(f"output={output[0]}x{output[1]}")
        if stats.get("queue_depth") is not None:
            parts.append(f"qdepth_submit={stats['queue_depth']}")
        if stats.get("inflight") is not None:
            parts.append(f"inflight_submit={stats['inflight']}")
        if stats.get("cache_accepted") is False:
            parts.append("cache_accepted=0")
        return " ".join(parts)

    def _begin_navigation_hold(
        self,
        direction: int,
        *,
        source: Optional[QObject] = None,
    ) -> None:
        """Move once, then enter stable-rate navigation after the repeat delay."""
        if direction not in (-1, 1) or self._is_grid_view_active:
            return
        if (
            self._held_navigation_direction == direction
            and self._navigation_hold_source is source
        ):
            # Native auto-repeat reaches here while the physical key remains
            # down. Normally the controller timer, a decode, or a presentation
            # gate is already active. If none is active, restart the cadence;
            # if a gate lost its watchdog, restore that safety net too.
            with self._nav_trace_lock:
                has_present_gate = self._paced_present_gate is not None
            if has_present_gate:
                if not self._navigation_gate_watchdog_timer.isActive():
                    self._navigation_gate_watchdog_timer.start()
            elif (
                self._paced_navigation_pending is None
                and not self._navigation_hold_timer.isActive()
            ):
                self._resume_paced_navigation()
            return
        if self._held_navigation_direction:
            self._abandon_paced_navigation_state("navigation hold direction changed")

        self._held_navigation_direction = direction
        self._navigation_hold_source = source
        self._last_navigation_direction = direction
        self._navigation_repeat_started = False
        self._quality_decode_immediate_pending = False
        self._quality_decode_token += 1
        self._quality_decode_timer.stop()
        self.prefetcher.cancel_pending_cover_tasks()

        self._navigation_hold_timer.stop()
        self._request_paced_navigation_step(direction)

    def _abandon_paced_navigation_state(self, reason: str) -> None:
        """Invalidate every state item owned by a prior paced target.

        The epoch rejects decode completions, while clearing the presentation
        gate under the trace lock prevents late Ready/swap acknowledgements from
        releasing or re-establishing pacing for a newer source.
        """
        gate_seq = None
        pending_seq = (
            self._paced_navigation_pending.get("nav_seq")
            if self._paced_navigation_pending is not None
            else None
        )
        self._paced_navigation_epoch += 1
        self._paced_navigation_pending = None
        self._paced_navigation_queue.clear()
        self._held_navigation_direction = 0
        self._navigation_hold_source = None
        self._navigation_repeat_started = False
        self._quality_decode_immediate_pending = False
        self._navigation_hold_timer.stop()
        self._navigation_gate_watchdog_timer.stop()
        abandoned_logs = []
        with self._nav_trace_lock:
            if self._paced_present_gate is not None:
                gate_seq = self._paced_present_gate.get("seq")
            self._paced_present_gate = None
            self._paced_present_ready = False

            # A Ready/synchronized source from the sequence being abandoned
            # must not become eligible for a later swap from a replacement
            # source. Sequence ids are monotonic; retain any genuinely newer
            # entries rather than clearing presentation telemetry wholesale.
            abandon_through = self._current_nav_seq
            abandoned_seqs = {
                seq
                for seq in (gate_seq, pending_seq)
                if isinstance(seq, int) and seq <= abandon_through
            }
            abandoned_seqs.update(
                seq
                for seq, _generation, _ready_t, _sync_t in self._nav_pending_present
                if seq <= abandon_through
            )

            # A bound QML source is outstanding before Image.Ready and therefore
            # may not have a paced gate or pending-present marker yet. Include
            # the active record when its initial source is still unpresented or
            # it owns an outstanding refresh/upgrade request.
            active_record = self._nav_records.get(self._current_nav_seq)
            if active_record is not None:
                active_requests = active_record.get("requests", {}).values()
                has_outstanding_request = any(
                    not request.get("presented")
                    and not request.get("coalesced")
                    and not request.get("abandoned")
                    for request in active_requests
                )
                if (
                    not active_record.get("first_source_presented")
                    or has_outstanding_request
                ):
                    abandoned_seqs.add(self._current_nav_seq)

            self._nav_pending_present = [
                pending
                for pending in self._nav_pending_present
                if pending[0] not in abandoned_seqs
            ]

            abandoned_t = time.perf_counter()
            for seq in abandoned_seqs:
                record = self._nav_records.get(seq)
                if record is None:
                    continue
                request_abandoned = False
                for request in record.get("requests", {}).values():
                    if (
                        not request.get("presented")
                        and not request.get("coalesced")
                        and not request.get("abandoned")
                    ):
                        request["abandoned"] = True
                        request["abandoned_t"] = abandoned_t
                        request["abandoned_reason"] = reason
                        request_abandoned = True
                initial_abandoned = not record.get("first_source_presented")
                if initial_abandoned:
                    record["abandoned"] = True
                    record["abandoned_t"] = abandoned_t
                    record["abandoned_reason"] = reason
                if not initial_abandoned and not request_abandoned:
                    continue
                record["terminal"] = True
                record["summary_emitted_pending"] = False
                abandoned_logs.append(
                    (
                        seq,
                        record.get("index"),
                        record.get("filename", "n/a"),
                        "initial" if initial_abandoned else "request",
                    )
                )

            # A list/generation transition may retain the same path and index.
            # Force the next displayed source evaluation to mint a fresh trace
            # sequence instead of reusing the explicitly terminated record.
            self._nav_target_index = None
            self._nav_target_path_key = None
        if self.debug_cache:
            for seq, index, filename, frame_kind in abandoned_logs:
                log.info(
                    "[NAVTRACE] seq=%s index=%s file=%r frame=%s_abandoned reason=%s",
                    seq,
                    index,
                    filename,
                    frame_kind,
                    reason,
                )

    def _begin_direct_image_transition(self, reason: str) -> None:
        """Invalidate paced/deferred work before a direct list/source mutation."""
        self._abandon_paced_navigation_state(reason)
        with self._preview_lock:
            cancel_event = self._preview_refinement_cancel
        if cancel_event is not None:
            cancel_event.set()
        # Invalidate the active URL correlation immediately. The replacement
        # source may not be evaluated until a later QML turn, and an old Ready
        # acknowledgement must not remain eligible during that gap.
        with self._nav_trace_lock:
            self._nav_seq += 1
            self._current_nav_seq = self._nav_seq
        self._quality_decode_token += 1
        self._quality_decode_immediate_pending = False
        self._quality_decode_timer.stop()
        self.prefetcher.cancel_pending_cover_tasks()

    def _cancel_paced_navigation(self) -> None:
        """Invalidate pending navigation during view or generation changes."""
        self._abandon_paced_navigation_state("paced navigation cancelled")

    def _end_navigation_hold(
        self,
        direction: int,
        *,
        source: Optional[QObject] = None,
    ) -> bool:
        """Stop held navigation; an already-requested exact step may still land."""
        if direction not in (-1, 0, 1):
            return False
        if source is not None and self._navigation_hold_source is not source:
            return False
        if direction and self._held_navigation_direction != direction:
            return False
        if self._held_navigation_direction == 0:
            return False
        self._held_navigation_direction = 0
        self._navigation_hold_source = None
        self._paced_navigation_queue.clear()
        self._navigation_repeat_started = False
        self._navigation_hold_timer.stop()
        if direction:
            # A real key release identifies the final target precisely. If its
            # frame is still decoding/presenting, preserve this request until
            # that frame swaps; otherwise submit cover quality immediately.
            self._restart_quality_decode_timer(immediate=True)
        elif (
            self._paced_navigation_pending is None and not self._paced_navigation_queue
        ):
            self._restart_quality_decode_timer()
        return True

    @Slot()
    def _paced_navigation_tick(self) -> None:
        direction = (
            self._paced_navigation_queue.popleft()
            if self._paced_navigation_queue
            else self._held_navigation_direction
        )
        if direction:
            if self.debug_cache:
                self._restart_nav_settle_timer()
            started = self._request_paced_navigation_step(direction)
            if (
                not started
                and self._paced_navigation_pending is None
                and self._paced_present_gate is None
            ):
                self._resume_paced_navigation()

    # Adaptive-cadence governor tuning: grow the interval while the decoded
    # look-ahead buffer is at or below LOW, relax while at or above HIGH, and
    # hold steady in between (the hysteresis band prevents oscillation).
    _PACING_LOW_DEPTH = 2
    _PACING_HIGH_DEPTH = 6
    _PACING_GROW = 1.25
    _PACING_RELAX = 0.85

    def _effective_navigation_interval_ms(self) -> float:
        """Current held-navigation cadence, including any adaptive stretch."""
        if not self._navigation_adaptive_pacing:
            return float(self._navigation_interval_ms)
        return self._paced_interval_ms

    def _update_adaptive_navigation_pacing(self, direction: int) -> None:
        """Adjust the held-key cadence from decoded look-ahead feedback."""
        if not self._navigation_adaptive_pacing or not self._held_navigation_direction:
            # Single taps are human-paced; only a held key follows the timer.
            return
        depth = self._fast_buffer_depth(self.current_index, direction)
        previous = self._paced_interval_ms
        if depth <= self._PACING_LOW_DEPTH:
            self._paced_interval_ms = min(
                self._paced_interval_max_ms,
                previous * self._PACING_GROW,
            )
        elif depth >= self._PACING_HIGH_DEPTH:
            self._paced_interval_ms = max(
                float(self._navigation_interval_ms),
                previous * self._PACING_RELAX,
            )
        if not self.debug_cache:
            return
        with self._nav_trace_lock:
            if self._burst is not None:
                self._burst["pace_intervals"].append(self._paced_interval_ms)
        if self.debug_cache_trace and abs(self._paced_interval_ms - previous) >= 0.05:
            log.info(
                "[NAVTRACE] pacing interval=%.1fms->%.1fms depth=%d",
                previous,
                self._paced_interval_ms,
                depth,
            )

    def _resume_paced_navigation(self) -> None:
        """Continue queued/held input after a request could not be published."""
        if self._paced_navigation_queue or self._held_navigation_direction:
            self._navigation_hold_timer.setInterval(
                max(1, round(self._effective_navigation_interval_ms()))
            )
            self._navigation_hold_timer.start()
        else:
            self._restart_quality_decode_timer()

    @Slot()
    def _on_paced_present_timeout(self) -> None:
        """Recover a held key when a QML presentation acknowledgement is missed."""
        with self._nav_trace_lock:
            gate = self._paced_present_gate
            if gate is None:
                return
            if not self._paced_present_ready:
                waiting_for = "Image.Ready"
            elif gate.get("sync_t") is None:
                waiting_for = "scene-graph synchronization"
            else:
                waiting_for = "frame swap"
            self._paced_present_gate = None
            self._paced_present_ready = False
        elapsed_ms = (time.perf_counter() - gate["started_t"]) * 1000.0
        log.warning(
            "Navigation presentation acknowledgement timed out for seq=%s "
            "waiting for %s after %.0fms (watchdog budget %dms); "
            "continuing held navigation",
            gate.get("seq"),
            waiting_for,
            elapsed_ms,
            self._navigation_gate_watchdog_timer.interval(),
        )
        if self._held_navigation_direction:
            # The watchdog interval already includes the initial keyboard repeat
            # delay, so resume at the normal paced cadence.
            self._navigation_repeat_started = True
        self._resume_paced_navigation()

    def _request_paced_navigation_step(self, direction: int) -> bool:
        """Request one adjacent image without exposing an unready source to QML."""
        if direction not in (-1, 1) or not self._loupe_decode_allowed():
            return False
        if (
            self._paced_navigation_pending is not None
            or self._paced_present_gate is not None
        ):
            # At most one superseding direct step is useful while a frame is in
            # flight. Physical key repeat never reaches this path; coalescing is
            # a defensive bound for forwarded/programmatic activations.
            if not self._paced_navigation_queue:
                self._paced_navigation_queue.append(direction)
            elif self._paced_navigation_queue[-1] != direction:
                self._paced_navigation_queue.clear()
                self._paced_navigation_queue.append(direction)
            return False

        target_index = self.current_index + direction
        if target_index < 0 or target_index >= len(self.image_files):
            return False

        if self._held_navigation_direction == 0:
            self._quality_decode_token += 1
            self._quality_decode_timer.stop()
            self.prefetcher.cancel_pending_cover_tasks()

        self._update_adaptive_navigation_pacing(direction)
        self._last_navigation_direction = direction
        nav_seq = None
        if self.debug_cache:
            nav_seq = self._seed_nav_record(target_index, direction=direction)
        else:
            self._nav_seq += 1
            nav_seq = self._nav_seq
            self._current_nav_seq = nav_seq
        # Shift directly to the target window before checking its cache entry.
        # The later commit reuses this update instead of repeating the same
        # scheduling work after current_index changes.
        self._do_prefetch(
            target_index,
            is_navigation=True,
            direction=direction,
        )

        display_width, display_height, display_generation = self.get_display_info()
        target_path = self.image_files[target_index].path
        cache_quality = "fast" if display_width > 0 and display_height > 0 else "cover"
        target_key = build_cache_key(
            target_path,
            display_generation,
            cache_quality,
        )
        if self.image_cache.get(target_key) is not None:
            self._commit_paced_navigation(target_index, direction)
            return True

        buffer_depth = self._fast_buffer_depth(self.current_index, direction)
        self._record_navigation_underflow(
            target_index,
            direction,
            buffer_depth,
        )
        epoch = self._paced_navigation_epoch
        future = self.prefetcher.submit_task(
            target_index,
            self.prefetcher.generation,
            priority=True,
            quality="fast",
            nav_seq=nav_seq,
            window_bound=False,
        )
        if future is None:
            self._resume_paced_navigation()
            return False

        pending = {
            "future": future,
            "epoch": epoch,
            "index": target_index,
            "direction": direction,
            "path_key": self._key(target_path),
            "display_generation": display_generation,
            "cache_quality": cache_quality,
            "nav_seq": nav_seq,
        }
        self._paced_navigation_pending = pending

        def _on_done(fut, *, request=pending):
            try:
                result = fut.result()
                error = None
            except Exception as exc:  # pylint: disable=broad-exception-caught
                result = None
                error = exc
            self._pacedNavigationReady.emit(
                {
                    **request,
                    "result": result,
                    "error": error,
                }
            )

        future.add_done_callback(_on_done)
        return False

    @Slot(object)
    def _on_paced_navigation_ready(self, payload) -> None:
        """Commit a worker-completed adjacent image on the GUI thread."""
        pending = self._paced_navigation_pending
        if not payload or pending is None:
            return
        if payload.get("future") is not pending.get("future"):
            return
        self._paced_navigation_pending = None

        if payload.get("epoch") != self._paced_navigation_epoch:
            return
        error = payload.get("error")
        result = payload.get("result")
        if error is not None:
            log.debug("Paced navigation decode failed: %s", error)
        if result is None:
            # Decode failures normally yield a cached broken-image placeholder.
            # A None here represents cancellation/shutdown; a held key may retry
            # on its next cadence tick without ever changing the visible source.
            self._resume_paced_navigation()
            return

        target_index = int(payload.get("index", -1))
        direction = int(payload.get("direction", 0))
        if target_index != self.current_index + direction:
            self._resume_paced_navigation()
            return
        if not (0 <= target_index < len(self.image_files)):
            self._resume_paced_navigation()
            return

        decoded_path, decoded_generation = result
        if decoded_generation != payload.get("display_generation"):
            self._resume_paced_navigation()
            return
        if self._key(Path(decoded_path)) != payload.get("path_key"):
            self._resume_paced_navigation()
            return
        result_key = build_cache_key(
            decoded_path,
            decoded_generation,
            payload.get("cache_quality", "fast"),
        )
        if self.image_cache.get(result_key) is None:
            self._resume_paced_navigation()
            return

        self._commit_paced_navigation(target_index, direction)

    def _commit_paced_navigation(self, target_index: int, direction: int) -> None:
        """Publish a cache-ready adjacent target without catch-up acceleration."""
        if target_index != self.current_index + direction:
            return
        commit_started = time.perf_counter()
        self._navigation_hold_timer.stop()
        with self._nav_trace_lock:
            self._paced_present_gate = {
                "seq": self._current_nav_seq,
                "started_t": commit_started,
                "due_t": commit_started
                + self._effective_navigation_interval_ms() / 1000.0,
                "sync_t": None,
            }
            self._paced_present_ready = False
        self._navigation_gate_watchdog_timer.start()
        self._set_current_index(
            target_index,
            direction=direction,
            prefetch_already_updated=True,
            paced_presentation=True,
        )
        if self.current_index != target_index:
            with self._nav_trace_lock:
                self._paced_present_gate = None
                self._paced_present_ready = False
            self._navigation_gate_watchdog_timer.stop()
            self._resume_paced_navigation()
            return
        # _set_current_index may mint a fresh sequence for the target: in
        # debug-cache mode a source refresh during the pending decode
        # re-targets the navigation records at the displayed image, so the
        # target is seeded again when it becomes current. QML captures that
        # final sequence for the new source, and the gate must carry the same
        # value or the target's Ready event can never release it and held
        # navigation stalls until the watchdog fires.
        with self._nav_trace_lock:
            gate = self._paced_present_gate
            if gate is not None and gate.get("seq") != self._current_nav_seq:
                gate["seq"] = self._current_nav_seq
