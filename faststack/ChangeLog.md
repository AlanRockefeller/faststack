# ChangeLog

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
- The "About" menu item has been changed to "Key Bindings" and now displays the application's key bindings.
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
