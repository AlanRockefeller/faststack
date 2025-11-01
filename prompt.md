# FastStack: Ultra-fast JPG Viewer for RAW Stacking Selection (Windows)

## Goal
Generate a complete, production-quality Python project that displays **JPEGs only** (as proxies for paired RAW files), optimized for **instant next/prev**, smooth **zoom/pan**, aggressive **prefetch**, and **byte-capped LRU caches**. The app writes sidecar metadata and launches **Helicon Focus** with selected **RAW** files, but never demosaics RAWs itself.

We prioritize **speed**, **low-latency UI**, and **Windows-native packaging**. No WSL.

---

## Tech Stack (must use)
- **Language:** Python 3.11+ (compatible with 3.13)
- **UI/Rendering:** **PySide6 (Qt6)** with **Qt Quick (QML)**; leverage D3D11 via Qt for fast textures
- **JPEG decode (fast path):** **PyTurboJPEG** (libjpeg-turbo); **Pillow** only as optional fallback
- **Concurrency:** `concurrent.futures.ThreadPoolExecutor`
- **Caching:** `cachetools` LRU by **bytes**, not count
- **FS Watch:** `watchdog`
- **CLI (dev):** `typer`
- **Packaging:** **PyInstaller** (standalone Windows build)
- **Testing (basic):** `pytest` for a couple of smoke tests (not heavy)

---

## Performance Targets (hard requirements)
- **Cache hit next/prev switch:** < **10 ms** (just swap pointer/texture)
- **Cold view (first decode + upload) for ~24MP JPG:** < **60 ms** amortized by prefetch (decode off the UI thread)
- **Prefetch:** When viewing index *i*, maintain decoded cache for *(i−N … i+N)*, default **N = 4**
- **No jank:** UI must remain responsive during folder scans and decoding
- **Memory ceiling:** Single configurable **cache_bytes** budget (default **1.5 GB**), enforced for:
  - CPU RGB buffers (numpy arrays)
  - GPU textures (Qt textures)
- **Zero-copy bridge:** Turn decoded `uint8 h×w×3` numpy into `QImage` **without copying**. Keep the numpy buffer alive for the lifetime of the QImage/texture to avoid dangling memory.

---

## Features (acceptance criteria)
1) **Folder open** (File→Open or argument) scans for JPGs (case-insensitive). Build a stable list and a **JPG→RAW** map by **stem** (`IMG_0123.JPG → IMG_0123.ORF/.RW2/.CR3/.ARW/.NEF/.RAF/.DNG`), prefer RAW in the **same folder**, timestamp ±2s as tiebreaker.
2) **One-up (loupe) viewer** with:
   - Wheel zoom centered at cursor; left-drag to pan
   - Mipmapped textures for smooth zoom
   - Overlay with filename, flag, reject, stack_id (toggleable)
3) **Grid view** (toggle with `G`) with **lazy thumbnails** (scaled decode ~256px long edge using turbojpeg scaling factors; separate thumbnail cache with its own byte cap).
4) **Keyboard UX** (must be instantaneous on cache hits):
   - `J/K`: next/prev
   - `S`: add current **RAW** to active stack set (selection)
   - `[` / `]`: begin/end new stack group (monotonic `stack_id`)
   - `Space`: toggle **flag**
   - `X`: toggle **reject**
   - `Enter`: **Launch Helicon Focus** with ordered list of selected **RAW** paths (one per line in a temp .txt)
5) **Sidecar** (single JSON at folder root `faststack.json`):
   - Schema:
     ```json
     {
       "version": 1,
       "last_index": 0,
       "entries": {
         "IMG_01234": { "flag": true, "reject": false, "stack_id": 3 }
       }
     }
     ```
   - **Atomic writes** (write temp then replace)
6) **Prefetch & cancellation**:
   - ThreadPool workers decode *(i−N … i+N)*; **cancel stale** jobs using a **generation counter** (each navigation increments; workers drop results if generation mismatches).
7) **LRU cache (by bytes)**:
   - One cache for full-res previews (RGB888 numpy + QImage/texture handle)
   - One cache for thumbnails
   - On eviction, **free both CPU and GPU resources**
8) **Settings** in `%APPDATA%\faststack\faststack.ini`:
   - `[core] cache_bytes, prefetch_radius`
   - `[helicon] exe, args`
   - Provide a small Settings dialog in-app to edit these and save
9) **Error handling**:
   - If PyTurboJPEG is unavailable or decode fails, **fallback to Pillow**
   - If Helicon path invalid, show actionable dialog and let user browse to exe
   - Invalid images should not crash the app; show placeholder + log entry

---

## Project Layout (generate exactly)
```
faststack/
  pyproject.toml
  requirements.txt
  README.md
  LICENSE
  faststack/
    __init__.py
    app.py
    config.py
    types.py
    logging_setup.py
    qml/
      Main.qml
      Components.qml
    ui/
      provider.py
      keystrokes.py
    imaging/
      jpeg.py
      cache.py
      prefetch.py
    io/
      indexer.py
      sidecar.py
      watcher.py
      helicon.py
    tests/
      test_pairing.py
      test_sidecar.py
      test_cache.py
  faststack.spec
```

---

## Implementation Details (must follow)

### JPEG decoding (`imaging/jpeg.py`)
- Primary path: **PyTurboJPEG**
  - `decode_rgb(jpeg_bytes) -> np.ndarray[h,w,3]`
  - Thumbnails: use `scaling_factor` to nearest downscale to ~256 px long edge
- Fallback: **Pillow**
- Return numpy arrays (C-contiguous, uint8)

### Zero-copy QImage
```python
qimg = QImage(buf, w, h, w*3, QImage.Format.Format_RGB888)
```
Keep numpy buffer ref alive for lifetime.

### Prefetch (`imaging/prefetch.py`)
- ThreadPoolExecutor(max_workers=min(4, os.cpu_count()))
- Generation counter cancels stale jobs.

### LRU Cache (`imaging/cache.py`)
- Byte-capped using cachetools.
- Evict frees numpy + texture.

### Indexing (`io/indexer.py`)
- os.scandir walk
- JPG→RAW mapping by stem & timestamp proximity.

### Helicon (`io/helicon.py`)
- Build temp .txt list of RAW paths, then subprocess.Popen(HeliconFocus.exe).

### Config & Logging
- %APPDATA%\faststack\faststack.ini
- Rotating logs in %APPDATA%\faststack\logs\app.log

---

## Dependencies
```
PySide6==6.*
PyTurboJPEG==1.*
numpy==2.*
cachetools==5.*
watchdog==4.*
typer==0.12.*
pyinstaller==6.*
Pillow==10.*
pytest==8.*
```

---

## PyInstaller Spec
- Single-folder build
- Copy turbojpeg.dll
- `--collect-all PySide6`
- `--hidden-import PySide6.QtQml`
- Output: `dist/FastStack/FastStack.exe`
