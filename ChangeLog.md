# ChangeLog

Todo:   More testing Linux / Mac.   Create Windows .exe.   Write better documentation / help.   Add splash screen / icon.   Fix raw image support.

## 1.6.2 (2026-03-28)

- Added a reusable soft-mask subsystem for local adjustments (mask model, mask engine, masked operations).
- Added a Background Darkening tool (K key) as the first consumer of the mask system.
  - Paint rough background hints (left-click) and subject protection (right-click).
  - Strokes act as smart hints combined with image analysis, not hard mattes.
  - Modes: Assisted, Paint Only, Strong Subject Protection, Border-Connected Auto.
  - Controls: darken amount, edge protection, subject protection, feather, dark range, neutrality, expand/contract, auto from edges.
  - Configurable mask overlay (blue default, selectable colours, toggle visibility).
  - Darkening uses pedestal subtraction + multiplicative darkening for natural results.
  - Mask data stored in normalised coordinates, survives rotate/straighten/crop changes.
- Added "Darken Background (K)" button in the Image Editor effects section.
- J and K keys no longer navigate to next/previous image. Use arrow keys instead.
- K key now opens the Background Darkening tool (works from loupe view or inside the editor).

## 1.6.1 (2026-03-13)

- Added a slim custom title bar with hover-revealed menus.
- Added custom minimize, maximize, and close buttons for the frameless window.
- Added a zoom indicator in the title bar and the current directory path in the status bar.
- Moved menu activation from hovering over the image to hovering over the title bar.
- Expand the default prefetch window from a radius of 4 to 12 images.
- Introduce directional awareness to task cancellation logic, making the prefetcher a lot faster.
- Improved TurboJPEG setup on Windows by using shared library detection logic in JPEG decoding and thumbnail prefetching.  Thanks to Andy Arijs for the PR!
- Added Windows documentation for installing turbojpeg.dll, using FASTSTACK_TURBOJPEG_LIB, and understanding fallback behavior.  Thanks to Andy Arijs!
- FastStack now more clearly explains when it falls back to Pillow for JPEG decoding and thumbnails.  Thanks to Andy Arijs!
- Recycle bin restore is now per-directory: each bin shows its destination, file counts, and an independent Restore button
- Bins with legacy files that cannot be auto-restored are clearly labeled instead of silently ignored
- Restore feedback reports skipped files and legacy remainders
- RAW decode failures now show a distinct "Preview unavailable" placeholder instead of a plain dark image

## 1.6.0 (2026-03-06) 

- Added a "Todo" flag: toggle with D, filterable in Filter dialog, shown on thumbnails (badge, tile visuals, red on sparkline), and displayed as "Todo since {date}" in the UI.
- Fixed batch range alignment after deletions to prevent stale/misaligned UI state.
- Improved cross‑platform thumbnail path matching, more robust shutdown/teardown, and reduced eviction-related UI races.
- Fixed image list refresh after filesystem watcher events so the current image stays selected when possible.
- Fixed undo and rollback so deleted images are restored to the correct positions.
- Fixed stack state being lost or shifted incorrectly after delete and undo operations.
- Improved display cache invalidation so zoom, resize, filter, and edit changes refresh the correct image version.
- Improved prefetch behavior when zooming or resizing to reduce stale background work.
- Improved thumbnail lookup speed by adding a faster path-to-row mapping.
- Reduced chances of UI state getting out of sync after external file changes.
- Added `@overload` type hints to `SidecarManager.get_metadata` to provide strict static typing based on the state of the `create` parameter.
- Modified `SidecarManager.get_metadata` to accept a `create` boolean parameter. When `create=False`, the method now returns `None` instead of instantiating and saving an empty metadata entry.
- Updated `AppController` read-only operations (such as thumbnail dictionary generation, status checks, and batching) to request metadata with `create=False`.
- Refactored `AppController` flag extraction (e.g., `uploaded`, `favorite`) to explicitly handle `None` values, replacing older, bulky type-checking logic that looked for both `dict` and `object` structures.

