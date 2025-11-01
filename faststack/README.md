# FastStack

# Version 0.1 - October 31, 2025
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
- `S`: Add/Remove current RAW to/from selection set
- `[`: Begin new stack group
- `]`: End current stack group
- `Space`: Toggle Flag
- `X`: Toggle Reject
- `Enter`: Launch Helicon Focus with selected RAWs
