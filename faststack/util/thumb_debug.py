"""Thumbnail pipeline debug logging and performance tracing."""

import logging
import time
import threading
import itertools
from contextlib import contextmanager
from typing import Dict, Optional, Any
from pathlib import Path

log = logging.getLogger(__name__)

# Global configuration
timing_enabled = False
trace_enabled = False

# Global RID counter
_rid_counter = itertools.count(1)

# Interval statistics
_stats_lock = threading.Lock()
_interval_stats = {
    # Provider stats
    "req_total": 0,
    "req_cache_hit": 0,
    "req_cache_miss": 0,
    # Decode stats
    "decode_submitted": 0,
    "decode_coalesced": 0,
    "decode_started": 0,
    "decode_done_ok": 0,
    "decode_done_fail": 0,
    "decode_cancelled": 0,
    # Summary helpers
    "total_ms": 0.0,
    "max_ms": 0.0,
    "inflight": 0,
    "qdepth": 0,
    "qdepth_max": 0,
}

_last_summary_time = time.monotonic()
_summary_interval = 5.0


def init(timing: bool = False, trace: bool = False):
    """Initialize thumbnail debug logging globals."""
    global timing_enabled, trace_enabled
    timing_enabled = timing
    trace_enabled = trace
    if timing_enabled:
        log.info("Thumbnail timing logs ENABLED")
    if trace_enabled:
        log.info("Thumbnail trace logs ENABLED")


def log_trace(event: str, **kwargs):
    """Log a verbose trace event if enabled."""
    if not trace_enabled:
        return

    parts = [f"thumbtrace event={event}"]
    for k, v in kwargs.items():
        parts.append(f"{k}={v}")

    log.info(" ".join(parts))


def inc(name: str, delta: Any = 1):
    """Increment a counter statistic."""
    if not (timing_enabled or trace_enabled):
        return
    with _stats_lock:
        if name in _interval_stats:
            _interval_stats[name] += delta
            if name == "total_ms" and delta > _interval_stats["max_ms"]:
                _interval_stats["max_ms"] = delta


def gauge(name: str, value: Any):
    """Set a gauge statistic."""
    if not (timing_enabled or trace_enabled):
        return
    with _stats_lock:
        if name in _interval_stats:
            _interval_stats[name] = value
            if name == "qdepth":
                _interval_stats["qdepth_max"] = max(
                    _interval_stats["qdepth_max"], value
                )


def inc_request_count():
    """Thread-safe increment for the global request counter."""
    if not (timing_enabled or trace_enabled):
        return
    with _stats_lock:
        _interval_stats["req_total"] += 1
        return _interval_stats["req_total"]


def record_stat(name: str, value: Any):
    """Deprecated: use inc() or gauge() instead."""
    if name == "done":
        inc("decode_done_ok")
    elif name == "hit":
        inc("req_cache_hit")
    elif name == "miss":
        inc("req_cache_miss")
    elif name == "cancel":
        inc("decode_cancelled")
    elif isinstance(value, (int, float)) and name in ("inflight", "qdepth"):
        gauge(name, int(value))
    else:
        inc(name, 1 if value is None else int(value))


