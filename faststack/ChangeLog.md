# ChangeLog

## Version 0.4

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