## 1.5.9 (2026-02-16)

- Full-Screen Mode: Press F11 to toggle fullscreen in loupe view 
- Spark Line Display: Grid view now shows upload progress indicators per folder.
- Optimized grid view performance and prefetch behavior.
- Added EXIF brief display in status bar showing ISO, aperture, shutter speed, and capture time.
- Enhanced metadata display with camera-style shutter speed formatting.
- Added new thumbnail badges for Backups (Bk) and Developed (D) variants.
- Improved cache eviction handling and thread-safety for concurrent operations.
- Fixed a bug where deleting an image could mess up the batch selection ranges if the delete was cancelled, failed, or undone.

## 1.5.8 (2026-02-13)

- Instant delete: move recycle/permanent delete to background thread; debounce refresh; improved undo handling.
- Users can now filter by flags (uploaded/stacked/edited/restacked/favorite)
- Fixed bugs in grid view
- Added **Jump to Last Uploaded** (Alt+U + menu item) to jump to the most recently-uploaded photo in the folder.
- Improved **shutdown safety**: saving and delete/recycle operations now finish cleanly on exit to avoid data loss.
- Improved **thumbnail responsiveness**: visible thumbnails are now queued with higher priority than background prefetch.
- Improved **prefetch stability/performance**: prefetch work runs on daemon threads and cleans up finished futures.
- UI tweaks: recycle-bin details text is selectable and uses updated colors; metadata filename now shows RAW extension when present (e.g., `IMG_0001.JPG + ORF`).
- Helicon Focus: Avoid a race condition by deferring deletion of temporary file lists until app shutdown.

## 1.5.7 (2026-02-09)

