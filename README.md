# FastStack

# Version 1.6.4 - June 7, 2026

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
- **Image Editor:** Built-in editor with exposure, contrast, white balance, sharpness, and more (E key)
- **Background Darkening:** Mask-based background darkening tool (K key) with smart edge detection, subject protection, and multiple modes. Paint rough background hints and the tool refines them into natural-looking dark backgrounds.
- **Quick Auto Adjust:** Press `l` for quick auto-levels, `L` for auto white balance + auto-levels together, `A` for auto white balance, `-`/`_` to keep adjusting the highlight/white side in 14-point steps, and `=`/`+` to adjust the shadow/black side in 7-point steps. These update the live in-memory edit session immediately and save once when you navigate away, start a drag, or explicitly save. Auto-levels treats its clip threshold as a budget (a few already-clipped specular pixels won't disable the stretch), brightens underexposed midtones toward a configurable target, and rolls off stretched highlights smoothly instead of hard-clipping them. Auto white balance analyzes only the cropped area, fades its correction when the scene lacks reliable neutrals, and damps magenta/green corrections so foliage isn't mistaken for a color cast. The midtone target, channel clip budget, highlight rolloff, export dithering, and tint correction strength are all adjustable in Settings.
- **Photoshop / Gimp Integration:** Edit current image in Photoshop or Gimp (P key) - always uses RAW files when available.
- **Clipboard Support:** Copy image path to clipboard (Ctrl+C)
- **Image Filtering:** Filter images by filename
- **Drag & Drop:** Drag images to external applications. Press { and } to batch files to drag & drop multiple images.
- **Theme Support:** Toggle between light and dark themes
- **Delete & Undo:** Move images to recycle bin (Delete/Backspace) with undo support (Ctrl+Z)
- **Has Memory:** Starts where you left off, tells you which images have been edited, stacked and uploaded.
- **RAW Pairing:** Automatically maps JPGs to their corresponding RAW files (`.CR3`, `.ARW`, `.NEF`, etc.).
- **Configurable:** Adjust cache sizes, prefetch behavior, and Helicon Focus / Photoshop paths via a settings dialog and a persistent `.ini` file.
- **Accurate Colors:** Uses monitor ICC profile to display colors correctly.
- **RGB Histogram:** Pressing H brings up a RGB histogram which is designed to show even a little bit of highlight clipping and updates as you zoom in.
- **Full Screen Mode:** Pressing F11 enters full screen mode - Esc/F11 exits.

## Installation

### macOS (Recommended)

FastStack performs best on Python 3.12 due to PySide6 compatibility.

1.  **Install Python 3.12 (via Homebrew):**

    ```bash
    brew install python@3.12
    ```

2.  **Create and Activate a Virtual Environment:**

    ```bash
    python3.12 -m venv venv
    source venv/bin/activate
    ```

3.  **Install FastStack:**

    ```bash
    # From source directory
    python -m pip install -U pip
    python -m pip install .
    ```

    _Note: If you encounter issues with `opencv-python` or `PySide6` on newer Python versions (3.13+), please stick to Python 3.12._

4.  **Run:**
    ```bash
    faststack
    faststack --loupe /path/to/photos  # start in loupe view, skip initial thumbnails
    ```

### Windows / Linux

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
  the configured default directory or prompts for one.
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

### Troubleshooting on Windows

If startup logs mention:

```text
TurboJPEG initialization failed (N location(s) tried). Falling back to Pillow for JPEG decoding.
```

that means the Python package is installed but FastStack could not initialize TurboJPEG from any discovered location and is using Pillow instead.

Fastest fixes:

1. Install `libjpeg-turbo` for Windows x64 so that this file exists:
   `C:\libjpeg-turbo64\bin\turbojpeg.dll`
2. Or point FastStack to the dll explicitly:

```cmd
set FASTSTACK_TURBOJPEG_LIB=C:\path\to\turbojpeg.dll
faststack "C:\path\to\photos"
```

If you do nothing, FastStack will still run, but JPEG decoding and thumbnail generation will use Pillow instead of `libjpeg-turbo`, which is slower.

## Keyboard Shortcuts

- `Right Arrow`: Next Image
- `Left Arrow`: Previous Image
- `K`: Mask-based background darkening (smart edge detection, subject protection, multiple modes)
- `G`: Jump to Image Number
- `I`: Show EXIF Data
- `F11`: Toggle Fullscreen (Loupe View)
- `Space` (hold in loupe view): Show the original with the current crop, hiding other edits until released
- `S`: Toggle current image in/out of stack
- `X`: Remove current image from batch/stack
- `B`: Toggle current image in/out of batch
- `D`: Toggle todo flag - shows up red on the sparkline so you can see if you have flagged images to work on later
- `[`: Begin new stack group
- `]`: End current stack group
- `C`: Clear all stacks
- `{`: Begin new drag & drop batch
- `}`: End current drag & drop batch
- `|` (Shift+`\`): Clear drag & drop batch
- `U`: Toggle uploaded flag
- `Ctrl+E`: Toggle edited flag
- `Ctrl+S`: Toggle stacked flag
- `Enter`: Launch Helicon Focus with selected RAWs
- `P`: Edit in Photoshop or Gimp (uses RAW file when available)
- `O` (or Right-Click): Toggle crop mode (Enter to apply crop to the live session, Esc to cancel)
- `Delete` / `Backspace`: Move image to recycle bin
- `Ctrl+Z`: Undo last saved action (delete or saved edit)
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

- **Light** — Exposure, Contrast, Whites, Shadows, Blacks (the full editor
  also adds Brightness and Highlights).
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

Press `O` (or right-click the image) to enter crop mode. Drag the crop
rectangle, optionally press `1`/`2`/`3`/`4` to lock a 1:1, 4:3, 3:2, or 16:9
aspect ratio, then press `Enter` to apply the crop to the live session or `Esc`
to cancel. You must apply or cancel a crop before you can save.

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

- `Ctrl+Z` undoes the last saved edit, restoring the image from its `-backup`.
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
