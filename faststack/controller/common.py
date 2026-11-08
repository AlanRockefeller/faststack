"""Helpers shared by the AppController mixins."""

import logging
import sys
from typing import Optional

log = logging.getLogger(__name__)


def app_debug_mode() -> bool:
    """The --debug flag. main() sets it on faststack.app at startup."""
    app_module = sys.modules.get("faststack.app")
    return bool(getattr(app_module, "_debug_mode", False))


def _worker_unaccounted_ms(stats: dict) -> Optional[float]:
    """Worker wall time not claimed by any measured decode phase.

    total_ms spans the whole worker call. Only the top-level phases are summed:
    decode_ms already contains read/jpeg/resize/metadata, so adding those again
    would overstate the accounted time and hide a real gap behind the clamp.
    Anything left over is time the thread was not doing decode work. Returns
    None when the trace is too sparse to subtract.
    """
    total = stats.get("total_ms")
    if not isinstance(total, (int, float)):
        return None
    accounted = 0.0
    for key in ("setup_ms", "decode_ms", "icc_ms", "finalize_ms", "cache_ms"):
        value = stats.get(key)
        if isinstance(value, (int, float)):
            accounted += value
    return max(0.0, total - accounted)