- Auto levels is now much faster!
- Images can now be tagged as favorite, and there is a menu item to add favorited images to the batch.
- Avoid full directory rescan after quick saves by inserting the backup file into the cached list via bisect using indexer sort rules.
- Speed up AWB (Lab) by subsampling from editor float_image; add no-op thresholds + clearer “direction” labels.
- Improve auto-levels/AWB UX: detailed status messages and per-stage timing logs (compute/save/list/total).
- Track last auto-levels detail string for “saved” message reuse; minor import/indexer integration tweaks.
- Centralize canonical image sort key in indexer; store developed adjacency name on ImageFile.
- Sync filename filter to the thumbnail grid model (apply/clear) and cancel stale thumbnail prefetch jobs so filtered thumbnails load quickly.
- Add debug timing logs for auto-levels and auto white balance (subsample/mask/Lab compute) to pinpoint slow stages.
- Add debug-only timing breakdowns for image load, auto-levels percentile analysis, and save pipeline in `ImageEditor`.
- Refactor `ThumbnailModel` filtering into `set_filter()` with an active filter state; assert refresh runs on the GUI thread to catch threading mistakes.`
- Export performance: Skip the expensive sRGB→Linear→sRGB round-trip when no linear-space edits are active (WB/exposure/highlights/shadows/clarity/texture/sharpness), and clamp export output to [0,1] on that path.
- Save performance: Avoid float_image.copy() during export when the edit set guarantees the pipeline won’t mutate the input buffer.
- Load performance: Apply EXIF orientation on the 8-bit Pillow path before float conversion (rotate uint8), and only rotate the float buffer on the 16-bit OpenCV path.
- Logging/robustness: Switch warnings/errors to lazy log formatting and improve load/save diagnostics.
- Quick Auto Levels saves are faster for regular JPGs by using a lightweight “levels-only” save path when possible.
- Folder refreshes from filesystem changes are now debounced (grouped together), so you get fewer slow rescans during saves.
- Backup images (`*-backup.jpg`, `*-backup2.jpg`, etc.) are no longer shown in the image list.


## 1.5.6 (2026-02-08)

### Performance
- Debounced `metadataChanged` / `highlightStateChanged` emissions to reduce UI overhead during rapid navigation.
- Increased default prefetch radius to **6** and raised prefetch worker cap to **8** for smoother fast navigation.
- Added optional `[DBGCACHE]` timing logs for image request/decode and UI refresh paths when `debug_cache` is enabled.

### Stability
- Refactored shutdown into `shutdown_qt()` (main thread) and `shutdown_nonqt()` (background-safe), wired from `aboutToQuit` in `main()` with a timeout/stacks fallback to diagnose hangs.
- Added cooperative cancellation and `cancel_futures=True` shutdown behavior to both main image and thumbnail prefetchers.
- Ensured thumbnail “ready” callbacks run on the Qt thread when available; hardened cancellation/callback ordering to avoid deadlocks and worker-thread Qt warnings.
- Enabled Ctrl-C termination via SIGINT handling and a periodic Qt timer to allow Python signal processing.


## 1.5.5 (2026-02-07)

### Changed
- Image save behavior in the editor is now navigation-aware:
  - Only clear editor state / close editor UI when the user is still on the same image.
  - Only perform a full list refresh + re-select logic when the user is still on the same image.
  - If the user navigated away, preserve their selection and only invalidate the saved image’s cache entry.

- Recycle/delete of JPG+RAW pairs is now more atomic and robust:
  - Check RAW existence **before** any moves to avoid post-move existence ambiguity.
  - Move JPG first; only attempt RAW move if JPG succeeds and RAW existed.
  - If RAW move fails after JPG succeeds, roll back the JPG move to keep pairs consistent.
  - Track `raw_moved` based on whether RAW existed and whether it was moved successfully.

- Cache invalidation after edits is now targeted instead of global:
  - Replace multiple `image_cache.clear()` calls after save/export with `image_cache.pop_path(saved_path)` to invalidate only the edited file.

- Keep internal path→index lookup consistent:
  - Rebuild the path-to-index map after operations that mutate the image list, including after recycle/rollback flows.

### Fixed
- Rotation/autocrop and straighten edge handling:
  - Use `floor()` instead of `round()` in inscribed-rectangle and crop coordinate math to reduce off-by-one drift.
  - Skip inset trimming for exact 90° rotations to preserve full dimensions and avoid unnecessary cropping.

## 1.5.4 (2026-02-04)

### Fixed
- Image rotation fixed - no more black wedges on the edges of the image.
- Prevented “undo delete” from resurrecting files when recycle/rollback fails: if a JPG can’t be restored after a partial delete, it’s marked as deleted (`jpg_moved=True`), a warning is shown, and a `recycled_jpg_path` breadcrumb is stored for potential cleanup.
- Improved crop behavior when straightening/rotating with `expand=True` by transforming crop coordinates from original image space into the expanded canvas.
- Prevented exporting with stale preview-resolution blur caches by validating cached array shapes against the current Y channel dimensions.
- Improved highlight recovery by switching to an adaptive rational compression shoulder (new `k` parameter) and added tests for identity-at-zero, pivot invariance, and increasing compression with higher amount.
- Fixed QML empty-state message timing by only showing “No images in this folder” after the folder has been scanned at least once.
- Improved Escape key reliability during crop/rotation by explicitly re-focusing the loupe view.

### Changed
- Refactored deletion into a unified core deletion engine (`_delete_indices`) shared by loupe, grid cursor, grid selection, and batch deletion paths.
- Deletion now uses an optimistic UI update for instant feedback, with deferred/coalesced disk refresh to avoid flicker and “deleted items reappear” issues.
- Grid deletion now supports multi-selection and cursor deletion through a single entry point, rebuilding the path→index mapping for reliable lookup.
- Image saving is now offloaded to a background thread to keep the UI responsive:
  - Added an `isSaving` state to disable Save actions and show “Saving…” feedback.
  - Prevented “surprise close” by only auto-closing the editor if the user is still on the same image when the save completes.
- Improved recycle-bin cleanup on quit:
  - Replaced the simple message dialog with a richer dialog showing per-bin counts (JPG/RAW/other) and an optional detailed file list.

### UI
- Resized the Image Editor dialog to accommodate the saving state/controls.
- Enhanced recycle bin cleanup dialog layout and interaction (expandable detailed list, clearer button actions).


## 1.5.3 (2026-01-27)

### Added
- New **Thumbnail Grid View** (folder-style browser) with a fast thumbnail pipeline:
  - `ThumbnailModel`, `ThumbnailProvider`, `ThumbnailPrefetcher`, `ThumbnailCache`, and `PathResolver` integrated into `AppController`.
  - App now defaults to starting in grid view (thumbnail mode) and initializes the model/resolver on startup.
  - Registered a dedicated QML image provider (`thumbnail://...`) and exposed `thumbnailModel` to QML.
