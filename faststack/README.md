# FastStack

# Version 1.2 - November 22, 2025
# By Alan Rockefeller

Ultra-fast, caching JPG viewer designed for culling and selecting RAW or JPG files for focus stacking and website upload.

This tool is optimized for speed, using `libjpeg-turbo` for decoding, aggressive prefetching, and byte-aware LRU caches to provide a fluid experience when reviewing thousands of images.

## Features

- **Instant Navigation:** Sub-10ms next/previous image switching, high-peformance decoding via `PyTurboJPEG`.
- **Zoom & Pan:** Smooth zooming and panning.
- **Stack Selection:** Group images into stacks (`[`, `]`) and select them for processing (`S`).
- **Helicon Focus Integration:** Launch Helicon Focus with your selected RAW files with a single keypress (`Enter`).
- **Image Editor:** Built-in editor with exposure, contrast, white balance, sharpness, and more (E key)
- **Quick Auto White Balance:** Press A to apply auto white balance and save automatically with undo support (Ctrl+Z).   For better white balance load the raw into Photoshop with the P key.
- **Photoshop Integration:** Edit current image in Photoshop (P key) - always uses RAW files when available, even for backup files
- **Clipboard Support:** Copy image path to clipboard (Ctrl+C)
- **Image Filtering:** Filter images by filename
- **Drag & Drop:** Drag images to external applications.   Press { and } to batch files to drag & drop multiple images.
- **Theme Support:** Toggle between light and dark themes
- **Delete & Undo:** Move images to recycle bin (Delete/Backspace) with undo support (Ctrl+Z)
- **Has Memory:** Starts where you left off, tells you which images have been edited, stacked and uploaded
- **RAW Pairing:** Automatically maps JPGs to their corresponding RAW files (`.CR3`, `.ARW`, `.NEF`, etc.).
- **Configurable:** Adjust cache sizes, prefetch behavior, and Helicon Focus / Photoshop paths via a settings dialog and a persistent `.ini` file.
- **Accurate Colors:** Uses monitor ICC profile to display colors correctly.

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
- `G`: Go to image #
- `S`: Toggle selection of current image for stacking 
- `B`: Toggle selection of current image for batch drag & drop
- `[`: Begin new stack group
- `]`: End current stack group
- `{`: Begin new drag & drop batch
- `}`: End current drag & drop batch
- '\': Clear drag & drop batch
- 'U': Toggle uploaded flag
- 'Ctrl+E': Toggle edited flag
- 'Ctrl+S': Toggle stacked flag
- `Enter`: Launch Helicon Focus with selected RAWs
- `P`: Edit in Photoshop (uses RAW file if available)
- `Delete` / `Backspace`: Move image to recycle bin
- `Ctrl+Z`: Undo last action (delete or auto white balance)
- `A`: Quick auto white balance (saves automatically)
- `E`: Toggle Image Editor
- `Ctrl+C`: Copy image path to clipboard
- `Ctrl+0`: Reset zoom and pan
- `C`: Clear all stacks
