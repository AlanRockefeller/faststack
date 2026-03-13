"""Manages application configuration via an INI file."""

import configparser
import logging
import sys
import glob
import os
import re
from pathlib import PureWindowsPath

from faststack.logging_setup import get_app_data_dir

log = logging.getLogger(__name__)


def detect_rawtherapee_path():
    """Attempts to find the RawTherapee executable on Windows."""
    if sys.platform != "win32":
        return None

    # Pattern to match RawTherapee CLI installations in Program Files (both x64 and x86)
    # The CLI version (rawtherapee-cli.exe) is required for batch processing with -t -Y -o -c flags
    # Finds paths like C:\Program Files\RawTherapee\5.9\rawtherapee-cli.exe
    base_patterns = [
        r"C:\Program Files\RawTherapee*\**\rawtherapee-cli.exe",
        r"C:\Program Files (x86)\RawTherapee*\**\rawtherapee-cli.exe",
    ]

    try:
        matches = []
        for pattern in base_patterns:
            matches.extend(glob.glob(pattern, recursive=True))

        if not matches:
            return None

        # Helper to extract version numbers for natural sorting
        # e.g., "5.10" -> [5, 10]
        def version_sort_key(path):
            for part in reversed(PureWindowsPath(path).parts):
                if re.fullmatch(r"\d+(?:\.\d+)*", part):
                    return [int(n) for n in part.split(".")]
            return [0]

        # Sort matches to try and get the latest version (by path name)
        # 5.10 > 5.9
        matches.sort(key=version_sort_key, reverse=True)
        return matches[0]
    except (OSError, RuntimeError) as e:
        log.warning("Error detecting RawTherapee path: %s", e)
        return None


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
            self.save()
        else:
            log.info("Loading config from %s", self.config_path)
            self.config.read(self.config_path)
            # Ensure all sections and keys exist
            for section, keys in DEFAULT_CONFIG.items():
                if not self.config.has_section(section):
                    self.config.add_section(section)
                for key, value in keys.items():
                    if not self.config.has_option(section, key):
                        self.config.set(section, key, value)
            self.save()  # Save to add any missing keys

            # Validate RawTherapee path (re-detect if missing)
            if sys.platform == "win32":
                current_rt_path = self.get("rawtherapee", "exe")
                if not os.path.exists(current_rt_path):
                    log.warning(
                        "Configured RawTherapee path not found: %s. Attempting re-detection...",
                        current_rt_path,
                    )
                    new_path = detect_rawtherapee_path()
                    if new_path and new_path != current_rt_path:
                        log.info("Found new RawTherapee path: %s", new_path)
                        self.set("rawtherapee", "exe", new_path)
                        self.save()

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