- UI controls to switch between **Thumbnail View** and **Single Image View**:
  - Menu item in the actions menu to toggle views.
  - `T` shortcut to toggle grid/loupe view.
  - Grid-mode status bar controls for selection count, clear selection, refresh, and quick return to single image.

### Changed
- Implemented grid/loupe view switching using a `StackLayout` in `Main.qml` to keep both views loaded and preserve state while toggling.
- Improved grid-to-loupe opening performance by adding an O(1) resolved-path → index map (`_path_to_index`) for quick lookup when opening an image from the grid.
- Directory changes now refresh thumbnail infrastructure:
  - Clear thumbnail cache before refresh to avoid stale thumbnails.
  - Update model directories, refresh, update resolver, and emit `gridDirectoryChanged`.
- Grid selection count is now exposed efficiently to QML via `uiState.gridSelectedCount` (avoids copying full selected-path lists just to display counts).

### Fixed
- Thread-safety for thumbnail completion callbacks:
  - Thumbnail decode completion now hops to the GUI thread via an internal signal (`_thumbnailReadySignal`) using an explicit `Qt.QueuedConnection`.
  - Added guards to avoid model updates during/after shutdown.
- Added shutdown safety for thumbnail prefetcher (guard against partial initialization).


## 1.5.2 (2026-01-25)

### Added
- **Highlight recovery telemetry + UI indicators**
  - New highlight state analysis in the editor pipeline (headroom/clipping/near-white metrics) and a UI signal (`highlightStateChanged`) to keep it live.
  - Image editor dialog now shows **Headroom** and **Clipped** indicators under the histogram when relevant.
- **Unified EXIF orientation handling**
  - Editor now **bakes EXIF orientation into pixels on load** (Pillow original + float master buffer), and saving **sanitizes Orientation to 1** to prevent double-rotation in viewers.
  - Prefetcher now applies EXIF orientation in a **single unified post-decode block**, reusing pre-read EXIF when available.
- **Optional OpenCV dependency support**
  - Centralized optional OpenCV usage (`optional_deps`) with fallbacks (e.g., Pillow-based Gaussian blur fallback when OpenCV is unavailable).
  - Tests updated to skip/patch appropriately when OpenCV isn’t installed.

### Changed
- **Save flow restored to “old behavior”**
  - Saving now: **closes editor → clears editor state → refreshes image list → reselects saved image → clears cache/prefetches → syncs UI**.
  - Save errors now surface as user-visible status messages with safer exception handling.
- **Histogram rendering and layout improvements**
  - Histogram panel height increased; channel labels expanded (Red/Green/Blue); non-minimal histogram display enabled.
  - Single-channel histogram drawing now downsamples using **max-pooling** when canvas width is smaller than bin count to reduce aliasing/spikes.
- **Slider double-click reset robustness**
  - Replaced heuristic double-click logic with `TapHandler` double-tap reset and removed competing `slider.value` writers for more deterministic behavior.
- **Color/edit pipeline tuning**
  - Contrast and saturation slider sensitivity reduced (scaled effect).
  - Headroom “safety” shoulder moved to linear space (`_apply_headroom_shoulder`) replacing the old sRGB-side shoulder.
  - Auto-levels now kicks the preview worker for immediate visual feedback; histogram updates are guarded by visibility in more places.

