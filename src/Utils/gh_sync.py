"""
gh_sync.py
Background sync of custom game handlers and wizard plugins from the
Amethyst-Mod-Manager ``Resources`` branch on GitHub.

Tkinter-free port of the old ``gui.py`` ``_sync_custom_handlers`` /
``_sync_plugins`` startup helpers, with two Qt-era differences:

* Custom handlers are all fetched from the flat ``Custom Handlers/`` root
  folder — the versioned ``X.Y/`` subfolder walking (``1.3`` etc.) is gone
  because those subfolders were removed from the repo.
* Plugins are fetched from ``Plugins/v2`` (Qt-compatible) instead of the
  ``Plugins`` root, which still holds the Tkinter-only versions.

Both functions run their network + disk work on a daemon thread and swallow
all errors so a startup sync never blocks or crashes the UI. An optional
``on_changed`` callback fires (on the worker thread — marshal to the GUI
thread yourself) when at least one file was written.
"""

from __future__ import annotations

import json
import threading
from typing import Callable, Optional

from Utils.config_paths import (
    get_custom_games_dir, get_plugins_dir, get_languages_dir,
)
from Utils.gh_cache import fetch, fetch_text
from Utils.ui_config import load_dev_mode

_CUSTOM_HANDLERS_API_URL = (
    "https://api.github.com/repos/ChrisDKN/Amethyst-Mod-Manager/contents/"
    "Custom%20Handlers?ref=Resources"
)

# Qt-compatible plugins live under Plugins/v2; the Plugins root holds the
# Tkinter versions that will not work with the Qt UI.
_PLUGINS_API_URL = (
    "https://api.github.com/repos/ChrisDKN/Amethyst-Mod-Manager/contents/"
    "Plugins/v2?ref=Resources"
)

# Compiled UI translations (amethyst_<code>.qm) live under Localisation/.
_LANGUAGES_API_URL = (
    "https://api.github.com/repos/ChrisDKN/Amethyst-Mod-Manager/contents/"
    "Localisation?ref=Resources"
)


def _write_if_changed(dest, raw: str) -> bool:
    """Write *raw* to *dest* only if it differs from the current contents.

    Returns True if the file was written.
    """
    try:
        if dest.is_file() and dest.read_text(encoding="utf-8") == raw:
            return False
    except Exception:
        pass
    dest.write_text(raw, encoding="utf-8")
    return True


def _write_bytes_if_changed(dest, data: bytes) -> bool:
    """Write binary *data* to *dest* only if it differs. Returns True if written.

    Used for .qm translation files (binary, unlike the text handlers/plugins).
    """
    try:
        if dest.is_file() and dest.read_bytes() == data:
            return False
    except Exception:
        pass
    dest.write_bytes(data)
    return True


def sync_custom_handlers(on_changed: Optional[Callable[[], None]] = None) -> None:
    """Background-download every custom handler .json, overwriting stale copies.

    Skips entirely in dev mode so a developer's in-place edits are never
    clobbered by the repo copy.
    """
    if load_dev_mode():
        return

    def _do():
        try:
            listing = fetch_text(_CUSTOM_HANDLERS_API_URL, timeout=15, min_interval=3600)
            if listing is None:
                return
            data = json.loads(listing)
            entries = [
                e for e in data
                if isinstance(e, dict)
                and e.get("type") == "file"
                and e.get("name", "").endswith(".json")
            ]
            changed = False
            dest_dir = get_custom_games_dir()
            for e in entries:
                filename = e.get("name", "")
                download_url = e.get("download_url")
                if not download_url:
                    continue
                try:
                    raw = fetch_text(
                        download_url, accept="*/*", timeout=10, min_interval=3600,
                    )
                    if raw is None:
                        continue
                    json.loads(raw)  # validate
                    if _write_if_changed(dest_dir / filename, raw):
                        changed = True
                except Exception:
                    pass
            if changed and on_changed is not None:
                on_changed()
        except Exception:
            pass

    threading.Thread(target=_do, daemon=True).start()


def sync_plugins(on_changed: Optional[Callable[[], None]] = None) -> None:
    """Background-download every Qt wizard plugin, overwriting stale copies.

    Pulls from ``Plugins/v2`` (Qt-compatible). Skips in dev mode.
    """
    if load_dev_mode():
        return

    def _do():
        try:
            listing = fetch_text(_PLUGINS_API_URL, timeout=15, min_interval=3600)
            if listing is None:
                return
            data = json.loads(listing)
            entries = [
                e for e in data
                if isinstance(e, dict)
                and e.get("type") == "file"
                and e.get("name", "").endswith(".py")
            ]
            changed = False
            dest_dir = get_plugins_dir()
            for e in entries:
                filename = e.get("name", "")
                download_url = e.get("download_url")
                if not download_url:
                    continue
                try:
                    raw = fetch_text(
                        download_url, accept="*/*", timeout=10, min_interval=3600,
                    )
                    if raw is None:
                        continue
                    if _write_if_changed(dest_dir / filename, raw):
                        changed = True
                except Exception:
                    pass
            if changed:
                try:
                    from Utils.plugin_loader import discover_plugins
                    discover_plugins(force=True)
                except Exception:
                    pass
                if on_changed is not None:
                    on_changed()
        except Exception:
            pass

    threading.Thread(target=_do, daemon=True).start()


def sync_languages(on_changed: Optional[Callable[[], None]] = None,
                   *, force: bool = False) -> None:
    """Background-download every UI translation (amethyst_<code>.qm) from the
    Resources branch ``Localisation/`` folder into the config languages/ dir.

    Mirrors sync_custom_handlers/sync_plugins: runs on a daemon thread, swallows
    all errors, only fires ``on_changed`` (on the worker thread — marshal it
    yourself) when at least one .qm was written.

    ``force=True`` (a manual "Sync language files" press) bypasses the dev-mode
    skip and the fetch cache interval so it always hits the network. The
    automatic startup sync leaves force=False (skips in dev, throttled by cache).

    NB: .qm files are binary, so they are fetched with fetch() (raw bytes) and
    written with _write_bytes_if_changed, unlike the text handlers/plugins.
    """
    if load_dev_mode() and not force:
        return

    def _do():
        try:
            listing = fetch_text(
                _LANGUAGES_API_URL, timeout=15,
                min_interval=(0 if force else 3600), force=force,
            )
            if listing is None:
                return
            data = json.loads(listing)
            entries = [
                e for e in data
                if isinstance(e, dict)
                and e.get("type") == "file"
                and e.get("name", "").startswith("amethyst_")
                and e.get("name", "").endswith(".qm")
            ]
            changed = False
            dest_dir = get_languages_dir()
            for e in entries:
                filename = e.get("name", "")
                download_url = e.get("download_url")
                if not download_url:
                    continue
                try:
                    raw = fetch(
                        download_url, accept="*/*", timeout=15,
                        min_interval=(0 if force else 3600), force=force,
                    )
                    if raw is None:
                        continue
                    if _write_bytes_if_changed(dest_dir / filename, raw):
                        changed = True
                except Exception:
                    pass
            if changed and on_changed is not None:
                on_changed()
        except Exception:
            pass

    threading.Thread(target=_do, daemon=True).start()
