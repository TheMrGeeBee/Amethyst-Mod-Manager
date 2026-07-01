"""Real modlist metadata (versions / installed dates / flags from meta.ini, and
conflicts from filemap overrides). Pure backend calls — no Qt, no gui.* — so
they can run on a worker thread.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from Utils.modlist import ModEntry


# Flag bits for the Flags column — only the ones the Tk app shows there.
# (FOMOD/BAIN are install methods, NOT flag icons; note.png = a real saved
#  user note, not FOMOD. brush = xedit-modified — both wired in a later pass.)
FLAG_UPDATE = 1 << 0       # has_update & not ignored
FLAG_ENDORSED = 1 << 1
FLAG_ROOT = 1 << 2
FLAG_MODIFIED_MF = 1 << 3  # modified in the Mod Files tab (excluded files/strip)
FLAG_MISSING_REQS = 1 << 4  # meta.missing_requirements has un-ignored entries
FLAG_COLLECTION_BUNDLED = 1 << 5  # meta.from_collection_bundled (bundled by a collection)
FLAG_COLLECTION_PATCHED = 1 << 6  # meta.from_collection_patched (diff-patched by a collection)


def _parse_missing_req_names(raw: str) -> list[str]:
    """Names from a meta.ini `missing_requirements` value: semicolon-separated
    `modId:name` pairs (the name half). Tolerates bare names / blank entries."""
    names: list[str] = []
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part:
            continue
        # "modId:Name" -> "Name"; bare "Name" -> "Name".
        names.append(part.split(":", 1)[1].strip() if ":" in part else part)
    return [n for n in names if n]


def read_meta_for_entries(entries: list[ModEntry], staging_dir: Path,
                          ignored_reqs: frozenset[str] = frozenset()):
    """Return a MetaInfo-ish tuple keyed by mod name.

    versions[name]   -> version string ("" if none)
    installed[name]  -> short date string ("" if none)
    flags[name]      -> int bitmask of FLAG_* above
    categories[name] -> Nexus category display name ("" if none)
    updates          -> set of mod names with a pending update
    fomod            -> set of mod names installed via FOMOD (meta.is_fomod)
    bain             -> set of mod names installed via BAIN (meta.is_bain)
    missing_reqs     -> set of mod names with un-ignored missing requirements

    *ignored_reqs* — requirement names the user has dismissed (per-profile); a
    mod is only flagged if it still has missing requirements outside this set.
    """
    versions: dict[str, str] = {}
    installed: dict[str, str] = {}
    flags: dict[str, int] = {}
    categories: dict[str, str] = {}
    updates: set[str] = set()
    fomod: set[str] = set()
    bain: set[str] = set()
    missing_reqs: set[str] = set()

    try:
        from Nexus.nexus_meta import read_meta
    except Exception:
        return (versions, installed, flags, categories, updates, fomod, bain,
                missing_reqs)

    for e in entries:
        if e.is_separator:
            continue
        meta_path = staging_dir / e.name / "meta.ini"
        if not meta_path.is_file():
            continue
        try:
            meta = read_meta(meta_path)
        except Exception:
            continue

        if meta.version:
            versions[e.name] = meta.version

        if meta.installed:
            try:
                installed[e.name] = datetime.fromisoformat(
                    meta.installed).strftime("%Y-%m-%d")
            except Exception:
                installed[e.name] = meta.installed[:10]

        if meta.category_name:
            categories[e.name] = meta.category_name

        if getattr(meta, "is_fomod", False):
            fomod.add(e.name)
        if getattr(meta, "is_bain", False):
            bain.add(e.name)

        bits = 0
        if meta.has_update and meta.latest_version != meta.ignored_version:
            bits |= FLAG_UPDATE
            updates.add(e.name)
        if meta.endorsed:
            bits |= FLAG_ENDORSED
        if meta.root_folder:
            bits |= FLAG_ROOT
        if getattr(meta, "missing_requirements", ""):
            unignored = [n for n in _parse_missing_req_names(meta.missing_requirements)
                         if n not in ignored_reqs]
            if unignored:
                bits |= FLAG_MISSING_REQS
                missing_reqs.add(e.name)
        # Collection-install provenance (stamped in meta.ini at install time).
        if getattr(meta, "from_collection_bundled", False):
            bits |= FLAG_COLLECTION_BUNDLED
        if getattr(meta, "from_collection_patched", False):
            bits |= FLAG_COLLECTION_PATCHED
        if bits:
            flags[e.name] = bits

    return (versions, installed, flags, categories, updates, fomod, bain,
            missing_reqs)


# ---- mod folder sizes (Size column) — ported from gui/modlist_panel.py --------
def _dir_size_bytes(path: Path) -> int:
    """Recursively sum file sizes under path (bytes). Safe to run in a thread."""
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                    elif entry.is_dir(follow_symlinks=False):
                        total += _dir_size_bytes(Path(entry.path))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _format_size(num_bytes: int) -> str:
    """Format a byte count as a short KB/MB/GB string."""
    if num_bytes <= 0:
        return ""
    kb = num_bytes / 1024
    if kb < 1024:
        return f"{kb:.0f} KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f} MB"
    return f"{mb / 1024:.2f} GB"


def compute_sizes(entries: list[ModEntry], staging_dir: Path) -> dict[str, str]:
    """Formatted folder size per non-separator mod. Walks the staging dir, so
    only call it when the Size column is visible (Tk gates the same way)."""
    sizes: dict[str, str] = {}
    if staging_dir is None:
        return sizes
    for e in entries:
        if e.is_separator:
            continue
        mod_dir = staging_dir / e.name
        if not mod_dir.is_dir():
            continue
        s = _format_size(_dir_size_bytes(mod_dir))
        if s:
            sizes[e.name] = s
    return sizes


def compute_plugin_stats(rows) -> dict:
    """Aggregate plugin stats for the plugins footer stats row: total / ESL /
    non-ESL. ESL = the PF_ESL (light-flagged or .esl) bit. In-memory, instant."""
    from gui_qt.plugin_state import PF_ESL
    total = len(rows)
    esl = sum(1 for r in rows if getattr(r, "flags", 0) & PF_ESL)
    return {"total": total, "esl": esl, "non_esl": total - esl}


# Qt display conflict codes (drawn by the delegate). Mirrors the Tk app's
# icon mapping: WINS→winner, LOSES→loser, PARTIAL→mixed, FULL→redundant.
DISP_NONE = 0
DISP_WINS = 1
DISP_LOSES = -1
DISP_PARTIAL = 2
DISP_FULL = 3


def display_codes_from_conflict_map(conflict_map: dict):
    """Map the backend's full conflict_map (CONFLICT_* from Utils.filemap:
    NONE=0 WINS=1 LOSES=2 PARTIAL=3 FULL=4) to the Qt delegate's display codes.
    This preserves FULL (fully-overridden / redundant) which the old
    override-set re-derivation lost."""
    from Utils.filemap import (
        CONFLICT_WINS, CONFLICT_LOSES, CONFLICT_PARTIAL, CONFLICT_FULL,
    )
    out: dict[str, int] = {}
    for name, code in (conflict_map or {}).items():
        if code == CONFLICT_WINS:
            out[name] = DISP_WINS
        elif code == CONFLICT_LOSES:
            out[name] = DISP_LOSES
        elif code == CONFLICT_PARTIAL:
            out[name] = DISP_PARTIAL
        elif code == CONFLICT_FULL:
            out[name] = DISP_FULL
    return out


def conflicts_from_filemap(overrides: dict, overridden_by: dict):
    """[legacy] Re-derive per-mod codes from override sets (no FULL). Kept for
    BSA conflicts which only expose override maps; prefer
    display_codes_from_conflict_map for loose conflicts."""
    codes: dict[str, int] = {}
    wins = {m for m, v in (overrides or {}).items() if v}
    loses = {m for m, v in (overridden_by or {}).items() if v}
    for m in wins | loses:
        if m in wins and m in loses:
            codes[m] = 2
        elif m in wins:
            codes[m] = 1
        else:
            codes[m] = -1
    return codes