class ThumbTimer:
    """Timer and tracer for a single thumbnail request."""

    def __init__(self, key: str, path: Optional[Path] = None, reason: str = "unknown"):
        self.rid = next(_rid_counter)
        self.key = key
        self.path = path
        self.reason = reason
        self.stages: Dict[str, float] = {}
        self._current_stage: Optional[str] = None
        self._stage_start: float = 0.0
        self.cancelled = False
        self.started = False

        # Timestamps (perf_counter)
        self.t_requested = time.perf_counter()
        self.t_queued: Optional[float] = None
        self.t_worker_start: Optional[float] = None
        self.t_done: Optional[float] = None

        # Priority info
        self.prio_submitted: Optional[int] = None
        self.prio_effective: Optional[int] = None
        self.coalesced_from: Optional[str] = None

        # Short path for logging
        self.src = str(path.name) if path else "none"

    @contextmanager
    def stage(self, name: str):
        """Context manager to time a pipeline stage."""
        if not timing_enabled and not trace_enabled:
            yield
            return

        t0 = time.perf_counter()
        log_trace("stage_start", rid=self.rid, stage=name)
        try:
            yield
        finally:
            dt = (time.perf_counter() - t0) * 1000
            self.stages[name] = self.stages.get(name, 0.0) + dt
            log_trace("stage_end", rid=self.rid, stage=name, ms=f"{dt:.2f}")

    def log_timing(self, **kwargs):
        """Log the final timing summary for this request."""
        if not timing_enabled:
            return

        now = time.perf_counter()
        total_ms = (now - self.t_requested) * 1000
        inc("total_ms", total_ms)

        # Base parts
        parts = [
            "thumbtiming",
            f"rid={self.rid}",
            f"key={self.key}",
            f"src={self.src}",
            f"reason={self.reason}",
        ]

        # Priority info
        if self.prio_submitted is not None:
            parts.append(f"prio={self.prio_submitted}")
        if (
            self.prio_effective is not None
            and self.prio_effective != self.prio_submitted
        ):
            parts.append(f"prio_eff={self.prio_effective}")
        if self.coalesced_from:
            parts.append(f"coalesced_from={self.coalesced_from}")

        # Overall timing
        parts.append(f"total_ms={total_ms:.1f}")

        # Phase timings
        if self.t_queued is not None:
            sched_ms = (self.t_queued - self.t_requested) * 1000
            parts.append(f"sched_ms={sched_ms:.1f}")

            if self.t_worker_start is not None:
                wait_ms = (self.t_worker_start - self.t_queued) * 1000
                parts.append(f"wait_ms={wait_ms:.1f}")

                if self.t_done is not None:
                    worker_ms = (self.t_done - self.t_worker_start) * 1000
                    parts.append(f"worker_ms={worker_ms:.1f}")

        # Stage breakdown
        for stage, ms in self.stages.items():
            parts.append(f"{stage}_ms={ms:.1f}")

        # Extra tags
        for k, v in kwargs.items():
            parts.append(f"{k}={v}")

        log.info(" ".join(parts))


def check_periodic_summary():
    """Print the periodic summary if the interval has passed."""
    if not (timing_enabled or trace_enabled):
        return

    global _last_summary_time
    now = time.monotonic()
    if now - _last_summary_time < _summary_interval:
        return

    with _stats_lock:
        stats = _interval_stats.copy()
        # Reset interval stats
        # NOTE: inflight and qdepth are persistent states, do NOT reset them to 0.
        # qdepth_max is reset so we see the max for the NEW interval.
        for k in [
            "req_total",
            "req_cache_hit",
            "req_cache_miss",
            "decode_submitted",
            "decode_coalesced",
            "decode_started",
            "decode_done_ok",
            "decode_done_fail",
            "decode_cancelled",
            "total_ms",
            "max_ms",
            "qdepth_max",
        ]:
            if k in _interval_stats:
                _interval_stats[k] = 0
        _last_summary_time = now

    # Summary is useful if ANYTHING happened or if there is work in flight
    activity = (
        stats["req_total"]
        + stats["decode_submitted"]
        + stats["decode_done_ok"]
        + stats["decode_cancelled"]
        + stats["inflight"]
    )
    if activity == 0:
        return

    avg_ms = (
        stats["total_ms"] / stats["decode_done_ok"]
        if stats["decode_done_ok"] > 0
        else 0
    )

    log.info(
        f"thumbtiming-summary "
        f"REQ[tot={stats['req_total']} hit={stats['req_cache_hit']} miss={stats['req_cache_miss']}] "
        f"DEC[sub={stats['decode_submitted']} coal={stats['decode_coalesced']} start={stats['decode_started']} ok={stats['decode_done_ok']} fail={stats['decode_done_fail']} can={stats['decode_cancelled']}] "
        f"avg_ms={avg_ms:.1f} max_ms={stats['max_ms']:.1f} "
        f"inflight={stats['inflight']} qdepth={stats['qdepth']} qdepth_max={stats['qdepth_max']}"
    )
