"""Manages application configuration via an INI file."""

import configparser
import glob
import logging
import os
import re
import shutil
import sys
from pathlib import Path, PureWindowsPath

from faststack.logging_setup import get_app_data_dir

log = logging.getLogger(__name__)


_TOOL_LABELS = {
    "helicon": "Helicon Focus",
    "photoshop": "Photoshop",
    "rawtherapee": "RawTherapee",
}


def _dedupe_paths(paths: list[str]) -> list[str]:
    result = []
    seen = set()
    for path in paths:
        if not path:
            continue
        normalized = os.path.normcase(
            os.path.normpath(os.path.expanduser(os.path.expandvars(path)))
        )
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(path)
    return result


def _existing_file(path: str | None) -> str | None:
    if not path:
        return None

    cleaned = path.strip().strip('"')
    if not cleaned:
        return None

    expanded = os.path.expanduser(os.path.expandvars(cleaned))
    if os.path.isfile(expanded):
        return expanded
    return None


def _is_wsl_linux() -> bool:
    if sys.platform != "linux":
        return False

    try:
        version = Path("/proc/version").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return "microsoft" in version.lower()


def _runtime_os() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if _is_wsl_linux():
        return "wsl"
    return "linux"


def _join(root: str, *parts: str) -> str:
    return os.path.join(root, *parts)


def _windows_program_roots() -> list[str]:
    return _dedupe_paths(
        [
            os.environ.get("ProgramW6432", ""),
            os.environ.get("ProgramFiles", ""),
            r"C:\Program Files",
            os.environ.get("ProgramFiles(x86)", ""),
            r"C:\Program Files (x86)",
        ]
    )


def _windows_local_program_roots() -> list[str]:
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if not local_app_data:
        return []
    return _dedupe_paths([_join(local_app_data, "Programs")])


def _wsl_windows_program_roots() -> list[str]:
    return _dedupe_paths(["/mnt/c/Program Files", "/mnt/c/Program Files (x86)"])


def _wsl_local_program_roots() -> list[str]:
    return glob.glob("/mnt/c/Users/*/AppData/Local/Programs")


def _application_dirs() -> list[str]:
    # /Applications is normally a firmlink to /System/Volumes/Data/Applications,
    # so globbing /Applications already reaches apps on the Data volume. The
    # /System/Volumes/Data/... entries are a belt-and-suspenders fallback for the
    # rare case that firmlink is missing. Detection prefers the /Applications/...
    # form via _install_preference when both resolve to the same app.
    return _dedupe_paths(
        [
            "/Applications",
            "/Applications/Utilities",
            str(Path.home() / "Applications"),
            "/System/Volumes/Data/Applications",
            "/System/Volumes/Data/Applications/Utilities",
        ]
    )


def _which_candidates(names: list[str]) -> list[str]:
    matches = []
    for name in names:
        found = shutil.which(name)
        if found:
            matches.append(found)
    return _dedupe_paths(matches)


def _windows_tool_patterns(tool_name: str, *, wsl: bool = False) -> list[str]:
    program_roots = _wsl_windows_program_roots() if wsl else _windows_program_roots()
    local_roots = _wsl_local_program_roots() if wsl else _windows_local_program_roots()

    if tool_name == "photoshop":
        patterns = []
        for root in program_roots:
            patterns.extend(
                [
                    _join(root, "Adobe", "Adobe Photoshop*", "Photoshop.exe"),
                    _join(root, "Adobe", "*Photoshop*", "Photoshop.exe"),
                ]
            )
        return patterns

    if tool_name == "helicon":
        patterns = []
        for root in program_roots:
            patterns.extend(
                [
                    _join(
                        root,
                        "Helicon Software",
                        "Helicon Focus*",
                        "HeliconFocus.exe",
                    ),
                    _join(root, "Helicon Software", "*", "HeliconFocus.exe"),
                ]
            )
        return patterns

    if tool_name == "rawtherapee":
        patterns = []
        for root in [*program_roots, *local_roots]:
            patterns.append(_join(root, "RawTherapee*", "**", "rawtherapee-cli.exe"))
        return patterns

    return []


