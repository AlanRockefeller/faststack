"""Secrets in the OS keyring (Windows Credential Manager, macOS Keychain,
Secret Service on Linux), with a 0600 file fallback when no backend exists."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from faststack.logging_setup import get_app_data_dir

log = logging.getLogger(__name__)

SERVICE = "FastStack"
INAT_TOKEN = "inaturalist_api_token"
MYCOMAP_KEY = "mycomap_vision_api_key"

_FALLBACK_FILE = "secrets.json"


class CredentialStore:
    def __init__(self, fallback_dir: Optional[Path] = None):
        self._fallback_dir = fallback_dir
        self._keyring = None
        self._keyring_checked = False
        # Values read or written this session. The dialog asks for the same
        # two secrets on every card action and property read; without this
        # each one is a round trip to the OS keyring on the GUI thread.
        self._cache: dict[str, str] = {}

    # --- public -----------------------------------------------------------

    @property
    def backend_name(self) -> str:
        kr = self._get_keyring()
        if kr is None:
            return "file"
        try:
            return type(kr.get_keyring()).__name__
        except Exception:
            return "keyring"

    def get(self, name: str) -> str:
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        value = self._read(name)
        self._cache[name] = value
        return value

    def _read(self, name: str) -> str:
        kr = self._get_keyring()
        if kr is not None:
            try:
                value = kr.get_password(SERVICE, name)
                if value:
                    return value
            except Exception as exc:
                log.warning("keyring read failed for %s: %s", name, exc)
        return self._read_fallback().get(name, "")

    def set(self, name: str, value: str) -> bool:
        """Store (or, for an empty value, forget) a secret. False if it could
        not be written anywhere, so the caller can tell the user."""
        value = (value or "").strip()
        stored = self._store(name, value)
        if stored:
            self._cache[name] = value
        else:
            self._cache.pop(name, None)
        return stored

    def _store(self, name: str, value: str) -> bool:
        kr = self._get_keyring()
        if kr is not None:
            try:
                if value:
                    kr.set_password(SERVICE, name, value)
                else:
                    try:
                        kr.delete_password(SERVICE, name)
                    except Exception:
                        pass
                # Keep the fallback file free of a stale copy.
                try:
                    self._write_fallback_key(name, "")
                except OSError as exc:
                    log.warning("Could not tidy the secrets file: %s", exc)
                return True
            except Exception as exc:
                log.warning("keyring write failed for %s; using file: %s", name, exc)
        try:
            self._write_fallback_key(name, value)
        except OSError as exc:
            log.error("Could not save %s to the secrets file: %s", name, exc)
            return False
        return True

    # --- internals --------------------------------------------------------

    def _get_keyring(self):
        if self._keyring_checked:
            return self._keyring
        self._keyring_checked = True
        try:
            import keyring

            # A probe read tells us whether a real backend answers: keyring's
            # "fail" backend (and an empty chainer) raise here instead of on
            # the first real read in the dialog.
            keyring.get_password(SERVICE, "__probe__")
            self._keyring = keyring
        except Exception as exc:
            log.info("No OS keyring available (%s); secrets go to a 0600 file", exc)
            self._keyring = None
        return self._keyring

    def _fallback_path(self) -> Path:
        base = self._fallback_dir or get_app_data_dir()
        return Path(base) / _FALLBACK_FILE

    def _read_fallback(self) -> dict[str, str]:
        path = self._fallback_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return (
                {str(k): str(v) for k, v in data.items()}
                if isinstance(data, dict)
                else {}
            )
        except (OSError, ValueError):
            return {}

    def _write_fallback_key(self, name: str, value: str) -> None:
        data = self._read_fallback()
        if value:
            data[name] = value
        else:
            data.pop(name, None)
        path = self._fallback_path()
        if not data:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        # Create the file owner-only before any secret is written to it; a
        # leftover .tmp with wider permissions is also tightened first.
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data))
        os.replace(tmp, path)