### Fixed
- **EXIF orientation “double rotation” bugs**
  - Saving now consistently drops/sanitizes EXIF when orientation can’t be safely serialized, preventing incorrect viewer rotations.
  - Developed JPG sidecar EXIF from a paired JPEG is sanitized for Orientation as well.
- **Prefetch stability under rapid scrolling**
  - ImageProvider keepalive queue increased (32 → 128) to reduce crashes from QML/texture lifetime mismatches during thrash.
- **RawTherapee Windows path detection**
  - Improved version selection by sorting detected installs via a version-aware path component key (e.g., `5.10` > `5.9`).


## [1.5.1] - 2026-01-23

### Added
- Added experimental RAW processing via Rawtherapee
- Added explicit **JPEG vs RAW editing modes** with UI + signals to keep QML and backend in sync (`editSourceModeChanged`, `saveBehaviorMessage`). RAW mode can develop to a 16-bit working TIFF and optionally write a `*-developed.jpg` output while leaving the original JPEG untouched.
- Added **RAW development workflow** via RawTherapee **CLI** (`rawtherapee-cli`) with configurable extra args, better error reporting, output validation, and timeout handling.
- Added editor quality upgrades: **16-bit aware editing pipeline** using float32 working buffers, sRGB↔linear conversions for “true headroom” edits, OpenCV Gaussian blur helpers, and new **Texture** control.
- Added editor metadata display in the window title (filename + detected bit depth).
- Added robust undo/restore helper (`_restore_backup_safe`) to better handle locked files and tricky restore scenarios.
- Added support for indexing and displaying `*-developed.jpg` images and **orphaned RAWs** in the browser list; updated pairing test expectations accordingly.

### Changed
- Reworked README installation instructions:
  - macOS recommended flow with **Python 3.12** (Homebrew) + venv + `pip install .`
  - Simplified run command (`faststack`) and clarified Windows/Linux steps.
- Switched RawTherapee path detection defaults from GUI executable to **CLI executable** on Windows/macOS/Linux.
- Improved Prefetcher decode behavior by using TurboJPEG **only for JPEGs**, with a Pillow fallback for non-JPEG formats or decode failures.
- Centralized navigation state changes (`_set_current_index`) and ensured edit mode resets appropriately on navigation (defaults back to JPEG unless RAW-only).

### Fixed
- Fixed editor memory usage by clearing large editor buffers when the editor closes and resetting cached preview state.
- Fixed a QML slider double-click reset edge case where the slider could remain in a pressed/dragging state (force release via a short disable/reenable tick).
- Fixed histogram scheduling/thread-safety issues by tightening locking around pending/inflight state and improving failure handling when preview data is missing or executor submission fails.



## [1.5.0] - 2025-12-01

- Fixed rotating images via the crop interface.
- Control-1 zooms to 1:1 magnification (100%).   Control-2 to 200, etc to control-4 (400%).

## [1.4.0] - 2025-12-01

- Changed how image caching works for even faster display.   
- Pressing H brings up a RGB histogram which is designed to show even a little bit of highlight clipping and updates as you zoom in.
- Added batch delete with confirmation dialog.
- Added the --cachedebug command line argument which gives info on the image cache in the status bar. Doesn't seem to slow down the program at all, just takes up room in the status bar.- Added a setting that switches between image display optimized for speed or quality.
- **Auto-Levels:** Automatic image enhancement with configurable threshold and strength (L key)
- **Image Metadata:** Extract and display EXIF metadata (I key)
- **Image Processing:** Auto white balance, texture enhancement, and straightening
- **Crop Operations:** Fixed crop functionality with rotation support

## [1.3.0] - 2025-11-23

