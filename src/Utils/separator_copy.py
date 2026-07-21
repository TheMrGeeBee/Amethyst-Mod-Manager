"""Copy / move a separator (marker only — name, color, lock state) to another
profile — tkinter-free. Companion to ``mod_copy.py``, but separators have no
on-disk folder, so this is purely a modlist.txt + profile_state.json edit."""

from __future__ import annotations

from pathlib import Path

from Utils.modlist import ModEntry, _SEPARATOR_SUFFIX, read_modlist, write_modlist


def separator_exists_in_profile(target_modlist_path: Path, name: str) -> bool:
    """True if a separator named *name* (internal, with suffix) already
    exists in *target_modlist_path*."""
    try:
        entries = read_modlist(target_modlist_path) if target_modlist_path.exists() else []
    except Exception:
        entries = []
    return any(e.is_separator and e.name == name for e in entries)


def register_separators_in_modlist(target_modlist_path: Path,
                                   target_profile_dir: Path,
                                   separators: "list[dict]") -> "list[str]":
    """Prepend *separators* — ordered highest-priority-first — to the target
    modlist.txt as a single block (dedup by internal name, skip existing),
    mirroring ``mod_copy.register_mods_in_modlist``. Each dict is
    ``{"name": internal-name-with-suffix, "color": str|None, "locked": bool}``.
    Also writes the color (keyed by internal name) and lock (keyed by display
    name) into the target profile's profile_state.json. Returns the internal
    names actually inserted."""
    try:
        entries = read_modlist(target_modlist_path) if target_modlist_path.exists() else []
    except Exception:
        entries = []
    existing = {e.name for e in entries if e.is_separator}
    new_entries: list[ModEntry] = []
    added: list[str] = []
    for sep in separators:
        name = sep["name"]
        if name in existing:
            continue
        new_entries.append(ModEntry(name=name, enabled=True, locked=True,
                                    is_separator=True))
        added.append(name)
    if new_entries:
        write_modlist(target_modlist_path, new_entries + entries)
        _write_separator_extras(target_profile_dir, separators, added)
    return added


def _write_separator_extras(target_profile_dir: Path, separators: "list[dict]",
                            added_names: "list[str]") -> None:
    from Utils.profile_state import (
        read_separator_colors, write_separator_colors,
        read_separator_locks, write_separator_locks)
    by_name = {s["name"]: s for s in separators}
    colors = read_separator_colors(target_profile_dir)
    locks = read_separator_locks(target_profile_dir)
    changed_colors = changed_locks = False
    for name in added_names:
        sep = by_name.get(name) or {}
        if sep.get("color"):
            colors[name] = sep["color"]
            changed_colors = True
        if sep.get("locked"):
            display = (name[:-len(_SEPARATOR_SUFFIX)]
                      if name.endswith(_SEPARATOR_SUFFIX) else name)
            locks[display] = True
            changed_locks = True
    if changed_colors:
        write_separator_colors(target_profile_dir, colors)
    if changed_locks:
        write_separator_locks(target_profile_dir, locks)
