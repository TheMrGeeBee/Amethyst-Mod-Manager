"""Toolkit-neutral logic for the Mod Files tab (Tk + Qt share this).

The Mod Files tab shows a per-mod file tree with two checkbox columns:

  * **Top Level** — promotes/demotes a path as the deploy "top level" by adding
    / removing strip-prefix entries (``mod_strip_prefixes`` in profile_state).
    Checking a nested row strips every ancestor segment so the row deploys at
    the game root; unchecking reclaims the parent.
  * **Disable** — excludes individual files from deploy (``excluded_mod_files``).

All the fiddly bits — the strip-prefix promotion/demotion algorithm, the
exclusion save-merge that preserves filtered-out rows, conflict tagging, and the
raw file listing (with index fast-path + disk-scan fallback) — live here so both
the Tk mixin (gui/plugin_panel_mod_files.py) and the Qt view
(gui_qt/mod_files_view.py) drive identical behaviour. Pure stdlib + Utils.* —
no GUI toolkit imported.
"""

from __future__ import annotations

from pathlib import Path

from Utils.profile_state import (
    read_excluded_mod_files, write_excluded_mod_files,
    read_mod_strip_prefixes, write_mod_strip_prefixes,
)
from Utils.filemap import OVERWRITE_NAME


# ---------------------------------------------------------------------------
# File listing (raw, no strip applied) — index fast-path + scan fallback
# ---------------------------------------------------------------------------
def load_mod_files(game, mod_name: str, index_path: Path | None,
                   full_index: dict | None = None,
                   prefer_live: bool = False) -> dict[str, str]:
    """Return {rel_key (lower) -> rel_str (raw on-disk casing)} for *mod_name*.

    Reuses modindex.bin (raw per-mod casing) for stable mods; [Overwrite] and
    mods missing from the index are scanned live. Empty dict if nothing found.

    prefer_live=True ALWAYS scans the mod folder from disk first (only the one
    displayed mod, so it's cheap), falling back to the index if the folder is
    missing. The Mod Files tab uses this so the tree matches the REAL on-disk
    structure — a stale index (flat where disk is nested) otherwise builds the
    tree with wrong paths, which orphans strip-prefix entries on toggle.
    """
    files: dict[str, str] = {}

    def _scan() -> bool:
        mod_dir = _mod_dir_for(game, mod_name)
        if mod_dir is not None and mod_dir.is_dir():
            try:
                from Utils.filemap import _scan_dir
                _name, normal, root, _invalid = _scan_dir(mod_name, str(mod_dir))
                files.update(normal)
                files.update(root)
                return True
            except Exception:
                return False
        return False

    if prefer_live and _scan():
        return files

    if full_index is None and index_path is not None and index_path.is_file():
        try:
            from Utils.filemap import read_mod_index
            full_index = read_mod_index(index_path)
        except Exception:
            full_index = None

    # [Overwrite] changes outside the index rebuild cycle → always scan live.
    idx_entry = (full_index.get(mod_name)
                 if full_index and mod_name != OVERWRITE_NAME else None)
    if idx_entry is not None:
        normal, root = idx_entry
        files.update(normal)
        files.update(root)
        return files

    _scan()
    return files


