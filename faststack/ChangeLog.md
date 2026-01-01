# ChangeLog

Todo:   Make it work on Linux / Mac.   Create Windows .exe.   Write better documentation / help.   Add splash screen / icon.   


## [1.5.0] - 2025-12-01

- Fixed rotating images via the crop interface.
- Control-1 zooms to 1:1 magnification (100%).   Control-2 to 200, etc to control-4 (400%).

## [1.4.0] - 2025-12-01

- Changed how image caching works for even faster display.   
- Pressing H brings up a RGB histogram which is designed to show even a little bit of highlight clipping and updates as you zoom in.
- Added batch delete with confirmation dialog.
- Added the --cachedebug command line argument which gives info on the image cache in the status bar.   Doesn't seem to slow down the program at all, just takes up room in the status bar.A
- Added a setting that switches between image display optimized for speed or quality.
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
## [0.8.0] - 2025-11-20

### Added
- Backspace key now deletes images (in addition to Delete key).   Control-Z restores.
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
