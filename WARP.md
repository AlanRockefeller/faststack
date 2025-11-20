# WARP.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Project Overview

FastStack is an ultra-fast JPEG viewer for Windows designed for culling and selecting RAW files for focus stacking. It displays JPEGs as proxies for RAW files, optimized for instant next/prev navigation, smooth zoom/pan, and integration with Helicon Focus for processing focus stacks.

**Key Technologies:**
- Python 3.11+ with PySide6 (Qt6) for UI
- Qt Quick (QML) for the rendering layer
- PyTurboJPEG (libjpeg-turbo) for fast JPEG decoding
- Byte-aware LRU caching for image data
- ThreadPoolExecutor for concurrent prefetching

## Development Commands

### Running the Application

```powershell
# Development run with path argument
python -m faststack.app "C:\path\to\images"

# Or from the inner faststack directory
python -m faststack.app "C:\path\to\images"

# Run from the installed package entry point (if installed)
faststack "C:\path\to\images"
```

### Testing

```powershell
# Run all tests
pytest

# Run tests from the faststack directory
pytest faststack/faststack/tests

# Run specific test file
pytest faststack/faststack/tests/test_cache.py

# Run with verbose output
pytest -v
```

### Building Distribution

```powershell
# Build standalone Windows executable using PyInstaller
pyinstaller faststack/faststack.spec

# Output will be in dist/FastStack/FastStack.exe
```

### Installing Dependencies

```powershell
# Install from requirements.txt (preferred for dev)
pip install -r faststack/requirements.txt

# Or install the package in editable mode
pip install -e faststack/
```

## Architecture

### Core Module Structure

The codebase follows a modular architecture split into distinct concerns:

