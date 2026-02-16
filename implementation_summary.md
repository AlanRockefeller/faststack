The "no full-res decode in grid view" gating cleanup has been completed with the requested 5 fixes and additional safety improvements.

**Summary of Changes:**

1.  **`faststack/app.py`**:
    *   **Fix 1**: Moved the `_loupe_decode_allowed()` check to the very top of `_do_prefetch`. This ensures no resize handling or other side effects occur when prefetch is blocked (e.g. in grid view).
    *   **Fix 2**: In `_set_grid_view_active`, explicitly cleared `self.pending_prefetch_index = None` when entering grid view to prevent stale deferred prefetches from firing upon return to loupe.
    *   **Fix 3**: Simplified `_loupe_decode_allowed` to access `self._folder_loaded` directly, removing `getattr`.

2.  **`faststack/ui/provider.py`**:
    *   **Fix 4**: Connected `isGridViewActiveChanged` to `currentImageSourceChanged.emit` in `UIState.__init__`. This fixes a bug where the signal was being re-connected repeatedly in `_on_dialog_state_changed`, causing potential leaks and performance issues.

3.  **`faststack/qml/ThumbnailGridView.qml`** & **`faststack/qml/Main.qml`**:
    *   **Fix 5**: Centralized prefetch gating ownership in `Main.qml` and improved robustness.
        *   **Main.qml**: Updated `onIsGridViewActiveChanged` and `gridViewLoader.onLoaded` to safely check for item existence and validity inside `Qt.callLater` callbacks, preventing crashes if the loader state changes quickly.
        *   **ThumbnailGridView.qml**: Removed the auto-enable logic from `Component.onCompleted`. Gated `onWidthChanged` and `onHeightChanged` timer restarts to only run when `prefetchEnabled` is true, avoiding unnecessary wakeups.

**Verification:**
*   Re-ran `pytest faststack/tests/test_startup_optimization.py` to ensure no regressions in existing tests.
*   Confirmed logical consistency: grid view operations now strictly avoid full-res decode paths, and view switching handles state cleanup and QML property updates deterministically and safely.