def _mod_dir_for(game, mod_name: str) -> Path | None:
    if game is None:
        return None
    try:
        from Utils.filemap import ROOT_FOLDER_NAME
        if mod_name == OVERWRITE_NAME and hasattr(game, "get_effective_overwrite_path"):
            return Path(game.get_effective_overwrite_path())
        if mod_name == ROOT_FOLDER_NAME and hasattr(game, "get_effective_root_folder_path"):
            return Path(game.get_effective_root_folder_path())
        if hasattr(game, "get_effective_mod_staging_path"):
            return Path(game.get_effective_mod_staging_path()) / mod_name
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Conflict cache — which post-strip keys are contested + the filemap winner
# ---------------------------------------------------------------------------
def build_conflict_cache(index_path: Path | None,
                         profile_dir: Path | None,
                         full_index: dict | None = None) -> tuple[set[str], dict[str, str]]:
    """Return (contested_keys, filemap_winner).

    contested_keys: post-strip rel_keys (lower) owned by >1 ENABLED mod.
    filemap_winner: rel_key (lower) -> winning mod name (from filemap.txt).
    Disabled mods are excluded from the contest count (they deploy nothing).
    """
    if index_path is None:
        return set(), {}
    fm_path = index_path.parent / "filemap.txt"
    ml_path = (profile_dir / "modlist.txt") if profile_dir is not None else None

    filemap_winner: dict[str, str] = {}
    if fm_path.is_file():
        try:
            for line in fm_path.read_text(encoding="utf-8").splitlines():
                if "\t" in line:
                    rk, mn = line.split("\t", 1)
                    filemap_winner[rk.lower()] = mn
        except Exception:
            pass

    if full_index is None:
        try:
            from Utils.filemap import read_mod_index
            full_index = read_mod_index(index_path)
        except Exception:
            full_index = None

    contested: set[str] = set()
    if full_index:
        disabled: set[str] = set()
        if ml_path is not None and ml_path.is_file():
            try:
                from Utils.modlist import read_modlist
                disabled = {e.name for e in read_modlist(ml_path)
                            if not e.is_separator and not e.enabled}
            except Exception:
                disabled = set()
        counts: dict[str, int] = {}
        for mn, (normal, root) in full_index.items():
            if mn in disabled:
                continue
            for k in normal:
                counts[k] = counts.get(k, 0) + 1
            for k in root:
                counts[k] = counts.get(k, 0) + 1
        contested = {k for k, c in counts.items() if c > 1}
    return contested, filemap_winner


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def parent_path(path: str) -> str:
    """Parent folder path of *path* (or '')."""
    p = path.replace("\\", "/").rstrip("/")
    return p.rsplit("/", 1)[0] if "/" in p else ""


def ancestor_paths(path: str) -> list[str]:
    """Ancestor folder paths of *path*, root → parent."""
    p = path.replace("\\", "/").rstrip("/")
    if "/" not in p:
        return []
    out: list[str] = []
    cur = ""
    for seg in p.split("/")[:-1]:
        cur = f"{cur}/{seg}" if cur else seg
        out.append(cur)
    return out


def rel_key_after_strip(raw_rel_key: str, stripped_paths: set[str]) -> str:
    """Apply the saved strip prefixes (longest-match-first, like _scan_dir) to a
    raw rel_key so it can be looked up in the post-strip conflict/filemap data."""
    k = raw_rel_key
    for s in sorted(stripped_paths, key=len, reverse=True):
        sl = s.lower()
        if k == sl or k.startswith(sl + "/"):
            return k[len(sl):].lstrip("/")
    return k


def is_top_level(path: str, stripped_paths: set[str]) -> bool:
    """True if *path* deploys at the top level given the strip list (its parent
    path is fully covered by a strip entry, or it has no parent)."""
    parent = parent_path(path)
    if not parent:
        return True
    return parent.lower() in stripped_paths


# ---------------------------------------------------------------------------
# Tree building
# ---------------------------------------------------------------------------
def build_tree(files: dict[str, str], *,
               keep_rel_key=None) -> dict:
    """Build a nested dict from {rel_key: rel_str}. Folders are sub-dicts; files
    live in a "__files__" list of (filename, rel_key, rel_str). *keep_rel_key*
    is an optional predicate(rel_key, rel_str) -> bool to drop filtered rows."""
    tree: dict = {}
    for rel_key, rel_str in sorted(files.items()):
        if keep_rel_key is not None and not keep_rel_key(rel_key, rel_str):
            continue
        parts = rel_str.replace("\\", "/").split("/")
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node.setdefault("__files__", []).append((parts[-1], rel_key, rel_str))
    return tree


