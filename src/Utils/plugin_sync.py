"""Sync plugins.txt / loadorder.txt when mods are enabled or disabled.

Tkinter-free port of gui/modlist_panel.py `_sync_plugins_for_toggle` (8730-8776):
disabling a mod removes its top-level plugin files from plugins.txt and
loadorder.txt; enabling appends any that aren't already listed. Unlike Tk (which
re-reads/re-writes both files once per mod), the whole batch is applied in a
single read-modify-write so Enable/Disable-all doesn't do N I/O round-trips.
"""

from __future__ import annotations

from pathlib import Path

from Utils.plugins import (
    read_plugins, write_plugins, read_loadorder, write_loadorder, PluginEntry,
)


def _mod_plugins(staging_root: Path, mod_name: str,
                 plugin_exts: set[str]) -> list[str]:
    """Top-level plugin filenames inside staging_root/<mod_name>/ (what Tk scans)."""
    mod_dir = staging_root / mod_name
    if not mod_dir.is_dir():
        return []
    try:
        return [f.name for f in mod_dir.iterdir()
                if f.is_file() and f.suffix.lower() in plugin_exts]
    except OSError:
        return []


def sync_plugins_for_mods(game, profile_dir: Path | None,
                          staging_root: Path | None,
                          changes: list[tuple[str, bool]],
                          log_fn=None) -> bool:
    """Apply mod enable/disable *changes* (``[(mod_name, now_enabled), ...]``)
    to plugins.txt + loadorder.txt. Returns True if either file was rewritten.

    When the same plugin belongs to both an enabled and a disabled mod in the
    batch, enable wins (the file is still provided by the enabled mod).
    """
    log = log_fn or (lambda m: None)
    if game is None or profile_dir is None or staging_root is None or not changes:
        return False
    plugin_exts = {e.lower() for e in
                   (getattr(game, "plugin_extensions", []) or [])}
    if not plugin_exts:
        return False
    plugins_path = profile_dir / "plugins.txt"
    # NB: do NOT bail when plugins.txt is missing. A game that has no plugins.txt
    # concept was already filtered out above (empty plugin_exts), so a missing
    # file here just means a fresh profile that has never had a plugin enabled.
    # Tk's _sync_plugins_for_toggle creates it via write_plugins in that case;
    # read_plugins returns [] and write_plugins creates the file (+ parents), so
    # the code below handles a missing file correctly. An earlier port bailed
    # here, which silently dropped a freshly-enabled mod's plugins on a new
    # profile (they never reached plugins.txt) — a Qt-vs-Tk regression.

    add: list[str] = []
    remove_lower: set[str] = set()
    for mod_name, now_enabled in changes:
        found = _mod_plugins(staging_root, mod_name, plugin_exts)
        if now_enabled and not found:
            # Enabling a mod whose staging folder has NO top-level plugin files
            # for this game's extensions. Expected for content-only mods; but if
            # this is the "copied mod has no plugins" report, it means the scan
            # of staging/<mod> came back empty (missing/symlinked/wrong-cased
            # folder, or the plugin sits in a subfolder Tk's flat scan misses).
            mdir = staging_root / mod_name
            log(f"WARN plugin sync: enabled mod \"{mod_name}\" has NO top-level "
                f"plugin files in {mdir} (is_dir={mdir.is_dir()}, "
                f"exts={sorted(plugin_exts)}) — nothing added to plugins.txt")
        for name in found:
            if now_enabled:
                add.append(name)
            else:
                remove_lower.add(name.lower())
    # Enable wins over disable within one batch.
    remove_lower -= {n.lower() for n in add}
    if not add and not remove_lower:
        return False

    star = getattr(game, "plugins_use_star_prefix", True)
    loadorder_path = profile_dir / "loadorder.txt"
    wrote = False

    entries = read_plugins(plugins_path, star_prefix=star)
    existing_lower = {e.name.lower() for e in entries}
    new_entries = [e for e in entries if e.name.lower() not in remove_lower]
    added = [n for n in add if n.lower() not in existing_lower]
    for name in added:
        new_entries.append(PluginEntry(name=name, enabled=True))
    if added or len(new_entries) != len(entries):
        write_plugins(plugins_path, new_entries, star_prefix=star)
        wrote = True

    loadorder = read_loadorder(loadorder_path)
    lo_lower = {n.lower() for n in loadorder}
    new_lo = [n for n in loadorder if n.lower() not in remove_lower]
    lo_added = [n for n in add if n.lower() not in lo_lower]
    new_lo.extend(lo_added)
    if lo_added or len(new_lo) != len(loadorder):
        write_loadorder(loadorder_path,
                        [PluginEntry(name=n, enabled=True) for n in new_lo])
        wrote = True

    if wrote:
        removed_ct = len(remove_lower & (existing_lower | lo_lower))
        log(f"Plugins synced: +{len(added)} / -{removed_ct} "
            f"for {len(changes)} mod(s).")
    return wrote
