"""
modio_api.py  (Baldur's Gate 3)

Minimal read-only client for the public mod.io REST API.  Used by BG3
update-checking, which only needs the per-mod file list and profile URL.
Requests route through ``resolve_ca_bundle()`` with a small session cache.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import requests

from Utils.app_log import app_log
from Utils.ca_bundle import resolve_ca_bundle

# mod.io's BG3 game is addressable by name-id ("@baldursgate3") so we never
# need to resolve the numeric game id (6715) ourselves.
_API_ROOT = "https://api.mod.io/v1"
_GAME = "@baldursgate3"

# Cache mod_id -> (timestamp, list[ModioFile]) for the session.
_FILES_CACHE: dict[int, tuple[float, "list[ModioFile]"]] = {}
_CACHE_TTL = 600.0  # seconds


@dataclass
class ModioFile:
    """One released modfile from the mod.io files endpoint."""

    file_id: int = 0
    version: str = ""
    date_added: int = 0
    filesize: int = 0
    filesize_uncompressed: int = 0
    filename: str = ""
    md5: str = ""
    changelog: str = ""

    @classmethod
    def from_json(cls, d: dict) -> "ModioFile":
        return cls(
            file_id=int(d.get("id") or 0),
            version=str(d.get("version") or ""),
            date_added=int(d.get("date_added") or 0),
            filesize=int(d.get("filesize") or 0),
            filesize_uncompressed=int(d.get("filesize_uncompressed") or 0),
            filename=str(d.get("filename") or ""),
            md5=str((d.get("filehash") or {}).get("md5") or "").lower(),
            changelog=str(d.get("changelog") or ""),
        )


class ModioAPIError(Exception):
    """Raised on a failed mod.io API request (network or HTTP error)."""


class ModioAPI:
    """Read-only mod.io client.  Requires a public read-only API key."""

    def __init__(self, api_key: str, timeout: float = 30.0):
        if not api_key:
            raise ValueError("mod.io API key is required")
        self._api_key = api_key.strip()
        self._timeout = timeout
        self._session = requests.Session()
        self._session.verify = resolve_ca_bundle() or True
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": "AmethystModManager",
        })

    def get_mod_files(self, mod_id: int, *, use_cache: bool = True) -> "list[ModioFile]":
        """Return all released files for *mod_id*, newest first.

        Raises :class:`ModioAPIError` on network/HTTP failure.
        """
        if mod_id <= 0:
            raise ValueError("mod_id must be a positive integer")

        if use_cache:
            cached = _FILES_CACHE.get(mod_id)
            if cached and (time.time() - cached[0]) < _CACHE_TTL:
                return cached[1]

        url = f"{_API_ROOT}/games/{_GAME}/mods/{mod_id}/files"
        params = {
            "api_key": self._api_key,
            "_sort": "-date_added",
            "_limit": 100,
        }
        try:
            resp = self._session.get(url, params=params, timeout=self._timeout)
        except requests.RequestException as e:
            raise ModioAPIError(f"network error: {e}") from e

        if resp.status_code == 401:
            raise ModioAPIError("invalid or missing mod.io API key (HTTP 401)")
        if resp.status_code == 404:
            raise ModioAPIError(f"mod {mod_id} not found on mod.io (HTTP 404)")
        if resp.status_code != 200:
            raise ModioAPIError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            data = resp.json().get("data", [])
        except ValueError as e:
            raise ModioAPIError(f"invalid JSON response: {e}") from e

        files = [ModioFile.from_json(d) for d in data]
        # mod.io already sorts -date_added, but guard against API drift.
        files.sort(key=lambda f: f.date_added, reverse=True)
        _FILES_CACHE[mod_id] = (time.time(), files)
        return files

    def get_latest_file(self, mod_id: int, *, use_cache: bool = True) -> Optional[ModioFile]:
        """Return the newest released file for *mod_id*, or None if none."""
        files = self.get_mod_files(mod_id, use_cache=use_cache)
        return files[0] if files else None

    def get_mod_profile_url(self, mod_id: int) -> str:
        """Return the mod's public mod.io page URL (its ``profile_url``).

        The page is slug-based (e.g. .../m/ancient-mega-pack-rel); the numeric
        id does NOT resolve client-side, so we must fetch the real URL.
        Returns "" on failure.
        """
        if mod_id <= 0:
            return ""
        url = f"{_API_ROOT}/games/{_GAME}/mods/{mod_id}"
        try:
            resp = self._session.get(url, params={"api_key": self._api_key},
                                     timeout=self._timeout)
            if resp.status_code != 200:
                return ""
            return str(resp.json().get("profile_url") or "")
        except (requests.RequestException, ValueError) as e:
            app_log(f"mod.io: profile_url lookup failed for {mod_id}: {e}")
            return ""

    def test_key(self) -> bool:
        """Lightweight key validation: a cheap games query that needs auth.

        Returns True if the key is accepted, False otherwise.  Never raises.
        """
        url = f"{_API_ROOT}/games/{_GAME}"
        try:
            resp = self._session.get(url, params={"api_key": self._api_key},
                                     timeout=self._timeout)
        except requests.RequestException as e:
            app_log(f"mod.io key test network error: {e}")
            return False
        return resp.status_code == 200
