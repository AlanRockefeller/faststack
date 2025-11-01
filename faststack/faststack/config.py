"""Manages application configuration via an INI file."""

import configparser
import logging
from pathlib import Path

from faststack.logging_setup import get_app_data_dir

log = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "core": {
        "cache_bytes": str(int(1.5 * 1024**3)), # 1.5 GB
        "prefetch_radius": "4",
    },
    "helicon": {
        "exe": "C:\\Program Files\\Helicon Software\\Helicon Focus 8\\HeliconFocus.exe",
        "args": "",
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