def _macos_tool_patterns(tool_name: str) -> list[str]:
    patterns = []
    for applications_dir in _application_dirs():
        if tool_name == "photoshop":
            patterns.extend(
                [
                    _join(
                        applications_dir,
                        "Adobe Photoshop*.app",
                        "Contents",
                        "MacOS",
                        "Adobe Photoshop*",
                    ),
                    _join(
                        applications_dir,
                        "Adobe Photoshop*",
                        "Adobe Photoshop*.app",
                        "Contents",
                        "MacOS",
                        "Adobe Photoshop*",
                    ),
                ]
            )
        elif tool_name == "helicon":
            patterns.extend(
                [
                    _join(
                        applications_dir,
                        "Helicon Focus*.app",
                        "Contents",
                        "MacOS",
                        "Helicon*",
                    ),
                    _join(
                        applications_dir,
                        "Helicon Focus*",
                        "Helicon Focus*.app",
                        "Contents",
                        "MacOS",
                        "Helicon*",
                    ),
                    _join(
                        applications_dir,
                        "HeliconFocus*.app",
                        "Contents",
                        "MacOS",
                        "Helicon*",
                    ),
                ]
            )
        elif tool_name == "rawtherapee":
            patterns.extend(
                [
                    _join(
                        applications_dir,
                        "RawTherapee*.app",
                        "Contents",
                        "MacOS",
                        "rawtherapee-cli",
                    ),
                    _join(
                        applications_dir,
                        "RawTherapee*",
                        "RawTherapee*.app",
                        "Contents",
                        "MacOS",
                        "rawtherapee-cli",
                    ),
                ]
            )
    return patterns


def _linux_tool_patterns(tool_name: str) -> list[str]:
    home = str(Path.home())

    if tool_name == "photoshop":
        return [
            _join(
                home,
                ".wine",
                "drive_c",
                "Program Files",
                "Adobe",
                "Adobe Photoshop*",
                "Photoshop.exe",
            ),
            _join(
                home,
                ".wine",
                "drive_c",
                "Program Files (x86)",
                "Adobe",
                "Adobe Photoshop*",
                "Photoshop.exe",
            ),
        ]

    if tool_name == "helicon":
        return [
            "/opt/HeliconFocus*/HeliconFocus",
            "/opt/Helicon Focus*/HeliconFocus",
            "/opt/Helicon*/HeliconFocus",
            "/usr/local/bin/HeliconFocus",
            "/usr/local/bin/heliconfocus",
            "/usr/bin/HeliconFocus",
            "/usr/bin/heliconfocus",
        ]

    if tool_name == "rawtherapee":
        return [
            "/opt/homebrew/bin/rawtherapee-cli",
            "/usr/local/bin/rawtherapee-cli",
            "/usr/bin/rawtherapee-cli",
            "/bin/rawtherapee-cli",
            "/snap/bin/rawtherapee-cli",
        ]

    return []


def _tool_candidates(tool_name: str, os_name: str) -> tuple[list[str], list[str]]:
    if tool_name == "photoshop":
        path_candidates = _which_candidates(["Photoshop.exe", "photoshop"])
    elif tool_name == "helicon":
        path_candidates = _which_candidates(
            ["HeliconFocus.exe", "HeliconFocus", "heliconfocus"]
        )
    elif tool_name == "rawtherapee":
        path_candidates = _which_candidates(["rawtherapee-cli", "rawtherapee-cli.exe"])
    else:
        return [], []

    if os_name == "windows":
        patterns = _windows_tool_patterns(tool_name)
    elif os_name == "macos":
        patterns = _macos_tool_patterns(tool_name)
        if tool_name == "rawtherapee":
            path_candidates.extend(
                [
                    "/opt/homebrew/bin/rawtherapee-cli",
                    "/usr/local/bin/rawtherapee-cli",
                    "/usr/bin/rawtherapee-cli",
                ]
            )
    elif os_name == "wsl":
        patterns = [
            *_windows_tool_patterns(tool_name, wsl=True),
            *_linux_tool_patterns(tool_name),
        ]
    else:
        patterns = _linux_tool_patterns(tool_name)

    return _dedupe_paths(path_candidates), _dedupe_paths(patterns)


def _version_sort_key(path: str) -> list[int]:
    path_parts = (
        PureWindowsPath(path).parts if "\\" in path or ":" in path else Path(path).parts
    )
    for part in reversed(path_parts):
        if re.fullmatch(r"\d+(?:\.\d+)*", part):
            return [int(n) for n in part.split(".")]
        match = re.search(
            r"(?:^|[^\dA-Za-z])(\d+(?:\.\d+)*)(?:$|[^\dA-Za-z])",
            part,
        )
        if match:
            return [int(n) for n in match.group(1).split(".")]
        match = re.search(r"(?:19|20)\d{2}", part)
        if match:
            return [int(match.group(0))]
    return [0]


