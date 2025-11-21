# FastStack

# Version 0.8 - November 20, 2025
# By Alan Rockefeller

Ultra-fast, caching JPG viewer designed for culling and selecting RAW files for focus stacking.

This tool is optimized for speed, using `libjpeg-turbo` for decoding, aggressive prefetching, and byte-aware LRU caches to provide a fluid experience when reviewing thousands of images.

## Features

- **Instant Navigation:** Sub-10ms next/previous image switching on cache hits.
- **High-Performance Decoding:** Uses `PyTurboJPEG` for fast JPEG decoding, with a fallback to `Pillow`.
- **Zoom & Pan:** Smooth, mipmapped zooming and panning.
- **RAW Pairing:** Automatically maps JPGs to their corresponding RAW files (`.CR3`, `.ARW`, `.NEF`, etc.).
- **Stack Selection:** Group images into stacks (`[`, `]`) and select them for processing (`S`).
- **Helicon Focus Integration:** Launch Helicon Focus with your selected RAW files with a single keypress (`Enter`).
- **Sidecar Metadata:** Saves flags, rejections, and stack groupings to a non-destructive `faststack.json` file.
- **Configurable:** Adjust cache sizes, prefetch behavior, and Helicon Focus path via a settings dialog and a persistent `.ini` file.
- **Photoshop Integration:** Edit current image in Photoshop (E key) - automatically uses RAW files when available
- **Clipboard Support:** Copy image path to clipboard (Ctrl+C)
- **Image Filtering:** Filter images by filename
- **Drag & Drop:** Drag images to external applications
- **Theme Support:** Toggle between light and dark themes
- **Delete & Undo:** Move images to recycle bin (Delete/Backspace) with undo support (Ctrl+Z)

## Installation & Usage

1.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

2.  **Run the App:**
    ```bash
    python -m faststack.app "C:\path\to\your\images"
    ```

## Keyboard Shortcuts

- `J` / `Right Arrow`: Next Image
- `K` / `Left Arrow`: Previous Image
- `G`: Toggle Grid View
- `S`: Toggle selection of current image for stacking
- `[`: Begin new stack group
- `]`: End current stack group
- `Space`: Toggle Flag
- `X`: Toggle Reject
- `Enter`: Launch Helicon Focus with selected RAWs
- `E`: Edit in Photoshop (uses RAW file if available)
- `Delete` / `Backspace`: Move image to recycle bin
- `Ctrl+Z`: Undo last delete
- `Ctrl+C`: Copy image path to clipboard
- `Ctrl+0`: Reset zoom and pan
- `C`: Clear all stacks
