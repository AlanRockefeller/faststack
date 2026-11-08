# FastStack

# Version 1.7.0 - November 6, 2026

# By Alan Rockefeller

Ultra-fast, caching JPG viewer designed for culling and selecting RAW or JPG files for focus stacking and website upload.

This tool is optimized for speed, using `libjpeg-turbo` for decoding, aggressive prefetching, and byte-aware LRU caches to provide a fluid experience when reviewing thousands of images.

## Features

- **Crop:** Added the ability to crop and rotate images via the cr(O)p hotkey (or right mouse click). It can be a freeform crop, or constrained to several popular aspect ratios.
- **Zoom & Pan:** Smooth zooming and panning.
- **Stack Selection:** Group images into stacks (`[`, `]`) and select them for processing (`S`).
- **Spark Line**: In grid view, a spark line is visible on each folder, so you can see how far you have gotten in uploading photos in each directory.
- **Helicon Focus Integration:** Launch Helicon Focus with your selected RAW files with a single keypress (`Enter`).
- **Instant Navigation:** Sub-10ms next/previous image switching, high performance decoding via `PyTurboJPEG`.
- **Drag & Drop:** Drag images to external applications. Press { and } to batch files to drag & drop multiple images.
- **Image Editor:** Built-in editor with exposure, contrast, white balance, sharpness, and more (E key)
- **Dodge & Burn:** Paint any compact-editor slider (exposure, contrast, shadows, white balance, vibrance, color mix…) onto part of the photo (Shift+D). Each stroke adds one coat, so overlapping strokes build up; strokes are kept as layers you can re-edit, hide, or delete.
- **Stack Paint:** For in-camera focus stacks (`... stacked.JPG`), paint any of the stack's input JPGs - or the stack output itself - back over the stacked photo, e.g. to restore a clean background or fix stacking halos. Each input is aligned to the stack automatically, the brush circle previews what a click will paint, and global edits and dodge & burn apply to the painted pixels too.
- **Quick Auto Adjust:** Press `l` for quick auto-levels, `L` for auto white balance + auto-levels together, `A` for auto white balance, `-`/`_` to keep adjusting the highlight/white side in 14-point steps, and `=`/`+` to adjust the shadow/black side in 7-point steps. These update the live in-memory edit session immediately and save once when you navigate away, start a drag, or explicitly save. Auto-levels treats its clip threshold as a budget (a few already-clipped specular pixels won't disable the stretch), brightens underexposed midtones toward a configurable target, and rolls off stretched highlights smoothly instead of hard-clipping them. Auto white balance analyzes only the cropped area, fades its correction when the scene lacks reliable neutrals, and damps magenta/green corrections so foliage isn't mistaken for a color cast. The midtone target, channel clip budget, highlight rolloff, export dithering, and tint correction strength are all adjustable in Settings.
- **Photoshop / GIMP Integration:** Edit the current image in Photoshop or GIMP (P key). Photoshop gets the RAW file when one exists; GIMP gets the JPG.
- **Image Filtering:** Filter images by filename
- **Theme Support:** Toggle between light and dark themes
- **Delete & Undo:** Move images to recycle bin (Delete/Backspace) with undo support (Ctrl+Z)
- **Has Memory:** Starts where you left off, tells you which images have been edited, stacked and uploaded.
- **RAW Pairing:** Automatically maps JPGs to their corresponding RAW files (`.CR3`, `.ARW`, `.NEF`, etc.).
- **Configurable:** Adjust cache sizes, prefetch behavior, and Helicon Focus / Photoshop paths via a settings dialog and a persistent `.ini` file.
- **Accurate Colors:** Uses monitor ICC profile to display colors correctly.
- **RGB Histogram:** Pressing H brings up a RGB histogram which is designed to show even a little bit of highlight clipping and updates as you zoom in.
- **Full Screen Mode:** Pressing F11 enters full screen mode - Esc/F11 exits.

## Installation

### Download a build (Recommended)

Most users should grab a prebuilt **Windows** or **macOS** build — no Python
setup required. Download the latest release for your platform from the
[GitHub Releases](https://github.com/AlanRockefeller/faststack/releases) page:

- **Windows:** Download the Windows build, unzip it, and run
  `FastStack.exe`.
- **macOS:** Download the macOS ZIP archive, extract it, and move FastStack to
  your Applications folder. On first launch you need to right-click the app
  and choose **Open** to bypass Gatekeeper.

FastStack checks GitHub Releases for newer versions when update checks are enabled in Settings.

### Install from source

Power users and Linux users can install from source. FastStack supports Python
3.11 through 3.14; release builds are tested and packaged with Python 3.14.10.

#### macOS

1.  **Install Python 3.14 (via Homebrew):**

    ```bash
    brew install python@3.14
    ```

2.  **Create and Activate a Virtual Environment:**

    ```bash
    python3.14 -m venv venv
    source venv/bin/activate
    ```

3.  **Install FastStack:**

    ```bash
    # From source directory
    python -m pip install -U pip
    python -m pip install .
    ```

4.  **Run:**
    ```bash
    faststack
    faststack --loupe /path/to/photos  # start in loupe view, skip initial thumbnails
    ```

#### Windows / Linux

```bash
python -m venv venv
# Activate venv (Windows: venv\Scripts\activate, Linux: source venv/bin/activate)
pip install .
faststack
```

Start directly in single-image loupe view when you want faster startup on large
folders and do not need the thumbnail grid immediately:

```bash
faststack --loupe "C:\path\to\photos"
```

### Updating

FastStack checks GitHub Releases for newer versions when update checks are
enabled in Settings. For source or virtualenv installs, open the release page
from FastStack and update from the checkout:

```bash
git pull
venv/Scripts/python.exe -m pip install -e .
```

On Linux or macOS, use the Python executable from the active virtualenv:

```bash
git pull
python -m pip install -e .
```

Automatic installation is intentionally disabled for source/virtualenv installs
because a running Python app cannot reliably replace its own environment across
Windows, Linux, and macOS.

### Command Line Options

```text
faststack [options] [image_dir]
python -m faststack.app [options] [image_dir]
```

- `image_dir`: Optional directory of images to open. If omitted, FastStack uses
  the default directory set in Settings, else the last folder you had open,
  else prompts for one.
- `--loupe`: Start directly in single-image loupe view and skip the initial
  thumbnail grid refresh for faster startup on large folders.
- `--debug`: Enable verbose debug logging and timing information.
- `--debugcache`: Enable cache telemetry/debug output.
- `--debug-thumbtiming`: Enable thumbnail pipeline timing logs. Implies
  `--debug`.
- `--debug-thumbtrace`: Enable detailed thumbnail pipeline trace logs. Implies
  `--debug`.

### Windows Performance Note

On Windows, `PyTurboJPEG` also needs the native `libjpeg-turbo` library (`turbojpeg.dll`).

- If `turbojpeg.dll` is installed, FastStack uses it automatically for faster JPEG decode and thumbnail generation.
- If it is missing, FastStack still runs, but falls back to Pillow and may feel slower on large folders.

Recommended install location:

- `C:\libjpeg-turbo64\bin\turbojpeg.dll`

FastStack also checks these optional environment variables if you installed it elsewhere:

- `FASTSTACK_TURBOJPEG_LIB`
- `TURBOJPEG_LIB`

Example:

```cmd
set FASTSTACK_TURBOJPEG_LIB=C:\path\to\turbojpeg.dll
faststack "C:\path\to\photos"
```

## Keyboard Shortcuts

- `Right Arrow`: Next Image
- `Left Arrow`: Previous Image
- `Shift+Right Arrow`: Jump forward 10 images in loupe view
- `Shift+Left Arrow`: Jump back 10 images in loupe view
- `Shift+D`: Dodge & burn brush in the compact editor. Sliders set the brush recipe; left-drag paints, right/Alt-drag erases, `[`/`]` brush size, Space+drag or middle-drag pans, Ctrl+Z undoes a stroke, `M` (or hold Ctrl) shows the mask, Esc leaves brush mode. The compact editor's "Stack" button (with its input dropdown) switches to stack paint, which uses the same brush keys; right/Alt-drag restores the stack there
- `G`: Jump to Image Number
- `Alt+U`: Jump to the last uploaded image
- `I`: Show EXIF Data
- `T`: Toggle between the thumbnail grid and single-image view
- `F11`: Toggle Fullscreen (Loupe View)
- `Space` (hold in loupe view): Show the original with the current crop, hiding other edits until released
- `S`: Toggle current image in/out of stack
- `X`: Remove current image from batch/stack
- `B`: Toggle current image in/out of batch
- `D`: Toggle todo flag - shows up red on the sparkline so you can see if you have flagged images to work on later
- `[`: Begin new stack group
- `]`: End current stack group
- `C`: Clear all stacks (Ctrl+Z restores them)
- `{`: Begin new drag & drop batch
- `}`: End current drag & drop batch
- `|` (Shift+`\`): Clear drag & drop batch (Ctrl+Z restores it)
- `U`: Toggle uploaded flag
- `F`: Toggle favorite flag
- `Ctrl+E`: Toggle edited flag
- `Ctrl+S`: Toggle stacked flag
- `Enter`: Launch Helicon Focus with selected RAWs
- `P`: Edit in Photoshop or GIMP. Photoshop receives the paired RAW file when one exists (otherwise the JPG); GIMP always receives the JPG, since it cannot open RAW files directly. Asks first if FastStack edits are unsaved
- `R`: Open the paired RAW file in RawTherapee (uses the RawTherapee path from Settings, or `rawtherapee` on your PATH)
- `O` (or Right-Click): Toggle crop mode (Enter to apply crop to the live session, Esc to cancel)
- `Delete` / `Backspace`: Move image to recycle bin
- `Ctrl+Z`: Undo. If the current image has unsaved live edits (quick auto adjust, crop, editor sliders), the first press discards those; otherwise it undoes the last saved action (delete, saved edit, or clearing stacks/batches)
- `A`: Quick auto white balance (live session; saved on navigation, drag, or Ctrl+S)
- `Ctrl+Shift+B`: Quick auto white balance (alternate)
- `l`: Quick auto levels + vibrance (live session; saved on navigation, drag, or Ctrl+S)
- `L`: Quick auto white balance + auto levels + vibrance (live session; saved on navigation, drag, or Ctrl+S)
- `-`: Darken the current auto-adjust highlights/whites by 14 points in the live session
- `_`: Raise the current auto-adjust whites by 14 points in the live session
- `+`: Raise the current auto-adjust shadows/blacks by 7 points in the live session
- `=`: Deepen the current auto-adjust shadows/background by 7 points in the live session
- `E`: Toggle Image Editor
- `Esc`: Close active dialog, editor, cancel crop, or exit fullscreen
- `H`: Toggle histogram window
- `Ctrl+C`: Copy image path to clipboard
- `Ctrl+0`: Reset zoom and pan to fit window
- `Ctrl+1`: Zoom to 100%
- `Ctrl+2`: Zoom to 200%
- `Ctrl+3`: Zoom to 300%
- `Ctrl+4`: Zoom to 400%

### Thumbnail grid

Press `T` (or use **Thumbnail View** in the Actions menu) to browse the folder
as a grid. Folders appear as tiles with a sparkline showing how far along you
are with uploads, edits, stacks, todos and batches in that folder.

- `Arrow keys`: Move the cursor
- `Enter` or click: Open the image in single-image view, or enter a folder
- `Space`, right-click, or `Ctrl+Click`: Toggle selection; `Shift+Click` selects a range
- `B`: Add the selected images to the batch
- `Delete` / `Backspace`: Move the selected images (or the cursor image) to the recycle bin
- `Esc`: Clear the selection, or return to single-image view
- Right-click a folder with no images to delete it. A completely empty folder is removed as soon as you choose **Delete empty folder**; a folder that still holds a few small leftover files (a `faststack.json`, for example) shows what is inside and asks before deleting. Folders that contain images show no menu

## Image Editor

Press `E` to toggle the image editor. It opens as a small floating panel (the
**compact editor**) docked to the right edge of the main window. Click the
expand button (⤢) in its header to switch to the **full editor**, a larger
dialog with the complete set of adjustments. Both edit the image currently
shown in the loupe.

### What it does

The editor applies a _live, non-destructive_ session on top of the current
image: nothing is written to disk until you save. The panel shows a live
histogram (overlay or per-channel R/G/B) and grouped adjustments:

- **Light** — Exposure, Brightness, Contrast, Highlights, Whites, Shadows,
  Blacks. Lowering Highlights darkens bright areas while keeping the detail
  inside them.
- **Color** — Temp (Blue/Yellow), Tint (Green/Magenta), and Vibrance (the full
  editor adds Saturation, Clarity, Texture, Sharpness, Vignette, and more).
- **Auto** buttons next to each group apply auto-levels or auto white balance.

Drag a slider to adjust it. **Double-click a slider** (or click its numeric
value) to reset that one control to 0. **Reset** clears every adjustment back
to the original.

### Using the keyboard in the compact editor

While the compact editor has focus, the arrow keys are split so you can both
browse and adjust without reaching for the mouse:

- `Left` / `Right` — go to the previous / next image. (Any unsaved edits on the
  current image are committed first — see _Saving_ below.)
- `Shift+Left` / `Shift+Right` — jump backward / forward 10 images.
- `Up` / `Down` — raise / lower the **highlighted** slider. The highlighted row
  is tinted and outlined; Exposure is highlighted by default.
- **Click a slider's label** (or its value) to make it the highlighted slider
  that `Up`/`Down` will affect.
- `S` (or `Ctrl+S`) — save the current edits.
- `E` or `Esc` — close the editor (you'll be prompted if there are unsaved
  edits).
- `O` — toggle crop mode.

Other shortcuts work the same as they do in the main view even while the editor
is focused, including `B` (add to batch), `F` (favorite), `D` (todo), `I`
(EXIF), and `G` (jump to image). In short, the compact editor never traps the
keyboard — only the editor-specific keys above behave differently.

### Cropping

Press `O` (or right-click the image) to enter crop mode. Drag out a crop
rectangle, or drag the edges and corners of the existing one. The **Aspect
Ratio** panel in the top-left corner locks the box to Freeform, 1:1, 4:5,
1.91:1, 16:9, or 9:16, offers a **Grid** switch for alignment guides (the grid
defaults to on in rotate mode, but the switch can show or hide it at any time),
and a **Rotate** mode that straightens the image by dragging the handle below
the crop box. Press `Enter` to apply the crop to the live session or `Esc` to
cancel. You must apply or cancel a crop before you can save.

### Saving

Saving writes the edited result back to the JPG on disk. Before overwriting,
FastStack creates a `-backup` copy of the original file, so the unedited image
is never lost. You can save explicitly with `S`/`Ctrl+S` or the **Save**
button.

Edits are also committed automatically when you **navigate to another image**
or **drag the image out** of FastStack — so pressing `Left`/`Right` in the
editor saves the current image's pending edits before moving on. Closing the
editor with unsaved edits prompts you to discard or keep them.

### Undo and caveats

- `Ctrl+Z` first discards any unsaved live edits on the current image. Press it
  again to undo the most recent saved action: a saved edit (the image is
  restored from its `-backup` copy), a deletion (restored from the recycle
  bin), or a cleared stack or batch. The status bar says so when there is
  nothing left to undo.
- Because the editor operates on the visual JPG, edits stack on top of the
  current file; for the most flexibility do your heavy adjustments before
  exporting elsewhere.
- The histogram, clip indicators, and live preview update as you adjust, which
  makes it easy to watch for blown highlights or crushed blacks (the clip
  counters turn hot when channels clip).

## Status Bar

The bar across the bottom of the window summarizes everything FastStack knows
about the current image. Items only appear when they are relevant, so you will
rarely see all of them at once.

### Left side — image identity

- **Image: N / M**: The position of the current image (N) within the folder, and
  the total number of images (M) after any active filter is applied.
- **Filename**: The file name of the image currently displayed.
- **EXIF brief**: A compact capture summary pulled from the image's EXIF data,
  shown as `ISO 800 | f/2.8 | 1/500s | 14:30:25` (ISO, aperture, shutter speed,
  and capture time). Any value the camera did not record is simply omitted.
- **Distance (`123 m`)**: When both the current and previous images contain GPS
  coordinates, this shows the straight-line distance, in meters, between where
  the two photos were taken. Hover over it for a reminder of what it means.
- **Directory path**: The folder currently being browsed (greyed out and
  shortened in the middle if long). Hover to see the full path.

### Center / right — status flags and badges

These appear only when the corresponding flag or condition is set on the image:

- **Stacked: \<date\>** (green): The image has been marked as stacked, with the date.
- **Uploaded on \<date\>** (green): The image has been flagged as uploaded.
- **Todo since \<date\>** (blue): The image is flagged as a todo for later work.
- **Edited on \<date\>** (green): The image has been flagged as edited.
- **Restacked on \<date\>** (cyan): The image has been flagged as restacked.
- **Favorite** (gold): The image is marked as a favorite.
- **Filter: "..."** (yellow): A search/filter string is active; only matching
  images are shown and counted.
- **Preloading bar**: A progress bar shown while images are being decoded and
  cached in the background.
- **Stack: ...** (orange badge): The stack group this image belongs to.
- **Batch: ...** (green badge): The drag-and-drop batch this image belongs to.
- **Variant badges**: When an image has multiple variants (e.g. JPG and a stacked
  result), clickable badges let you switch which variant is displayed. An italic
  hint describes what saving will do.

### Far right — modes, messages, and grid controls

- **Cache stats** (cyan, monospace): Live cache telemetry, shown only when started
  with `--debugcache`.
- **Saturation slider**: Adjusts display saturation; visible only in saturation
  color mode.
- **Status message**: Transient feedback such as save progress, crop prompts, and
  error notices. Turns green and bold while a save is in progress.
- **Grid controls** (grid view only): Shows the number of selected images and
  provides **Clear Selection**, **← Back**, **Refresh**, and **Single View**
  buttons.
- **Folder monitoring notice** (yellow): Shown when FastStack could not watch
  the folder for changes (for example on some network shares). Use **Refresh**
  in the grid to pick up files added or removed by other programs.

## Settings

Open **File > Settings...** The dialog has two tabs. **Save** applies every
change; **Cancel** (or `Esc`) discards them.

### General

- **Focus Stacker Path**: Helicon Focus executable, or OpenFocus's `main.py`
  (or its project folder). Used by `Enter` and the **Run Stacks** menu items.
- **Image Editor Path**: Photoshop, GIMP or another editor for `P`. Recently
  used editors are offered in the dropdown.
- **RawTherapee Path**: `rawtherapee-cli` is used by **Develop RAW** (the
  editor's experimental 16-bit workflow); `R` opens the GUI sibling.
- **Default Image Directory**: Opened at startup when no folder is given on
  the command line. Leave it empty to reopen the last folder you used.
- **Restack RAW Locations**: Where **Restack Source RAWs** looks for the input
  files of an in-camera stack: the root of your stacked photos plus one or
  more RAW source directories. Stack paint searches the same places.
- **Cache Size (GB)**: RAM budget for decoded images. More cache means fewer
  re-decodes when flipping back and forth. The dialog shows current usage.
- **Prefetch Radius**: How many images on either side of the current one are
  decoded ahead of time (default 20). Raise it for long held-key bursts.
- **Held-key Speed (FPS)** and **Held-key Image Quality**: How fast images flip
  while an arrow key is held, and the resolution used during the burst. The
  image you stop on is always upgraded to full display quality.
- **Editing Preview Resolution**: Size edits are rendered at while you drag a
  slider. *Match Window* is sharpest; a fixed size is faster on very large
  screens.
- **Optimize For**: *Speed* uses the fastest JPEG decode path; *Quality* uses
  the slower, exact one. Changing it re-decodes the images around you.
- **Theme**: Dark or light.
- **Check for Updates**: Lets FastStack tell you when a new release is on
  GitHub. Nothing is downloaded or installed automatically.

### Auto Adjustments

- **Auto Levels**: Clip Threshold %, Blacks Clip %, Strength (with an Auto
  option that backs off when clipping would be excessive), Auto Vibrance,
  Midtone Correction and its target, Channel Clip Budget, Highlight Soft
  Rolloff, and Export Dithering. These drive `l`, `L`, and the **Auto** button
  next to the Light sliders; every control has a tooltip explaining it.
- **Auto White Balance**: Algorithm (lab or rgb), Strength, Tint Correction,
  Warm Bias and Tint Bias, plus advanced luminance and RGB thresholds that
  decide which pixels count as neutral. These drive `A`, `L`, and the **Auto**
  button next to the Color sliders.

### Files FastStack writes

- `faststack.ini` holds the settings: `%APPDATA%\faststack\` on Windows,
  `~/.faststack/` on Linux and macOS. Logs go to a `logs` folder next to it
  when FastStack runs without a console.
- `faststack.json` in each image folder (the *sidecar*) records flags
  (uploaded, edited, favorite, todo...), stacks, batches, sort mode and the
  last viewed image. Several FastStack windows can share one folder safely.
- Deleted images move to an `image recycle bin` folder inside the folder you
  opened. FastStack offers to empty it when you quit.
- Saving an edit keeps the original as `<name>-backup.jpg` next to the image
  so `Ctrl+Z` can restore it.