def _install_preference(path: str) -> int:
    lower_path = path.lower()
    if "program files (x86)" in lower_path:
        return 1
    if "program files" in lower_path or lower_path.startswith("/applications/"):
        return 2
    return 0


def detect_external_tool_path(tool_name: str) -> str | None:
    """Return a likely executable path for a supported external tool."""
    os_name = _runtime_os()
    try:
        path_candidates, patterns = _tool_candidates(tool_name, os_name)
        matches = []

        for candidate in path_candidates:
            existing = _existing_file(candidate)
            if existing:
                matches.append(existing)

        for pattern in patterns:
            expanded_pattern = os.path.expanduser(os.path.expandvars(pattern))
            for match in glob.glob(expanded_pattern, recursive=True):
                existing = _existing_file(match)
                if existing:
                    matches.append(existing)

        matches = _dedupe_paths(matches)
        if not matches:
            return None

        matches.sort(
            key=lambda path: (_version_sort_key(path), _install_preference(path)),
            reverse=True,
        )
        return matches[0]
    except (OSError, RuntimeError) as e:
        log.warning(
            "Error detecting %s path: %s",
            _TOOL_LABELS.get(tool_name, tool_name),
            e,
        )
        return None


def detect_photoshop_path():
    """Attempts to find the Photoshop executable for the current OS."""
    return detect_external_tool_path("photoshop")


def detect_helicon_path():
    """Attempts to find the Helicon Focus executable for the current OS."""
    return detect_external_tool_path("helicon")


def detect_rawtherapee_path():
    """Attempts to find the RawTherapee CLI executable for the current OS."""
    return detect_external_tool_path("rawtherapee")


_TOOL_DETECTORS = {
    "helicon": detect_helicon_path,
    "photoshop": detect_photoshop_path,
    "rawtherapee": detect_rawtherapee_path,
}


# Determine default RawTherapee CLI path based on OS
# The CLI version is required for batch processing with command-line flags
if sys.platform == "win32":
    DEFAULT_RT_PATH = r"C:\Program Files\RawTherapee\5.12\rawtherapee-cli.exe"
elif sys.platform == "darwin":
    DEFAULT_RT_PATH = "/Applications/RawTherapee.app/Contents/MacOS/rawtherapee-cli"
else:
    DEFAULT_RT_PATH = "/usr/bin/rawtherapee-cli"

DEFAULT_CONFIG = {
    "core": {
        "cache_size_gb": "1.5",
        "prefetch_radius": "6",
        "theme": "dark",
        "default_directory": "",
        "optimize_for": "speed",  # "speed" or "quality"
        # Set once external-tool auto-detection has run, so we don't re-scan the
        # filesystem for Helicon/Photoshop/RawTherapee on every launch.
        "external_tools_detected": "False",
        # --- Auto Levels Configuration ---
        #
        # Behavior:
        #   Auto Levels are triggered when the user explicitly clicks "Auto Levels" in the
        #   image editor or uses the "Quick Auto Levels" hotkey.
        #
        # Algorithm:
        #   1. Compute black/white points by clipping `auto_level_threshold` fraction of pixels
        #      (0.0-1.0) at the dark and light ends of the histogram.
        #   2. Construct a levels transform to map these points to 0 and 255.
        #   3. Blend the transformed image with the original using `auto_level_strength`.
        #   4. If `auto_level_strength_auto` is True, `auto_level_strength` acts as a maximum;
        #      the system will automatically reduce the applied strength if the computed
        #      transform would cause excessive clipping or color instability.
        #
        # Practical Tuning:
        #   - auto_level_threshold: A fraction (not percent).
        #     Higher values (e.g. 0.05 = 5%) increase contrast but risk hard clipping.
        #     Lower values (e.g. 0.001 = 0.1%) are gentler and preserve more dynamic range.
        #   - auto_level_strength: 1.0 applies the full mathematical correction. Lower values
        #     blend the result for a subtler effect.
        "auto_level_threshold": "0.1",
        "auto_level_strength": "1.0",
        "auto_level_strength_auto": "False",
        "auto_vibrance_enabled": "True",
        "auto_add_edited_to_batch": "True",
    },
    "helicon": {
        "exe": "C:\\Program Files\\Helicon Software\\Helicon Focus 8\\HeliconFocus.exe",
        "args": "",
    },
    "photoshop": {
        "exe": "C:\\Program Files\\Adobe\\Adobe Photoshop 2026\\Photoshop.exe",
        "args": "",
    },
    "color": {
        "mode": "none",  # Options: "none", "saturation", "icc"
        "saturation_factor": "0.85",  # For 'saturation' mode: 0.0-1.0, lower = less saturated
        "monitor_icc_path": "",  # For 'icc' mode: path to monitor ICC profile
    },
    "awb": {
        "mode": "lab",  # "lab" or "rgb"
        "strength": "0.7",
        "warm_bias": "6",
        "tint_bias": "0",
        "luma_lower_bound": "30",
        "luma_upper_bound": "220",
        "rgb_lower_bound": "5",
        "rgb_upper_bound": "250",
    },
    "rawtherapee": {
        "exe": DEFAULT_RT_PATH,
        "args": "",
    },
    "raw": {
        "source_dir": "C:\\Users\\alanr\\pictures\\olympus.stack.input.photos",
        "mirror_base": "C:\\Users\\alanr\\Pictures\\Lightroom",
    },
    "updates": {
        "check_for_updates": "true",
        "auto_update": "false",
        "last_check_at": "",
        "last_ignored_version": "",
    },
}


