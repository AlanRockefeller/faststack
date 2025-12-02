# Repository Guidelines

## Project Structure & Module Organization
`app.py` is the PySide6 entrypoint that coordinates image indexing, caching, and the QML screens supplied in `qml/`. Supporting modules are grouped by role: `imaging/` manages decode workflow, metadata, editing, and ICC caches; `io/` hosts directory watchers, Helicon Focus integration, executable checks, and RAW indexing; `ui/` contains providers and keystroke maps shared with QML. Configuration defaults live in `config.py`, persisted session data in `faststack.json`, and targeted unit tests plus decode benchmarks in `tests/`.

## Build, Test, and Development Commands
Run `python -m faststack.app <image_dir>` from the repo root to open the client; add `--debug` or `--debugcache` when you need verbose timings or cache telemetry. Execute `pytest tests/` before every PR, or focus on one area (for example `pytest tests/test_metadata.py -k gps`). Benchmarks in `tests/benchmark_decode*.py` can be invoked directly with `python` to gauge decoder changes without launching the UI.

## Coding Style & Naming Conventions
Use four-space indentation, trailing commas for multi-line structures, and descriptive snake_case identifiers; CamelCase is reserved for classes and Qt signal names to match existing QML bindings. Type hints are expected (see `models.py` and `AppController`), and new modules should expose a top-level `log = logging.getLogger(__name__)`. Format with Black and lint with Ruff before opening a pull request.

## Testing Guidelines
Cover every new branch in `imaging/` or `io/` with focused `pytest` cases located beside the existing `test_*.py` files. Mock filesystem and Qt dependencies so tests stay deterministic, and mark anything that performs long disk scans as `pytest.mark.slow`. When adding heuristics that affect performance, pair the change with an updated benchmark script or a note explaining how to validate throughput.

## Commit & Pull Request Guidelines
Commit subjects follow the observed pattern `Release vX.Y - short result`; use the same `<feature> - <impact>` style even for smaller changes, and keep bodies succinct bullet lists of rationale or testing. Pull requests need: a one- or two-sentence summary, reproduction or validation steps (including `pytest` output), and screenshots/GIFs whenever UI behavior changes. Link issue IDs or TODO references directly so reviewers can trace intent, and never request review until `pytest tests/` passes locally.

## Security & Configuration Tips
Avoid committing personal state from `faststack.json`, local benchmarking output, or debug captures. Document any new INI key in `config.py`, prefer relative paths, and treat drag-and-drop helpers like `make_hdrop` as security-sensitive—validate all user-supplied paths before invoking external tools such as Helicon Focus.
