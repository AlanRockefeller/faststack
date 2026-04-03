# FastStack

# Version 1.6.2 - March 28, 2026
# By Alan Rockefeller

Ultra-fast, caching JPG viewer designed for culling and selecting RAW or JPG files for focus stacking and website upload.

This tool is optimized for speed, using `libjpeg-turbo` for decoding, aggressive prefetching, and byte-aware LRU caches to provide a fluid experience when reviewing thousands of images.

## Features

- **Crop:** Added the ability to crop and rotate images via the cr(O)p hotkey (or right mouse click).   It can be a freeform crop, or constrained to several popular aspect ratios.
- **Zoom & Pan:** Smooth zooming and panning.
- **Stack Selection:** Group images into stacks (`[`, `]`) and select them for processing (`S`).
- **Spark Line**: In grid view, a spark line is visible on each folder, so you can see how far you have gotten in uploading photos in each directory.
- **Helicon Focus Integration:** Launch Helicon Focus with your selected RAW files with a single keypress (`Enter`).
- **Instant Navigation:** Sub-10ms next/previous image switching, high performance decoding via `PyTurboJPEG`.
- **Image Editor:** Built-in editor with exposure, contrast, white balance, sharpness, and more (E key)
- **Background Darkening:** Mask-based background darkening tool (K key) with smart edge detection, subject protection, and multiple modes. Paint rough background hints and the tool refines them into natural-looking dark backgrounds.
- **Quick Auto White Balance:** Press A to apply auto white balance and save automatically with undo support (Ctrl+Z).   For better white balance, load the raw into Photoshop with the P key.
- **Photoshop / Gimp Integration:** Edit current image in Photoshop or Gimp (P key) - always uses RAW files when available.
- **Clipboard Support:** Copy image path to clipboard (Ctrl+C)
- **Image Filtering:** Filter images by filename
- **Drag & Drop:** Drag images to external applications.   Press { and } to batch files to drag & drop multiple images.
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
    *Note: If you encounter issues with `opencv-python` or `PySide6` on newer Python versions (3.13+), please stick to Python 3.12.*

4.  **Run:**
    ```bash
    faststack
    ```

### Windows / Linux
```bash
python -m venv venv
# Activate venv (Windows: venv\Scripts\activate, Linux: source venv/bin/activate)
pip install .
faststack
```

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
- `S`: Toggle current image in/out of stack
- `X`: Remove current image from batch/stack
- `B`: Toggle current image in/out of batch
- `D`: Toggle todo flag - shows up red on the sparkline so you can see if you have flagged images to work on later
- `[`: Begin new stack group 
- `]`: End current stack group
- `C`: Clear all stacks
- `{`: Begin new drag & drop batch
- `}`: End current drag & drop batch
- `\`: Clear drag & drop batch
- `U`: Toggle uploaded flag
- `Ctrl+E`: Toggle edited flag
- `Ctrl+S`: Toggle stacked flag
- `Enter`: Launch Helicon Focus with selected RAWs
- `P`: Edit in Photoshop or Gimp (uses RAW file when available)
- `O` (or Right-Click): Toggle crop mode (Enter to execute, Esc to cancel)
- `Delete` / `Backspace`: Move image to recycle bin
- `Ctrl+Z`: Undo last action (delete, auto white balance, or crop)
- `A`: Quick auto white balance (saves automatically)
- `Ctrl+Shift+B`: Quick auto white balance (alternate)
- `L`: Quick auto levels (saves automatically)
- `E`: Toggle Image Editor
- `Esc`: Close active dialog, editor, cancel crop, or exit fullscreen
- `H`: Toggle histogram window
- `Ctrl+C`: Copy image path to clipboard
- `Ctrl+0`: Reset zoom and pan to fit window
- `Ctrl+1`: Zoom to 100%
- `Ctrl+2`: Zoom to 200%
- `Ctrl+3`: Zoom to 300%
- `Ctrl+4`: Zoom to 400%