class AppConfig:
    """Manages application configuration backed by an INI file."""

    def __init__(self):
        self.config_path = get_app_data_dir() / "faststack.ini"
        self.config = configparser.ConfigParser()
        self.load()

    def load(self):
        """Loads the config, creating it with defaults if it doesn't exist."""
        if not self.config_path.exists():
            log.info("Creating default config at %s", self.config_path)
            self.config.read_dict(DEFAULT_CONFIG)
            config_changed = True
            newly_created = True
        else:
            log.info("Loading config from %s", self.config_path)
            self.config.read(self.config_path)
            config_changed = False
            newly_created = False
            # Ensure all sections and keys exist
            for section, keys in DEFAULT_CONFIG.items():
                if not self.config.has_section(section):
                    self.config.add_section(section)
                    config_changed = True
                for key, value in keys.items():
                    if not self.config.has_option(section, key):
                        self.config.set(section, key, value)
                        config_changed = True

        # Auto-detect external tool paths only once: on first run, or a one-time
        # migration for configs created before detection existed. Doing this on
        # every launch would re-scan the filesystem (slow on WSL/network mounts)
        # and could silently replace a path the user configured.
        detection_needed = newly_created or not self.getboolean(
            "core", "external_tools_detected", fallback=False
        )
        if detection_needed:
            if self._detect_external_tool_paths():
                config_changed = True
            self.set("core", "external_tools_detected", "True")
            config_changed = True

        if config_changed:
            self.save()

    def _detect_external_tool_paths(self) -> bool:
        """Fill in external tool paths that are not configured yet.

        Only a tool whose configured value is empty or still the bundled default
        placeholder is auto-detected. A path the user set is never overwritten,
        even when it is currently missing (e.g. on an unplugged drive).
        """
        changed = False

        for section, detector in _TOOL_DETECTORS.items():
            current_path = self.get(section, "exe")
            if _existing_file(current_path):
                continue  # configured path resolves; leave it untouched

            default_path = DEFAULT_CONFIG.get(section, {}).get("exe", "")
            if current_path and current_path != default_path:
                # User configured a custom path that is currently missing —
                # respect it rather than silently replacing it.
                continue

            detected_path = detector()
            if detected_path and detected_path != current_path:
                log.info("Detected %s path: %s", _TOOL_LABELS[section], detected_path)
                self.set(section, "exe", detected_path)
                changed = True

        return changed

    def save(self):
        """Saves the current configuration to the INI file."""
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with self.config_path.open("w") as f:
                self.config.write(f)
            log.info("Saved config to %s", self.config_path)
        except IOError as e:
            log.error("Failed to save config to %s: %s", self.config_path, e)

    def get(self, section, key, fallback=None):
        """Return a config value as a string."""
        return self.config.get(section, key, fallback=fallback)

    def getint(self, section, key, fallback=None):
        """Return a config value as an integer."""
        return self.config.getint(section, key, fallback=fallback)

    def getfloat(self, section, key, fallback=None):
        """Return a config value as a float."""
        return self.config.getfloat(section, key, fallback=fallback)

    def getboolean(self, section, key, fallback=None):
        """Return a config value as a boolean."""
        return self.config.getboolean(section, key, fallback=fallback)

    def set(self, section, key, value):
        """Set a config value, creating the section if needed."""
        if not self.config.has_section(section):
            self.config.add_section(section)
        self.config.set(section, key, str(value))


# Global config instance
config = AppConfig()
