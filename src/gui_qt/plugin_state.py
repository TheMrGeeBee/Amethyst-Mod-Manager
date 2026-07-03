"""Plugin list loading for the Qt Plugins tab.

Produces the ordered, flagged plugin list for the active game/profile by reusing
the backend: Utils.plugins (read_plugins / read_loadorder / write_plugins) and
Utils.plugin_parser (ESL / master header flags). Vanilla plugins are pinned to
the top, then mods follow saved loadorder.txt order.

v1 scope: list + order + enable-toggle + ESL/master flags. The deeper Tk logic
(orphan detection, Data_Core pruning, LOOT messages, bash tags, missing-master
checks) is deferred — the Flags column is structured to receive them later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from Utils.plugins import (
    read_plugins, read_loadorder, write_plugins, write_loadorder, PluginEntry,
)

# Flag bits for the plugin Flags column (drawn left→right in this order).
PF_MISSING = 1 << 0    # missing masters (red warning)
PF_LATE = 1 << 1       # master loads after a dependent (late master)
PF_VMM = 1 << 2        # version-mismatched master
PF_ESL = 1 << 3        # ESL / light-flagged
PF_LOOT = 1 << 4       # LOOT masterlist messages/requirements/incompatibilities
PF_DIRTY = 1 << 5      # dirty edits (needs cleaning)
PF_TAGS = 1 << 6       # bash tags
PF_MASTER = 1 << 7     # master (.esm or master-flagged)
PF_USERLIST = 1 << 8   # managed in userlist.yaml (white dot)
PF_UL_CYCLE = 1 << 9   # userlist rules form a broken cycle (dot turns red)


@dataclass
class PluginRow:
    name: str
    enabled: bool
    flags: int = 0
    vanilla: bool = False
    # Per-flag detail captured while computing the flag bits, so the Flags-column
    # tooltip can show the same content the Tk app shows (Tk parity).
    missing_masters: list[str] | None = None
    late_masters: list[str] | None = None
    vmm_masters: list[str] | None = None
    loot_info: dict | None = None


_EXT_ORDER = {".esm": 0, ".esp": 1, ".esl": 2}

_OVERWRITE_NAME = "[Overwrite]"


def plugins_path(game, profile: str) -> Path | None:
    if game is None or not profile:
        return None
    return game.get_profile_root() / "profiles" / profile / "plugins.txt"


def _find_plugin_in_mod_dir(mod_dir: Path, filename: str) -> Path | None:
    """Search *mod_dir* one level deep for *filename* (case-insensitive). Used
    when the filemap strips a prefix (e.g. 'Data Files') so the staging file
    lives in a subdir not reflected in the rel path. Ported from
    gui/plugin_panel.py:_find_plugin_in_mod_dir (pure Path logic)."""
    name_lower = filename.lower()
    if not mod_dir.is_dir():
        return None
    try:
        for entry in mod_dir.iterdir():
            if entry.is_file() and entry.name.lower() == name_lower:
                return entry
            if entry.is_dir():
                candidate = entry / filename
                if candidate.is_file():
                    return candidate
                for sub in entry.iterdir():
                    if sub.is_file() and sub.name.lower() == name_lower:
                        return sub
    except OSError:
        return None
    return None


def _resolve_plugin_paths(staging_dir: Path | None, data_dir: Path | None,
                          filemap_path: Path | None,
                          plugin_exts: tuple[str, ...]) -> dict[str, Path]:
    """Map plugin filename (lowercase) → its on-disk path, from THREE sources in
    priority order (Tk parity: gui/plugin_panel.py:_check_all_masters).

    Mod plugins live in staging / overwrite (resolved via filemap.txt), NOT in
    the vanilla Data dir, so reading a plugin header needs this resolver — using
    only data_dir misses every mod-added (incl. ESL-flagged) plugin.
    """
    paths: dict[str, Path] = {}
    exts = tuple(e.lower() for e in plugin_exts)

    # 1. filemap.txt → staging mods (and overwrite).
    overwrite_dir = staging_dir.parent / "overwrite" if staging_dir else None
    if filemap_path is not None and staging_dir is not None and filemap_path.is_file():
        try:
            for line in filemap_path.read_text(encoding="utf-8").splitlines():
                if "\t" not in line:
                    continue
                rel_path, mod_name = line.split("\t", 1)
                rel_path = rel_path.replace("\\", "/")
                if "/" in rel_path:
                    continue
                if not rel_path.lower().endswith(exts):
                    continue
                low = rel_path.lower()
                if mod_name == _OVERWRITE_NAME and overwrite_dir is not None:
                    paths[low] = overwrite_dir / rel_path
                else:
                    direct = staging_dir / mod_name / rel_path
                    if direct.is_file():
                        paths[low] = direct
                    else:
                        found = _find_plugin_in_mod_dir(
                            staging_dir / mod_name, rel_path)
                        paths[low] = found or direct
        except OSError:
            pass

    # 2. overwrite/ + overwrite/Data/ direct scan (plugins not yet in filemap).
    if overwrite_dir is not None and overwrite_dir.is_dir():
        for scan in (overwrite_dir, overwrite_dir / "Data"):
            if not scan.is_dir():
                continue
            try:
                for entry in scan.iterdir():
                    if entry.is_file() and entry.name.lower().endswith(exts):
                        paths.setdefault(entry.name.lower(), entry)
            except OSError:
                pass

    # 3. vanilla Data dir (or <data>_Core if present) via setdefault.
    if data_dir is not None and data_dir.is_dir():
        vanilla_dir = data_dir.parent / (data_dir.name + "_Core")
        scan_dir = vanilla_dir if vanilla_dir.is_dir() else data_dir
        try:
            for entry in scan_dir.iterdir():
                if entry.is_file() and entry.name.lower().endswith(exts):
                    paths.setdefault(entry.name.lower(), entry)
        except OSError:
            pass

    return paths


def resolve_plugin_paths_for_game(game, data_dir: Path | None = None
                                  ) -> dict[str, Path]:
    """Map each plugin filename (lowercase) → its REAL on-disk path (staging mod
    / overwrite / vanilla Data), using the same resolver load_plugins uses. Used
    by the Flags column and the plugins context menu (ESL toggle needs the path
    of the file to edit). Returns {} on any failure."""
    if data_dir is None:
        data_dir = (game.get_vanilla_plugins_path()
                    if hasattr(game, "get_vanilla_plugins_path") else None)
    try:
        staging = (game.get_effective_mod_staging_path()
                   if hasattr(game, "get_effective_mod_staging_path") else None)
        filemap_path = (staging.parent / "filemap.txt") if staging else None
        exts = tuple(x.lower() for x in (getattr(game, "plugin_extensions", []) or ())) \
            or (".esp", ".esm", ".esl")
        return _resolve_plugin_paths(staging, data_dir, filemap_path, exts)
    except Exception:
        return {}


def _filemap_deployed_plugins(game, plugin_exts: tuple[str, ...]) -> dict[str, str]:
    """Top-level plugin names that the CURRENT filemap.txt deploys — i.e. still
    provided by some enabled mod (or overwrite). Returns {lower: original_name}.

    A patcher mod (e.g. ESLifier Output) ships duplicate copies of plugins that
    other enabled mods also provide. Disabling it strips those names from
    plugins.txt (Tk parity: _sync_plugins_for_toggle removes a mod's own
    plugins unconditionally), but the other mods still deploy identically-named
    copies. Without this recovery those plugins vanish from the panel until a
    full re-sync. Tk recovers them in _refresh_plugins_tab via its Data/ orphan
    scan; we recover them from the freshly-rebuilt filemap instead.
    """
    staging = (game.get_effective_mod_staging_path()
               if hasattr(game, "get_effective_mod_staging_path") else None)
    if staging is None:
        return {}
    fm = staging.parent / "filemap.txt"
    if not fm.is_file():
        return {}
    exts = tuple(e.lower() for e in plugin_exts)
    found: dict[str, str] = {}
    try:
        for line in fm.read_text(encoding="utf-8").splitlines():
            if "\t" not in line:
                continue
            rel_path = line.split("\t", 1)[0].replace("\\", "/")
            if "/" in rel_path:
                continue   # top-level plugins only (matches deploy layout)
            low = rel_path.lower()
            if low.endswith(exts):
                found.setdefault(low, rel_path)
    except OSError:
        pass
    return found


def load_plugins(game, profile: str) -> list[PluginRow]:
    """Return the ordered plugin rows for *game*/*profile*, or [] if none."""
    p = plugins_path(game, profile)
    if p is None or not p.is_file():
        return []
    star = getattr(game, "plugins_use_star_prefix", True)
    entries = read_plugins(p, star_prefix=star)
    saved_order = read_loadorder(p.parent / "loadorder.txt")

    # Full vanilla set: base + DLC + Creation Club (.ccc), filtered to files
    # present in Data — same resolver the Tk app uses.
    try:
        from Utils.game_helpers import _vanilla_plugins_for_game
        vanilla = _vanilla_plugins_for_game(game)
    except Exception:
        vanilla = {n.lower(): n for n in getattr(game, "vanilla_plugins", [])}

    # Recover plugins still deployed by an enabled mod (per the fresh filemap)
    # but missing from plugins.txt — see _filemap_deployed_plugins. The guard is
    # plugins.txt entries only (NOT loadorder.txt): a disabled patcher mod's
    # sync strips its plugins from plugins.txt but leaves them in loadorder.txt,
    # so keying on loadorder would skip the very plugins we need to recover.
    exts = tuple(e.lower() for e in (getattr(game, "plugin_extensions", []) or [])) \
        or (".esp", ".esm", ".esl")
    listed_lower = {e.name.lower() for e in entries}
    for low, orig in _filemap_deployed_plugins(game, exts).items():
        if low in listed_lower or low in vanilla:
            continue
        entries.append(PluginEntry(name=orig, enabled=True))
        listed_lower.add(low)

    mod_map = {e.name.lower(): e for e in entries}

    ordered: list[PluginEntry] = []
    seen: set[str] = set()

    # Vanilla pinned first (in saved order where known, else ext-sorted).
    for name in saved_order:
        low = name.lower()
        if low in seen:
            continue
        if low in vanilla:
            ordered.append(PluginEntry(vanilla[low], True)); seen.add(low)
    for low, orig in sorted(vanilla.items(),
                            key=lambda kv: (_EXT_ORDER.get(Path(kv[0]).suffix, 9), kv[0])):
        if low not in seen:
            ordered.append(PluginEntry(orig, True)); seen.add(low)

    # Mods in saved loadorder order, then any leftovers from plugins.txt.
    for name in saved_order:
        low = name.lower()
        if low in seen:
            continue
        if low in mod_map:
            ordered.append(mod_map[low]); seen.add(low)
    for e in entries:
        if e.name.lower() not in seen:
            ordered.append(e); seen.add(e.name.lower())

    data_dir = (game.get_vanilla_plugins_path()
                if hasattr(game, "get_vanilla_plugins_path") else None)
    # Resolve each plugin's REAL path (staging mod / overwrite / Data) so header
    # flags (ESL, master, missing-master) work for mod plugins, not just vanilla.
    resolved = resolve_plugin_paths_for_game(game, data_dir)
    rows = [_to_row(e, vanilla, resolved, data_dir) for e in ordered]
    _apply_master_checks(rows, resolved, data_dir)
    _apply_loot_flags(rows, p.parent)
    _apply_userlist_flags(rows, p.parent)
    return rows


def _to_row(e: PluginEntry, vanilla: dict, resolved: dict[str, Path],
            data_dir: Path | None) -> PluginRow:
    low = e.name.lower()
    flags = 0
    path = resolved.get(low) or ((data_dir / e.name) if data_dir else None)
    if path and path.is_file():
        try:
            from Utils.plugin_parser import is_esl_flagged, is_master_flagged
            if is_esl_flagged(path) or low.endswith(".esl"):
                flags |= PF_ESL
            if is_master_flagged(path) or low.endswith(".esm"):
                flags |= PF_MASTER
        except Exception:
            if low.endswith(".esl"):
                flags |= PF_ESL
            if low.endswith(".esm"):
                flags |= PF_MASTER
    else:
        if low.endswith(".esl"):
            flags |= PF_ESL
        if low.endswith(".esm"):
            flags |= PF_MASTER
    return PluginRow(e.name, e.enabled, flags, low in vanilla)


def _apply_master_checks(rows: list[PluginRow], resolved: dict[str, Path],
                         data_dir: Path | None) -> None:
    """Flag missing / late / version-mismatched masters from each plugin's
    resolved on-disk path (staging/overwrite/Data), not just the Data dir — so
    the checks work for mod plugins on un-deployed profiles too. The check_*
    functions index plugin_paths by name.lower(), so key the dict that way."""
    names = [r.name for r in rows]
    # Lowercase-keyed paths: resolved path wins; fall back to data_dir/name.
    paths = {r.name.lower(): (resolved.get(r.name.lower())
                              or ((data_dir / r.name) if data_dir else None))
             for r in rows}
    paths = {k: v for k, v in paths.items() if v is not None}
    if not paths:
        return
    try:
        from Utils.plugin_parser import (
            check_missing_masters, check_late_masters,
            check_version_mismatched_masters)
        missing = check_missing_masters(names, paths)
        late = check_late_masters(names, paths)
        # vmm needs the vanilla Data dir for master sizes; skip if unavailable.
        vmm = (check_version_mismatched_masters(names, paths, data_dir)
               if data_dir is not None and data_dir.is_dir() else {})
    except Exception:
        return
    for r in rows:
        m = missing.get(r.name)
        if m:
            r.flags |= PF_MISSING
            r.missing_masters = list(m)
        lt = late.get(r.name)
        if lt:
            r.flags |= PF_LATE
            r.late_masters = list(lt)
        vm = vmm.get(r.name)
        if vm:
            r.flags |= PF_VMM
            r.vmm_masters = list(vm)


def _apply_loot_flags(rows: list[PluginRow], profile_dir: Path) -> None:
    """Flag LOOT messages / dirty edits / bash tags from the cached loot.json."""
    try:
        from LOOT.loot_sorter import read_loot_info
        data = read_loot_info(profile_dir)
    except Exception:
        return
    plugins = data.get("plugins", {}) if isinstance(data, dict) else {}
    version = data.get("version", 1) if isinstance(data, dict) else 1
    info: dict[str, dict] = {}
    if version >= 2:
        info = {k.lower(): v for k, v in plugins.items() if isinstance(v, dict) and v}
    else:
        info = {k.lower(): {"messages": v} for k, v in plugins.items()
                if isinstance(v, list) and v}
    for r in rows:
        d = info.get(r.name.lower())
        if not d:
            continue
        matched = False
        if d.get("messages") or d.get("requirements") or d.get("incompatibilities"):
            r.flags |= PF_LOOT
            matched = True
        if d.get("dirty"):
            r.flags |= PF_DIRTY
            matched = True
        if d.get("tags"):
            r.flags |= PF_TAGS
            matched = True
        if matched:
            # Keep the raw per-plugin dict so the Flags tooltip can render it.
            r.loot_info = d


def _apply_userlist_flags(rows: list[PluginRow], profile_dir: Path) -> None:
    """Flag plugins managed by <profile>/userlist.yaml (white dot; red when
    their rules form a cycle). Mirrors Tk _refresh_userlist_set + _predraw."""
    try:
        from Utils.userlist import read_userlist_state
        state = read_userlist_state(profile_dir / "userlist.yaml")
    except Exception:
        return
    if not state.plugins:
        return
    for r in rows:
        low = r.name.lower()
        if low in state.plugins:
            r.flags |= PF_USERLIST
            if low in state.cycle_plugins:
                r.flags |= PF_UL_CYCLE


_FILENAME_RE = re.compile(r'^Filename\(["\'](.+?)["\']\)$')


def format_loot_tooltip(info: dict, enabled_lower: set[str]) -> str:
    """Render a loot.json plugin-info dict into the multi-section tooltip string
    (messages / missing requirements / active incompatibilities / dirty edits /
    bash tags). Ported from Tk gui/plugin_panel_loot.py:_format_loot_tooltip.

    *enabled_lower* is the set of enabled plugin filenames (lowercase); it filters
    requirements to those not met by an enabled plugin, and incompatibilities to
    those whose conflicting plugin is currently enabled. The Tk app additionally
    resolves requirements against staged files / Nexus mod ids / script-extender
    detection — that refinement is deferred here (Qt v1)."""
    if not info:
        return ""
    sections: list[str] = []

    msgs = info.get("messages") or []
    if msgs:
        lines = []
        for m in msgs:
            prefix = {"error": "[!]", "warn": "[!]", "say": "[i]"}.get(
                m.get("type", "say"), "[i]")
            lines.append(f"{prefix} {m.get('text', '')}")
        sections.append("LOOT messages:\n" + "\n".join(lines))

    reqs = info.get("requirements") or []
    if reqs:
        lines = []
        for r in reqs:
            raw = r.get("name", "")
            display = r.get("display_name") or raw
            m = _FILENAME_RE.match(raw)
            fname = m.group(1) if m else raw
            fname_lower = fname.replace("\\", "/").lstrip("./").lstrip("../").lower()
            if fname_lower in enabled_lower:
                continue
            dm = _FILENAME_RE.match(display)
            if dm:
                display = dm.group(1)
            line = f"  - {display}"
            detail = r.get("detail", "")
            if detail:
                line += f" ({detail})"
            lines.append(line)
        if lines:
            sections.append("Requires (missing):\n" + "\n".join(lines))

    incs = info.get("incompatibilities") or []
    if incs:
        lines = []
        for i in incs:
            raw = i.get("name", "")
            display = i.get("display_name") or raw
            m = _FILENAME_RE.match(raw)
            fname = m.group(1) if m else raw
            fname_lower = fname.lower().lstrip("./").lstrip("../")
            if fname_lower not in enabled_lower:
                continue
            dm = _FILENAME_RE.match(display)
            if dm:
                display = dm.group(1)
            line = f"  - {display}"
            detail = i.get("detail", "")
            if detail:
                line += f" ({detail})"
            lines.append(line)
        if lines:
            sections.append("Incompatible with (currently active):\n" + "\n".join(lines))

    dirty = info.get("dirty") or []
    if dirty:
        lines = []
        for d in dirty:
            parts = []
            if d.get("itm"):
                parts.append(f"{d['itm']} ITM")
            if d.get("udr"):
                parts.append(f"{d['udr']} UDR")
            if d.get("nav"):
                parts.append(f"{d['nav']} deleted navmesh")
            counts = ", ".join(parts) if parts else "needs cleaning"
            line = f"  - {counts}"
            util = d.get("utility", "")
            if util:
                um = re.match(r'^\[(.+?)\]\(.+?\)$', util)
                line += f" — clean with {um.group(1) if um else util}"
            lines.append(line)
            detail = d.get("detail", "")
            if detail:
                lines.append(f"    {detail}")
        sections.append("Dirty edits:\n" + "\n".join(lines))

    tags = info.get("tags") or {}
    if tags:
        lines = []
        cur = tags.get("current") or []
        add = tags.get("add") or []
        rem = tags.get("remove") or []
        if cur:
            lines.append("  Current: " + ", ".join(cur))
        if add:
            lines.append("  Suggested (add): " + ", ".join(f"+{t}" for t in add))
        if rem:
            lines.append("  Suggested (remove): " + ", ".join(f"-{t}" for t in rem))
        if lines:
            sections.append("Bash Tags:\n" + "\n".join(lines))

    return "\n\n".join(sections)


def apply_loot_sort(rows: list[PluginRow], locked_indices: dict[int, PluginRow],
                    sorted_names: list[str],
                    include_vanilla: bool) -> "tuple[list[PluginRow], int]":
    """Re-interleave a LOOT sort result back into the row list.

    *rows* is the pre-sort order; *locked_indices* maps an index in *rows* → the
    locked PluginRow that must stay at that index; *sorted_names* is LOOT's order
    for the UNLOCKED plugins only. Returns (new_rows, visible_moved_count).

    Pure (no Qt) so it's unit-testable. Mirrors gui/plugin_panel_loot.py
    _apply_result (264-295).
    """
    vanilla_lower = {r.name.lower() for r in rows if r.vanilla}
    name_to_enabled = {r.name: r.enabled for r in rows}
    total = len(rows)
    pre_unlocked = [r.name for i, r in enumerate(rows) if i not in locked_indices]
    if len(sorted_names) != len(pre_unlocked):
        # Set mismatch (shouldn't happen — LOOT preserves the input set). Bail
        # to the original order rather than risk a bad interleave.
        return list(rows), 0
    it = iter(sorted_names)
    new_rows: list[PluginRow] = []
    for i in range(total):
        if i in locked_indices:
            new_rows.append(locked_indices[i])
        else:
            name = next(it)
            new_rows.append(PluginRow(
                name, name_to_enabled.get(name, True), 0,
                name.lower() in vanilla_lower))

    # Moved count over plugins the user actually sees (exclude hidden vanilla).
    def _visible(names):
        return [n for n in names
                if include_vanilla or n.lower() not in vanilla_lower]
    before = _visible([r.name for r in rows])
    after = _visible([r.name for r in new_rows])
    moved = sum(1 for i, n in enumerate(after)
                if i >= len(before) or before[i] != n)
    return new_rows, moved


def save_plugins(game, profile: str, rows: list[PluginRow]) -> None:
    """Write the plugin order + enable state back to disk.

    plugins.txt — mod plugins only (vanilla excluded unless the game includes
    them); loadorder.txt — the FULL order incl. vanilla so LOOT-sorted positions
    survive a refresh. Mirrors plugin_panel._save_plugins (Tk parity)."""
    p = plugins_path(game, profile)
    if p is None:
        return
    star = getattr(game, "plugins_use_star_prefix", True)
    include_vanilla = bool(getattr(game, "plugins_include_vanilla", False))
    mod_entries = [PluginEntry(r.name, r.enabled) for r in rows
                   if include_vanilla or not r.vanilla]
    write_plugins(p, mod_entries, star_prefix=star)
    full = [PluginEntry(r.name, True) for r in rows]
    write_loadorder(p.parent / "loadorder.txt", full)
    # Timestamp-ordered games (Oblivion/FO3/FNV) need deployed mtimes re-stamped.
    if game is not None and hasattr(game, "stamp_plugin_load_order"):
        try:
            game.stamp_plugin_load_order(profile)
        except Exception as exc:
            print(f"[gui_qt] stamp_plugin_load_order failed: {exc}", flush=True)