# ---------------------------------------------------------------------------
# Strip-prefix (Top Level) toggle — returns the new strip set
# ---------------------------------------------------------------------------
def toggle_top_level(path: str, stripped_paths: set[str]) -> set[str]:
    """Compute the new strip-prefix set after toggling *path*'s Top Level box.

    - path already stripped → un-strip it (+ any stripped descendants).
    - path currently top-level (a promoted row) → demote: remove the FULL
      ancestor chain that made it top-level, so a second click on the same row
      cleanly REVERSES the promotion (no orphaned `meshes`/`meshes/actors`
      leftovers). Ancestors still needed by another promoted sibling are kept.
    - else → promote: strip every ancestor segment up to it.

    Returns a NEW set (does not mutate the input). Root-level rows that are
    already top-level return the set unchanged (nothing to demote).
    """
    out = set(stripped_paths)
    path_l = path.lower()

    def _unstrip_subtree(root_l: str):
        prefix = root_l + "/"
        for s in list(out):
            if s == root_l or s.startswith(prefix):
                out.discard(s)

    if path_l in out:
        _unstrip_subtree(path_l)
    elif is_top_level(path, out):
        ancestors = [a.lower() for a in ancestor_paths(path)]
        if not ancestors:
            return out                      # root-level: nothing to demote
        # Remove only the ancestor strip entries that aren't ALSO promoting some
        # other branch. An ancestor `a` is still needed if a different stripped
        # entry sits strictly below `a` on a path that doesn't lead to `path`.
        to_remove = set(ancestors)
        keep_path = path_l
        for s in out:
            if s in to_remove:
                # Does another stripped descendant of `s` exist that ISN'T on
                # this row's own ancestor chain? If so, `s` is shared — keep it.
                for other in out:
                    if other == s:
                        continue
                    if other.startswith(s + "/") and not (
                            keep_path == other or keep_path.startswith(other + "/")
                            or other.startswith(keep_path + "/")):
                        to_remove.discard(s)
                        break
        for a in to_remove:
            out.discard(a)
    else:
        for anc in ancestor_paths(path):
            out.add(anc.lower())
    return out


def save_strip_prefixes(profile_dir: Path, mod_name: str,
                        stripped_paths: set[str],
                        case_hints: dict[str, str] | None = None) -> list[str]:
    """Persist *stripped_paths* for *mod_name*, preferring original-case forms
    from *case_hints* (lower → original). Returns the merged list written."""
    case_hints = case_hints or {}
    strip_map = read_mod_strip_prefixes(profile_dir, None)
    for e in strip_map.get(mod_name, []):
        if e:
            case_hints.setdefault(e.lower(), e)
    merged = sorted({case_hints.get(s, s) for s in stripped_paths if s})
    if merged:
        strip_map[mod_name] = merged
    else:
        strip_map.pop(mod_name, None)
    write_mod_strip_prefixes(profile_dir, strip_map)
    return merged


# ---------------------------------------------------------------------------
# Exclusion (Disable) save — merge visible state with preserved filtered rows
# ---------------------------------------------------------------------------
def save_exclusions(profile_dir: Path, mod_name: str,
                    visible_keys: set[str], excluded_visible: set[str]) -> set[str]:
    """Persist exclusions for *mod_name*. *visible_keys* are the post-strip keys
    currently shown (filters may hide others); *excluded_visible* the subset
    that's unchecked. Exclusions for hidden files are preserved untouched.
    Returns the full new excluded set for this mod."""
    all_excluded = read_excluded_mod_files(profile_dir, None)
    preserved = {k for k in all_excluded.get(mod_name, set())
                 if k not in visible_keys}
    excluded = preserved | excluded_visible
    if excluded:
        all_excluded[mod_name] = sorted(excluded)
    else:
        all_excluded.pop(mod_name, None)
    write_excluded_mod_files(profile_dir, all_excluded)
    return excluded


def read_exclusions(profile_dir: Path, mod_name: str) -> set[str]:
    """Saved excluded post-strip keys for *mod_name* (empty set if none)."""
    if profile_dir is None:
        return set()
    return set(read_excluded_mod_files(profile_dir, None).get(mod_name, set()))


def read_strip_prefixes(profile_dir: Path, mod_name: str) -> set[str]:
    """Saved strip-prefix entries (lowercased) for *mod_name*."""
    if profile_dir is None:
        return set()
    return {e.lower() for e in read_mod_strip_prefixes(profile_dir, None).get(mod_name, [])
            if e}