**faststack/faststack/** (main package):
- `app.py` - Main entry point, `AppController` manages application state and coordinates all subsystems
- `config.py` - Configuration management via INI file (`%APPDATA%\faststack\faststack.ini`)
- `models.py` - Core data models (`ImageFile`, `EntryMetadata`, `DecodedImage`, `Sidecar`)
- `logging_setup.py` - Application logging setup

**faststack/faststack/imaging/** - Image processing and caching:
- `jpeg.py` - JPEG decoding with PyTurboJPEG (fallback to Pillow), supports resized decoding
- `cache.py` - Byte-aware LRU cache (`ByteLRUCache`) that tracks memory usage in bytes
- `prefetch.py` - Background prefetching using `ThreadPoolExecutor` with generation-based cancellation

**faststack/faststack/io/** - I/O operations:
- `indexer.py` - Directory scanning, JPG→RAW pairing by stem and timestamp proximity (±2s)
- `sidecar.py` - Manages `faststack.json` sidecar file with atomic writes
- `watcher.py` - Filesystem watching for directory changes
- `helicon.py` - Launches Helicon Focus with selected RAW files

**faststack/faststack/ui/** - UI components:
- `provider.py` - `ImageProvider` for Qt image handling, `UIState` for QML bindings
- `keystrokes.py` - Keyboard event handling (`Keybinder`)

**faststack/faststack/qml/** - QML UI files:
- `Main.qml` - Main window and image viewer
- `Components.qml` - Reusable QML components
- `SettingsDialog.qml` - Settings dialog
- `FilterDialog.qml` - Filtering interface

### Key Architectural Patterns

#### 1. Two-Tier Caching with Display-Aware Prefetching

The app uses a sophisticated caching strategy:
- **Display generation tracking**: When window size or zoom state changes, `display_generation` increments, invalidating cached images
- **Cache keys**: Format `{index}_{display_generation}` ensures stale images are not reused
- **Prefetch radius**: Configurable (default 4), decodes images at indices `[i-N, i+N]`
- **Generation-based cancellation**: When navigating, `prefetcher.generation` increments, and workers check this before caching to avoid stale work

#### 2. Zero-Copy Image Pipeline

To minimize memory overhead:
- JPEG decoding produces contiguous `numpy` arrays (uint8, h×w×3)
- QImage is created with a direct pointer to the numpy buffer (`QImage(buf, w, h, w*3, Format_RGB888)`)
- The numpy array reference is kept alive for the QImage lifetime to prevent dangling pointers
- This approach is implemented via `DecodedImage.buffer` which stores a `memoryview`

#### 3. RAW-JPG Pairing Logic

`indexer.py` pairs JPEGs with RAWs by:
1. Scanning directory for JPGs and RAWs
2. Matching by stem (e.g., `IMG_0123.JPG` → `IMG_0123.CR3`)
3. Validating timestamp proximity (±2 seconds) to handle burst shooting
4. Supporting multiple RAW formats: `.CR3`, `.CR2`, `.ARW`, `.NEF`, `.ORF`, `.RW2`, `.RAF`, `.DNG`

#### 4. Sidecar Metadata Management

All user edits are non-destructive, stored in `faststack.json`:
- Schema version 2 format
- Tracks: flags, rejections, stack IDs, stacking status/date
- Atomic writes: write to temp file, then replace
- Filesystem watcher paused during writes to avoid recursion

#### 5. Performance Optimizations

Key techniques for sub-10ms cache hits:
- **Memory-mapped file I/O**: `mmap` for faster JPEG loading
- **PyTurboJPEG scaling factors**: Use hardware-accelerated downscaling (1/8, 1/4, 1/2) before Pillow resizing
- **Debounced resize handling**: 150ms debounce for window resize events
- **Optimal thread pool sizing**: `min(cpu_count() * 2, 8)` workers for I/O-bound JPEG decoding
- **Selective cache clearing**: Only clear when display dimensions or zoom state changes
- **BILINEAR resampling**: For large downscales (>4x), use faster BILINEAR instead of LANCZOS

## Development Guidelines

### Adding New Keyboard Shortcuts

Edit `ui/keystrokes.py` (`Keybinder.handle_key_press()`) to add new key bindings. The method maps Qt key events to AppController methods.

### Modifying Cache Behavior

The cache size and prefetch radius are configurable in `%APPDATA%\faststack\faststack.ini`:
```ini
[core]
cache_size_gb = 1.5
prefetch_radius = 4
```

Access via `config.getfloat('core', 'cache_size_gb')` or `config.getint('core', 'prefetch_radius')`.

### Adding Support for New RAW Formats

Add extensions to `RAW_EXTENSIONS` set in `io/indexer.py`:
```python
RAW_EXTENSIONS = {
    ".ORF", ".RW2", ".CR2", ".CR3", ".ARW", ".NEF", ".RAF", ".DNG",
    ".orf", ".rw2", ".cr2", ".cr3", ".arw", ".nef", ".raf", ".dng",
}
```

### Extending Sidecar Metadata

To add new metadata fields:
1. Update `EntryMetadata` dataclass in `models.py`
2. Update `Sidecar.version` in `models.py` if breaking changes
3. Handle migration logic in `SidecarManager.load()` in `io/sidecar.py`

### Working with the UI (QML)

- QML files are in `qml/` directory
- `Main.qml` is the entry point, connected to `AppController` via Qt signals/slots
- UI state is exposed via `UIState` object in `ui/provider.py`
- Use `@Slot` decorator for methods callable from QML
- Emit `dataChanged` signal from AppController to trigger UI updates

### PyInstaller Builds

The `faststack.spec` file handles packaging:
- Collects all PySide6 data files
- Includes turbojpeg binaries from `turbojpeg.lib_path`
- Adds hidden imports for `PySide6.QtQml`
- Produces single-folder distribution in `dist/FastStack/`

To add resources or fix missing imports, edit `faststack.spec`.

## Common Pitfalls

### Image Decode Performance Issues

If decoding is slow:
- Verify PyTurboJPEG is installed correctly (check logs for "PyTurboJPEG is available")
- Check if `turbojpeg.dll` is found (Windows) - should be in PyInstaller build
- Consider increasing prefetch radius for smoother navigation at cost of memory

### Cache Not Invalidating on Window Resize

The app uses debounced resize handling. If cache seems stale:
- Check `display_generation` is incrementing (logged as "Display size changed to...")
- Verify `cache_key = f"{index}_{display_generation}"` format in `prefetch.py`
- Ensure `sync_ui_state()` is called after generation increment

### Sidecar File Corruption

If `faststack.json` becomes corrupted:
- The app will log error and start with empty sidecar
- Consider implementing backup strategy in `SidecarManager.save()`
- Version number mismatch triggers fresh start

### Threading Issues with Prefetcher

When adding features that interact with the prefetcher:
- Always increment `generation` when invalidating work
- Check `self.generation != local_generation` before cache operations
- Use `cancel_all()` before clearing image list or display changes
