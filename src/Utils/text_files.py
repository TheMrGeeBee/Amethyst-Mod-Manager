"""Toolkit-neutral discovery + content search for the Text Files tab.

Lists config/text files from four sources — mod folders (via filemap.txt), the
active profile folder, the vanilla game folder, and (Bethesda) My Games — grouped
by source. Ported from the pure-Python parts of the Tk `gui/plugin_panel_ini.py`
(internally "Ini Files"; the UI is "Text Files") so the Qt tab stays in lockstep.
Pure stdlib + Utils.* — no GUI toolkit.
"""

from __future__ import annotations

import os
from pathlib import Path

TEXT_EXTENSIONS = frozenset({
    ".ini", ".json", ".toml", ".txt", ".cfg", ".conf", ".config",
    ".yaml", ".yml", ".xml", ".log", ".md",
})

# Synthetic source names used in the mod_name field for non-mod entries.
SRC_GAME = "Game Folder"
SRC_PROFILE = "Profile"
SRC_MYGAMES = "My Games"

SOURCE_LABELS = (
    ("mod", "Mod folders"),
    ("profile", "Profile"),
    ("game", "Game folder"),
    ("mygames", "My Games"),
)
_SOURCE_ORDER = {key: i for i, (key, _label) in enumerate(SOURCE_LABELS)}

# Profile subfolders surfaced by other sources / holding backups — skipped so we
# don't dump thousands of duplicate mod files under "Profile".
_PROFILE_SKIP_DIRS = frozenset({"mods", "overwrite", "root_folder", "backups",
                                "fomod"})


def entry_source(mod_name: str) -> str:
    if mod_name == SRC_GAME:
        return "game"
    if mod_name == SRC_PROFILE:
        return "profile"
    if mod_name == SRC_MYGAMES:
        return "mygames"
    return "mod"


def display_name(rel_path: str) -> str:
    """'<parent>/<filename>' when nested, else just '<filename>' (Tk parity)."""
    p = Path(rel_path)
    if p.parent != Path("."):
        return f"{p.parent.name}/{p.name}"
    return p.name


def sort_key(entry: tuple[str, str, Path]) -> tuple:
    rel_path, mod_name, _p = entry
    src = entry_source(mod_name)
    return (_SOURCE_ORDER.get(src, len(_SOURCE_ORDER)),
            rel_path.lower(), mod_name.lower())


def resolve_file_path(rel_path: str, mod_name: str,
                      staging_root: Path) -> Path | None:
    """Resolve a filemap entry to a full path (case-insensitive fallback)."""
    if staging_root is None:
        return None
    from Utils.filemap import OVERWRITE_NAME, ROOT_FOLDER_NAME
    rel_path = rel_path.replace("\\", "/")
    if mod_name == OVERWRITE_NAME:
        base = staging_root.parent / "overwrite"
    elif mod_name == ROOT_FOLDER_NAME:
        base = staging_root.parent / "Root_Folder"
    else:
        base = staging_root / mod_name
    exact = base / rel_path
    if exact.exists():
        return exact
    current = base
    for segment in rel_path.split("/"):
        if not current.is_dir():
            return exact
        seg_lower = segment.lower()
        match = next((c for c in current.iterdir()
                      if c.name.lower() == seg_lower), None)
        if match is None:
            return exact
        current = match
    return current


def _parse_filemap(filemap_path: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        with filemap_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if "\t" in line:
                    rel, mod = line.split("\t", 1)
                    out.append((rel, mod))
    except OSError:
        return []
    return out


def _collect_profile_files(profile_dir: Path,
                           exts: frozenset) -> list[tuple[str, Path]]:
    if not profile_dir or not Path(profile_dir).is_dir():
        return []
    root = Path(profile_dir)
    out: list[tuple[str, Path]] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            if Path(dirpath) == root:
                dirnames[:] = [d for d in dirnames
                               if d.lower() not in _PROFILE_SKIP_DIRS]
            for name in filenames:
                fpath = Path(dirpath) / name
                if fpath.suffix.lower() not in exts:
                    continue
                if not fpath.is_file() or fpath.is_symlink():
                    continue
                out.append((fpath.relative_to(root).as_posix(), fpath))
    except OSError:
        return []
    return out


def _collect_mygames_files(game, exts: frozenset) -> list[tuple[str, Path]]:
    fn = getattr(game, "_mygames_paths", None) if game else None
    if not callable(fn):
        return []
    try:
        dirs = fn()
    except Exception:
        return []
    out: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for mygames in dirs:
        mygames = Path(mygames)
        if not mygames.is_dir():
            continue
        try:
            for fpath in mygames.rglob("*"):
                if fpath.suffix.lower() not in exts:
                    continue
                if fpath.is_symlink() or not fpath.is_file():
                    continue
                rel = fpath.relative_to(mygames).as_posix()
                if rel in seen:
                    continue
                seen.add(rel)
                out.append((rel, fpath))
        except OSError:
            continue
    return out


def discover_text_files(game, profile_dir: Path | None,
                        filemap_path: Path | None,
                        staging_root: Path | None) -> list[tuple[str, str, Path]]:
    """Return sorted [(rel_path, source_mod, full_path)] across all four sources.
    Port of Tk `_refresh_ini_files_tab`. Deferred/expensive — call off the hot
    path (recursive game + My Games scans)."""
    entries: list[tuple[str, str, Path]] = []

    # 1. Mod-deployed text files (filemap).
    if filemap_path and Path(filemap_path).is_file() and staging_root is not None:
        for rel, mod in _parse_filemap(Path(filemap_path)):
            if Path(rel).suffix.lower() not in TEXT_EXTENSIONS:
                continue
            full = resolve_file_path(rel, mod, staging_root)
            if full is not None:
                entries.append((rel, mod, full))

    # 2. Vanilla game folder (skip symlinks/hardlinks = deployed files).
    game_path = (game.get_game_path()
                 if game and hasattr(game, "get_game_path") else None)
    if game_path and Path(game_path).is_dir():
        root = Path(game_path)
        try:
            for fpath in root.rglob("*"):
                if fpath.suffix.lower() not in TEXT_EXTENSIONS:
                    continue
                try:
                    st = fpath.stat()
                except OSError:
                    continue
                if fpath.is_symlink() or st.st_nlink > 1:
                    continue
                entries.append((fpath.relative_to(root).as_posix(),
                                SRC_GAME, fpath))
        except OSError:
            pass

    # 3. Profile folder.
    if profile_dir is not None:
        for rel, fpath in _collect_profile_files(Path(profile_dir),
                                                 TEXT_EXTENSIONS):
            entries.append((rel, SRC_PROFILE, fpath))

    # 4. My Games (Bethesda).
    for rel, fpath in _collect_mygames_files(game, TEXT_EXTENSIONS):
        entries.append((rel, SRC_MYGAMES, fpath))

    entries.sort(key=sort_key)
    return entries


def content_search(entries: list[tuple[str, str, Path]],
                   keyword: str) -> set[tuple[str, str]]:
    """Return {(rel_path, mod_name)} whose file text contains *keyword*
    (case-insensitive). Port of Tk `_run_ini_content_search`."""
    needle = keyword.casefold()
    matched: set[tuple[str, str]] = set()
    for rel, mod, full in entries:
        try:
            if not full.is_file():
                continue
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                if needle in f.read().casefold():
                    matched.add((rel, mod))
        except OSError:
            continue
    return matched