- Added the ability to crop images, via the cr(O)p hotkey.   It can be a freeform crop, or constrained to several popular aspect ratios.   
- Sorts images by time.
- Added the Stack Source Raws feature in the Action menu - if you import your images with stackcopy.py --lightroomimport (https://github.com/AlanRockefeller/faststack) and you are viewing a photo stacked in-camera, this feature will open the raw images that made this stack in Helicon Focus.
- Some fixes to the image cache - it doesn't expire when it shouldn't, does expire when it should, and warns you when the cache is full so you can consider increasing the cache size in settings.


## [1.2.0] - 2025-11-22

- Fixed menus, they now work well and look cool.
- Updated auto white balance to make it better, and put some controls for it in the settings

## [1.1.0] - 2025-11-22

### Major Features
- **Built-in Image Editor:** Full-featured image editor with draggable window
  - Exposure, highlights, shadows, whites, blacks, brightness, contrast
  - White balance (Blue/Yellow and Magenta/Green axes)
  - Auto White Balance button using grey world assumption
  - Saturation, vibrance, clarity, sharpness
  - Vignette effect
  - Rotation (90°, 180°, 270°)
  - EXIF metadata preservation (GPS, camera settings, timestamps)
  - Press `E` to open editor, `Ctrl+S` to save
  - Sequential backup naming (filename-backup.jpg, filename-backup2.jpg, etc.)

- **Quick Auto White Balance:** Press `A` key for instant auto white balance
  - Uses grey world assumption algorithm
  - Automatically saves with backup
  - Full undo support with Ctrl+Z

- **Enhanced Batch Display:** Batch counter shows total selected images
  - `B` key toggles images in/out of batch selection

### UI/UX Improvements
- **Updated Key Bindings Dialog:** Added documentation for new features
  - Auto white balance (A key)
  - Image editor toggle (E key)  

## [1.0.0] - 2025-11-21

### Major Features
- **Batch Selection System:** New batch selection mode for drag-and-drop operations
  - `{` to begin batch, `}` to end batch, `\` to clear all batches
  - `X` or `S` keys remove individual images from batches/stacks (shrinks or splits ranges)
  - Batches automatically cleared after successful drag operation
  - Multiple files can now be dragged to browsers and external applications simultaneously
- **Manual Flag Toggles:** Added keyboard shortcuts to manually control metadata flags
  - `U` toggles uploaded flag
  - `Ctrl+E` toggles edited flag  
  - `Ctrl+S` toggles stacked flag
- **Edited Flag Tracking:** New metadata flag for images edited in Photoshop
  - Displays "Edited on [date]" in status bar (green)
  - Can be manually toggled with `Ctrl+E`
- **Jump to Image Dialog:** Press `G` to jump directly to any image by number
  - Dynamic input field sizing based on image count
  - Proper keyboard event capture while dialog is open

### UI/UX Improvements
- **Auto Zoom Reset:** Image view automatically resets to fit-window after drag operations
- **Smooth Window Dragging:** Fixed flickering when dragging title bar by using global coordinates
- **Status Bar Enhancements:** 
  - Added batch info display (green badge showing position/count)
  - Added uploaded status display
  - Added edited status display

### Bug Fixes
- **Multi-file Drag:** Simplified drag implementation to work correctly with Chrome and other browsers

## [0.9.0] - 2025-11-20

### Performance Improvements
- **Zero-Copy JPEG Read:** Eliminated memory copy by passing mmap directly to decoders, reducing I/O time by 25-60% for large JPEGs.
- **Filter Performance:** Cached image list in memory to eliminate disk scans on every filter keystroke (100-1000x faster for large directories).
- **Smart Cache Management:** Removed unnecessary cache clearing on resize/zoom - LRU naturally evicts old entries while allowing instant reuse.
- **Generation Thrashing Fix:** Navigation no longer increments generation counter, preventing cache invalidation on every keystroke.
- **Directional Prefetching:** Asymmetric prefetch now biases 70% ahead and 30% behind in travel direction for faster sequential browsing.
- **ICC Transform Caching:** Cached ICC color transforms to eliminate repeated transform builds during color-managed viewing.
- **TurboJPEG for ICC:** ICC color path now uses TurboJPEG for decode+resize, then Pillow only for color conversion.

### Features
- **JPG Fallback for Helicon:** Helicon Focus stacking now works with JPG-only workflows when RAW files absent.
- **Comprehensive Timing Instrumentation:** Added detailed decode timing logs in debug mode for performance analysis.- **Jump to Photo:** Press `G` to jump directly to any image (feature documented more fully in [1.0.0]).
- **Comprehensive Timing Instrumentation:** Added detailed decode timing logs in debug mode for performance analysis.
- **Jump to Photo:** Press `G` to jump directly to any image (feature documented more fully in [1.0.0]).

## [0.8.0] - 2025-11-20- Backspace key now deletes images (in addition to Delete key).   Control-Z restores.
- Photoshop integration now automatically uses RAW files when available, falling back to JPG.
- We now have some new color modes in the view menu to make the images in your monitor reflect reality.   ICC profile mode works best on my system - try it if the images are over-saturated - or turn down the saturation in saturation mode.   Test it out by loading an image in Faststack and Photoshop or another image viewer and make sure the colors look the same.

## [0.7.0] - 2025-11-20

### Added
- **High-DPI Display Support:** Images now render at full physical pixel resolution on 4K displays by accounting for `devicePixelRatio` in display size calculations.
- **Ctrl+0 Zoom Reset:** Added keyboard shortcut to reset zoom and pan to fit window (like Photoshop), with visual feedback.
- **Active Filter Indicator:** Footer now displays active filename filter in yellow bold text for better visibility.
- **Directory Path Display:** Title bar now shows the current working directory path, centered between menu and window controls.

### Fixed
- **Property Name Mismatch:** Corrected `get_stack_summary` to `stackSummary` in UIState to match QML property naming conventions.
- **FilterDialog Theme Support:** Enhanced FilterDialog with proper Material theme support and background styling for consistent dark/light mode appearance.
- **Missing Signal Emissions:** Added `stackSummaryChanged` signal emission when stacks are created, cleared, or processed.

### Changed
- **Improved Error Handling:** Replaced broad `Exception` catches with specific exception types (`OSError`, `subprocess.SubprocessError`, `FileNotFoundError`, `IOError`, `PermissionError`).
- **Better Logging:** Changed `log.error()` to `log.exception()` to include full tracebacks for debugging.
- **Argument Parsing:** Now uses `shlex.split()` with platform-aware parsing (Windows vs POSIX) for proper handling of quoted paths and special characters.

### Testing
- **Executable Validator Tests:** Added comprehensive test suite for executable path validation with 8 test cases covering various security scenarios.

## [0.6.0] - 2025-11-03

### Fixed
-   Resolved an issue where the prefetch range was not being applied correctly after changing the prefetch radius in settings.
-   Corrected `decode_jpeg_thumb_rgb` to ensure that thumbnails generated by PyTurboJPEG do not exceed the `max_dim` by falling back to Pillow resizing when necessary.
-   Addressed excessive metadata queries during application startup by deferring UI synchronization until after images are loaded.
-   Fixed a bug where the zoom state callback was not firing, leading to low-resolution images being served when zoomed in.
-   Resolved a QML error "Cannot assign to non-existent property 'scaleTransform'" by correctly placing the scale change handlers within the `Scale` transform.
-   Handled the empty image files case in preloading to prevent unnecessary processing and correctly update the UI.

## [0.5.0] - 2025-11-03

### Added
-   Load full-resolution images when zooming in for maximum detail.
-   Call Helicon Focus for each defined stack when multiple stacks are present.

### Changed
-   The filesystem watcher is now less sensitive to spurious modification events, reducing unnecessary refreshes.
-   The preloading process now shares the same thread pool as the prefetcher for better resource utilization.
-   Stacks are now cleared automatically after being sent to Helicon Focus.

### Fixed
-   Corrected a `ValueError` in `PyTurboJPEG` caused by unsupported scaling factors.
-   Resolved an `AttributeError` in the JPEG scaling factor calculation.
-   Fixed an issue where panning the image was not working correctly.
-   Addressed a bug where panning speed was incorrect at high zoom levels.
-   Ensured that stale prefetcher futures are cancelled when the display size changes.

### Performance
-   Improved image decoding performance by using `PyTurboJPEG` for resized decoding.
-   Tuned the number of prefetcher thread pool workers based on system CPU cores.
-   Replaced synchronous file reads with memory-mapped I/O for faster image loading.
-   Optimized image resizing by using `BILINEAR` resampling for large downscales.
-   Debounced display size change notifications to reduce redundant UI updates.

## Version 0.4

### Todo

Make it use the full res image when zooming in
When multiple stacks are selected, call Helicon multiple times
After Helicon is called, clear the stacks
Fix S key - I guess it should remove an image from the stack?   Clarify what it does now.

### New Features
- **Two-tier caching system:** Implemented a two-tier caching system to prefetch display-sized images, significantly improving performance and reducing GPU memory usage.
- **"Preload All Images" feature:** Added a new menu option under "Actions" to preload all images in the current directory into the cache, ensuring quick access even for unviewed images.
- **Progress bar for preloading:** Introduced a visual progress bar in the footer to display the status of the "Preload All Images" operation.

### Changes
- **Theming improvements:** Adjusted the Material theme to ensure the menubar background is black in dark mode, providing a more consistent user experience.
- **Window behavior:** Changed the application window to a borderless fullscreen mode, allowing for normal Alt-Tab behavior and better integration with the operating system.

## Version 0.3

### New Features
- Implemented a "Settings" dialog with the following configurable options:
  - Helicon Focus executable path (with validation).
  - Image cache size (in GB).
  - Image prefetch radius.
  - Application theme (Dark/Light).
  - Default image directory.

## Version 0.2

### New Features
- Added an "Actions" menu with the following options:
  - "Run Stacks": Launch Helicon Focus with selected files or all stacks.
  - "Clear Stacks": Clear all defined stacks.
  - "Show Stacks": Display a dialog with information about the defined stacks.
- Pressing the 'S' key now adds or removes a RAW file from the selection for processing.
- Implemented tracking for stacked images:
  - `EntryMetadata` now includes `stacked` (boolean) and `stacked_date` (string) fields.
  - `launch_helicon` records stacking status and date upon successful launch.
  - The footer in `Main.qml` displays "Stacked: [date]" for previously stacked images.

### Changes
- Pressing the 'Enter' key will now launch Helicon Focus with the selected RAW files. If no files are selected, it will launch with all defined stacks.
- Refactored the theme toggling logic in `Main.qml` to use a boolean `isDarkTheme` property for more robustness.

### Bug Fixes
- Fixed an issue where both the main "Enter" key and the numeric keypad "Enter" key were not consistently recognized.
- The "Show Stacks" and "Key Bindings" dialogs now correctly follow the application's theme (light/dark mode).
- Fixed a bug that caused the "Show Stacks" dialog to be blank.
- Resolved a `NameError` caused by using `Optional` without importing it.
- Corrected an import error for `EntryMetadata` in the tests.
- Updated a test to assert the correct default version number.
- Fixed a `TypeError` in tests caused by a missing `stack_id` field in the `EntryMetadata` model.
- Resolved a QML issue where `anchors.fill` conflicted with manual positioning, preventing panning and zooming.
- Corrected the `launch_helicon` method to only clear the `selected_raws` set if Helicon Focus is launched successfully.
- Resolved `TypeError` and `Invalid property assignment` errors in QML related to settings dialog initialization and property bindings.
- Fixed QML warnings related to invalid anchor usage in `Main.qml`.
- Fixed missing minimize, maximize, and close buttons by correctly configuring the custom title bar.
- Resolved QML warnings about `mouse` parameter not being declared in `MouseArea` signal handlers.
