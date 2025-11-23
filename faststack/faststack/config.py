"""Manages application configuration via an INI file."""

import configparser
import logging
from pathlib import Path

from faststack.logging_setup import get_app_data_dir

log = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "core": {
        "cache_size_gb": "1.5",
        "prefetch_radius": "4",
        "theme": "dark",
        "default_directory": "",
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
    "awb": {        "mode": "lab", # "lab" or "rgb"
        "strength": "0.7",
        "warm_bias": "6",
        "luma_lower_bound": "30",
        "luma_upper_bound": "220",
        "rgb_lower_bound": "5",
        "rgb_upper_bound": "250",
    },
}

class AppConfig:
    def __init__(self):
        self.config_path = get_app_data_dir() / "faststack.ini"
        self.config = configparser.ConfigParser()
        self.load()

    def load(self):
        """Loads the config, creating it with defaults if it doesn't exist."""
        if not self.config_path.exists():
            log.info(f"Creating default config at {self.config_path}")
            self.config.read_dict(DEFAULT_CONFIG)
            self.save()
        else:
            log.info(f"Loading config from {self.config_path}")
            self.config.read(self.config_path)
            # Ensure all sections and keys exist
            for section, keys in DEFAULT_CONFIG.items():
                if not self.config.has_section(section):
                    self.config.add_section(section)
                for key, value in keys.items():
                    if not self.config.has_option(section, key):
                        self.config.set(section, key, value)
            self.save() # Save to add any missing keys


    def save(self):
        """Saves the current configuration to the INI file."""
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with self.config_path.open("w") as f:
                self.config.write(f)
            log.info(f"Saved config to {self.config_path}")
        except IOError as e:
            log.error(f"Failed to save config to {self.config_path}: {e}")

    def get(self, section, key, fallback=None):
        return self.config.get(section, key, fallback=fallback)

    def getint(self, section, key, fallback=None):
        return self.config.getint(section, key, fallback=fallback)

    def getfloat(self, section, key, fallback=None):
        return self.config.getfloat(section, key, fallback=fallback)

    def getboolean(self, section, key, fallback=None):
        return self.config.getboolean(section, key, fallback=fallback)

    def set(self, section, key, value):
        if not self.config.has_section(section):
            self.config.add_section(section)
        self.config.set(section, key, str(value))

# Global config instance
config = AppConfig()
