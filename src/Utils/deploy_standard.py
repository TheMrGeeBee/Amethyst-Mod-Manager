"""
deploy_standard.py
Standard-mode deployment (Data/ games: Bethesda, Stardew, Sims 4, OpenMW).

Extracted from deploy.py during the 2026-04 refactor. No behaviour changes.
"""

from __future__ import annotations

import concurrent.futures
import os
import shutil
import time as _time
from pathlib import Path

from Utils.app_log import safe_log as _safe_log
from Utils.path_utils import has_path_traversal as _has_traversal
from Utils.deploy_shared import (
    LinkMode,
    _OVERWRITE_NAME,
    _build_mod_index,
    _default_core,
    _deploy_workers,
    _do_link,
    _get_staging_source_path,
    _mkdir_leaves,
    _path_under_root,
    _prebuild_mod_indexes,
    _resolve_root_path_str,
    _resolve_source,
    _timer,
)


# ---------------------------------------------------------------------------
# Step 1 — back up the game install directory
# ---------------------------------------------------------------------------

def move_to_core(
    deploy_dir: Path,
    core_dir: Path | None = None,
    log_fn=None,
) -> int:
    """Move all files from deploy_dir into core_dir (the vanilla backup).

    deploy_dir — directory whose contents will be moved out (e.g. Data/)
    core_dir   — destination for the backup; defaults to Data_Core/ sibling
    Returns the number of files moved.

    If core_dir already exists it is removed first so we always start clean.
    If deploy_dir is empty, core_dir is still created (empty) so restore always
    finds a core folder and does not report "nothing to restore".
    """
    _log = _safe_log(log_fn)
    core_dir = core_dir or _default_core(deploy_dir)

    if core_dir.exists():
        _log(f"  {core_dir.name} already exists — removing old backup first.")
        shutil.rmtree(core_dir)

    if not deploy_dir.is_dir():
        core_dir.mkdir(parents=True, exist_ok=True)
        return 0

    # Count files before the move so we can report the number moved.
    # os.walk gets file/dir classification from readdir d_type on Linux —
    # no extra stat() per entry unlike rglob + is_file().
    with _timer("move_to_core — count files"):
        count = sum(len(fns) for _, _, fns in os.walk(str(deploy_dir)))
    if not count:
        core_dir.mkdir(parents=True, exist_ok=True)
        return 0

    # Same filesystem → os.rename is a single instant syscall.
    # shutil.move falls back to copy+delete if cross-device.
    with _timer("move_to_core — rename dir"):
        core_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(deploy_dir), str(core_dir))

    # Recreate the (now-empty) deploy dir so downstream code finds it.
    deploy_dir.mkdir(parents=True, exist_ok=True)
    return count


# ---------------------------------------------------------------------------
# Step 2 — link mod files listed in filemap.txt into the deploy directory
# ---------------------------------------------------------------------------

def deploy_filemap(
    filemap_path: Path,
    deploy_dir: Path,
    staging_root: Path,
    mode: LinkMode = LinkMode.HARDLINK,
    strip_prefixes: set[str] | None = None,
    per_mod_strip_prefixes: dict[str, list[str]] | None = None,
    per_mod_deploy_dirs: dict[str, Path] | None = None,
    per_mod_link_modes: dict[str, LinkMode] | None = None,
    log_fn=None,
    progress_fn=None,
    symlink_exts: set[str] | None = None,
    exclude: set[str] | None = None,
    core_dir: "Path | None" = None,
) -> tuple[int, set[str]]:
    """Read filemap.txt and transfer every listed file into deploy_dir.

    filemap_path   — Profiles/<game>/filemap.txt
    deploy_dir     — destination directory (e.g. <game_path>/Data)
    staging_root   — Profiles/<game>/mods/
    mode           — transfer method
    strip_prefixes — same set passed to build_filemap; used to locate source
                     files whose leading folder was stripped from the filemap
                     path (e.g. rel_str "Nautilus/Nautilus.dll" may live on
                     disk as "plugins/Nautilus/Nautilus.dll").
    per_mod_strip_prefixes — optional dict mapping mod name to list of
                     top-level folder names to prepend when resolving (user-
                     configured "ignore" folders for that mod).
    progress_fn    — optional callable(done: int, total: int) called after
                     each file is transferred.

    Returns:
        (count, placed_lower)
        placed_lower is the set of lowercased rel paths successfully placed —
        pass it to deploy_core() so it can skip files already provided by mods.
    """
    _log = _safe_log(log_fn)
    _strip = {p.lower() for p in strip_prefixes} if strip_prefixes else set()
    _per_mod = per_mod_strip_prefixes or {}
    _per_deploy = per_mod_deploy_dirs or {}
    _per_mode = per_mod_link_modes
    if _per_mode is None:
        try:
            from Utils.deploy_shared import (
                load_separator_deploy_paths as _lsdp,
                expand_separator_link_modes as _eslm,
            )
            from Utils.modlist import read_modlist as _rml
            _sd = _lsdp(filemap_path.parent)
            _se = _rml(filemap_path.parent / "modlist.txt")
            _per_mode = _eslm(_sd, _se)
        except Exception:
            _per_mode = {}
    _per_mode = _per_mode or {}
    overwrite_dir = staging_root.parent / "overwrite"

    already_seen: set[str] = set()
    tasks: list[tuple[Path, Path, str]] = []
    placed_lower: set[str] = set()
    _exclude: set[str] = exclude or set()

    _overwrite_str = str(overwrite_dir)
    _staging_str   = str(staging_root)
    sorted_strip   = sorted(_strip) if _strip else []
    nocase_cache: dict[Path, dict[str, list[Path]]] = {}
    mod_index_cache: dict[Path, dict[str, Path]] = {}
    dst_dir_cache: dict[Path, dict[str, str]] = {}

    _t_resolve_start = _time.perf_counter()
    with filemap_path.open(encoding="utf-8") as f:
        _tab_lines = [ln.rstrip("\n") for ln in f if "\t" in ln]
    total_lines = len(_tab_lines)
    line_idx = 0

    _prebuild_mod_indexes(
        _tab_lines, overwrite_dir, staging_root, mod_index_cache,
        profile_dir=filemap_path.parent,
        strip_prefixes=strip_prefixes,
        per_mod_strip_prefixes=per_mod_strip_prefixes,
    )
    print(f"  [TIMER] deploy_filemap — pre-build mod indexes: "
          f"{_time.perf_counter() - _t_resolve_start:.3f}s")

    _t_resolve_loop = _time.perf_counter()
    _index_hits = 0
    _slow_hits = 0
    # Cache mod_root Path objects — avoids 92k Path / operations for ~520 mods
    _mod_root_cache: dict[str, Path] = {}
    # String-based caches for _resolve_root_path_str
    _deploy_dir_str = str(deploy_dir)
    _core_base_str = str(core_dir) if core_dir is not None else None
    _dir_listing_cache: dict[str, dict[str, str]] = {}
    _resolved_dir_cache: dict[str, str] = {}
    # {custom_deploy_dir_str: {top_level_folder_name, ...}} — populated as we
    # build tasks, consumed by the folder-replace pass below.
    _custom_top_roots: dict[str, set[str]] = {}
    for line in _tab_lines:
        rel_str, mod_name = line.split("\t", 1)
        # Guard against path traversal in filemap entries.
        if _has_traversal(rel_str) or _has_traversal(mod_name):
            _log(f"  WARN: skipping suspicious filemap entry — rel={rel_str!r} mod={mod_name!r}")
            continue
        rel_lower = rel_str.lower()
        if rel_lower in already_seen:
            continue
        already_seen.add(rel_lower)
        if rel_lower in _exclude:
            continue
        line_idx += 1

        # --- Fast path: O(1) mod-index lookup (no syscall) ---
        _mr = _mod_root_cache.get(mod_name)
        if _mr is None:
            _mr = overwrite_dir if mod_name == _OVERWRITE_NAME else staging_root / mod_name
            _mod_root_cache[mod_name] = _mr
        _idx = mod_index_cache.get(_mr)
        src_str: str | None = None
        if _idx is not None:
            _hit = _idx.get(rel_lower)
            if _hit is not None:
                src_str = _hit if isinstance(_hit, str) else str(_hit)
                _index_hits += 1
        if src_str is None:
            # Fall back to full resolve (stat-based)
            src_str = _resolve_source(
                mod_name, rel_str, rel_lower, overwrite_dir, staging_root,
                _overwrite_str, _staging_str, sorted_strip, _per_mod,
                nocase_cache, mod_index_cache,
            )
            if src_str is not None:
                _slow_hits += 1
        if src_str is None:
            _log(f"  WARN: source not found — {rel_str} ({mod_name})")
            continue

        effective_dir = _per_deploy.get(mod_name, deploy_dir)
        _core_s = _core_base_str if effective_dir is deploy_dir else None
        _eff_s = _deploy_dir_str if effective_dir is deploy_dir else str(effective_dir)
        dst_str = _resolve_root_path_str(_eff_s, rel_str, _dir_listing_cache,
                                         core_base_str=_core_s,
                                         resolved_dir_cache=_resolved_dir_cache)
        use_symlink = symlink_exts is not None and os.path.splitext(src_str)[1].lower() in symlink_exts
        override_mode = _per_mode.get(mod_name)
        is_custom_task = effective_dir is not deploy_dir
        tasks.append((src_str, dst_str, rel_lower, is_custom_task, use_symlink, override_mode))
        # Track top-level folder roots that custom-deploy mods are writing into,
        # so we can wholesale-replace any same-named folder at the destination
        # (with backup) before the per-file deploy runs. Files that the mod
        # ships at the root (no folder component in rel_str) are excluded —
        # those still get the existing file-by-file backup-and-replace path.
        if is_custom_task and "/" in rel_str:
            _custom_top_roots.setdefault(_eff_s, set()).add(rel_str.split("/", 1)[0])

        if progress_fn is not None and line_idx % 500 == 0:
            progress_fn(line_idx, total_lines)

    print(f"  [TIMER] deploy_filemap — resolve loop: {_time.perf_counter() - _t_resolve_loop:.3f}s "
          f"(index={_index_hits}, slow={_slow_hits})")
    total = len(tasks)
    if total == 0:
        return 0, placed_lower

    _custom_backup_dir = filemap_path.parent / "custom_deploy_backup"
    _custom_log_path   = filemap_path.parent / "custom_deploy_log.txt"

    # Clear any stale backup from a previous deploy before we start, so we
    # never mix old backed-up originals with new ones (same pattern as
    # deploy_filemap_to_root).
    if _custom_backup_dir.exists():
        shutil.rmtree(_custom_backup_dir)

    import stat as _stat_module

    # Wholesale-replace pass: for every top-level folder that custom-deploy
    # mods are writing into, move the existing folder at the destination
    # (if any) into custom_deploy_backup/, mirroring the absolute path so
    # restore can put it back. This is the "Saves/ should replace, not
    # merge" rule for custom-deploy separators. Symlinks at that path are
    # unlinked instead of moved (they're our own from a previous deploy).
    _folders_replaced = 0
    for _eff_dir_s, _top_names in _custom_top_roots.items():
        for _top in _top_names:
            # Resolve the destination folder using the same case-insensitive
            # path walker the per-file logic uses, so e.g. mod "Saves" lands
            # on disk-side "saves" if that's what already exists there.
            _existing_dir_str = _resolve_root_path_str(
                _eff_dir_s, _top, _dir_listing_cache,
                resolved_dir_cache=_resolved_dir_cache,
            )
            try:
                _est = os.lstat(_existing_dir_str)
            except OSError:
                continue
            if _stat_module.S_ISLNK(_est.st_mode):
                # Stale symlink from a previous deploy — drop it.
                try:
                    os.unlink(_existing_dir_str)
                except OSError as exc:
                    _log(f"  WARN: could not remove stale symlink {_existing_dir_str}: {exc}")
                continue
            if not _stat_module.S_ISDIR(_est.st_mode):
                continue
            _existing_p = Path(_existing_dir_str)
            _bak_dir = _custom_backup_dir / _existing_p.relative_to(_existing_p.anchor)
            try:
                _bak_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(_existing_dir_str, str(_bak_dir))
                _folders_replaced += 1
                _log(f"  Backed up existing folder {_existing_p.name}/ → custom_deploy_backup/")
            except OSError as exc:
                _log(f"  WARN: could not back up folder {_existing_dir_str}: {exc}")
            # Invalidate the resolved-dir cache so subsequent path resolution
            # against this destination doesn't reuse the now-moved entry.
            _resolved_dir_cache.pop(_existing_dir_str.lower(), None)
            _dir_listing_cache.pop(os.path.dirname(_existing_dir_str), None)

    # Pre-create all destination directories up front (single-threaded) to
    # avoid mkdir races inside the thread pool.
    with _timer("deploy_filemap — mkdir"):
        needed_dirs: set[str] = {os.path.dirname(dst) for _, dst, _, _is_custom, _, _ in tasks}
        _mkdir_leaves(needed_dirs)

    # Back up any pre-existing files at custom deploy locations so restore can
    # put the originals back.  Mirror each dst's absolute path as a relative
    # path inside _custom_backup_dir (strip leading slash) so structure is
    # preserved and files with the same name in different dirs never collide.
    # One lstat per task instead of islink+isfile (two stat-equivalent calls).
    # Files whose top-level folder was already wholesale-replaced above will
    # no longer exist here — the lstat just no-ops and the loop moves on.
    _custom_backup_str = str(_custom_backup_dir)
    for _src_s, dst_s, _rel_lower, is_custom, _use_sym, _ov in tasks:
        if not is_custom:
            continue
        try:
            _st = os.lstat(dst_s)
        except OSError:
            continue
        if _stat_module.S_ISLNK(_st.st_mode):
            os.unlink(dst_s)
        elif _stat_module.S_ISREG(_st.st_mode):
            dst_p = Path(dst_s)
            bak = _custom_backup_dir / dst_p.relative_to(dst_p.anchor)
            bak.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(dst_s, str(bak))
            _log(f"  Backed up existing {os.path.basename(dst_s)} → custom_deploy_backup/")

    linked = 0
    done_count = 0

    def _do_transfer(item: tuple[str, str, str, bool, bool, "LinkMode | None"]) -> tuple[str | None, tuple[str, OSError] | None]:
        src, dst, rel_lower, _is_custom, use_symlink, override_mode = item
        if use_symlink:
            effective_mode = LinkMode.SYMLINK
        elif override_mode is not None:
            effective_mode = override_mode
        else:
            effective_mode = mode
        err = _do_link(src, dst, effective_mode)
        if err is None:
            return rel_lower, None
        return None, (dst, err)

    _t_transfer = _time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=_deploy_workers()) as pool:
        for result, err in pool.map(_do_transfer, tasks):
            done_count += 1
            if result is not None:
                placed_lower.add(result)
                linked += 1
            elif err is not None:
                dst_err, exc = err
                _log(f"  WARN: could not transfer {dst_err}: {exc}")
            if progress_fn is not None and (done_count % 200 == 0 or done_count == total):
                progress_fn(done_count, total)
    print(f"  [TIMER] deploy_filemap — transfer {total} files: {_time.perf_counter() - _t_transfer:.3f}s")

    # Write a log of files placed in custom locations so cleanup knows what to
    # remove.  Each line is the absolute path of a deployed file.
    custom_deployed = [
        dst
        for _src, dst, rel_lower, is_custom, _use_sym, _ov in tasks
        if is_custom and rel_lower in placed_lower
    ]
    try:
        if custom_deployed:
            _custom_log_path.write_text("\n".join(custom_deployed), encoding="utf-8")
        elif _custom_log_path.exists():
            _custom_log_path.unlink()
    except OSError:
        pass

    return linked, placed_lower


# ---------------------------------------------------------------------------
# Step 3 — fill gaps with vanilla files from the backup
# ---------------------------------------------------------------------------

def deploy_core(
    deploy_dir: Path,
    already_placed: set[str],
    core_dir: Path | None = None,
    mode: LinkMode = LinkMode.HARDLINK,
    log_fn=None,
    progress_fn=None,
) -> int:
    """Transfer files from core_dir into deploy_dir for any path not already
    covered by a mod.

    deploy_dir     — destination (e.g. <game_path>/Data)
    already_placed — lowercased rel paths already placed by deploy_filemap()
    core_dir       — vanilla backup directory; defaults to Data_Core/ sibling
    progress_fn    — optional callable(done: int, total: int)
    Returns the number of files transferred.
    """
    _log = _safe_log(log_fn)
    core_dir = core_dir or _default_core(deploy_dir)

    if not core_dir.is_dir():
        return 0

    # Use os.walk to collect files — avoids per-file stat() that rglob+is_file does.
    _core_str = str(core_dir)
    _core_prefix_len = len(_core_str) + 1  # +1 for the trailing separator

    _t_core_walk = _time.perf_counter()
    tasks_core: list[tuple[str, str]] = []  # (src_str, rel_str)
    for dirpath, _dirnames, filenames in os.walk(_core_str):
        for fname in filenames:
            src_str = dirpath + "/" + fname
            rel_str = src_str[_core_prefix_len:]
            if rel_str.replace("\\", "/").lower() not in already_placed:
                tasks_core.append((src_str, rel_str))
    print(f"  [TIMER] deploy_core — walk + filter: {_time.perf_counter() - _t_core_walk:.3f}s")

    if not tasks_core:
        return 0

    total = len(tasks_core)

    # Resolve destination paths using case-insensitive directory matching so
    # that core files (e.g. Data_Core/Scripts/) merge into any same-name
    # directory already created by mods (e.g. Data/scripts/) rather than
    # producing a duplicate folder with different casing.
    _deploy_dir_str = str(deploy_dir)
    _dir_listing_cache: dict[str, dict[str, str]] = {}
    _resolved_dir_cache: dict[str, str] = {}
    resolved_tasks: list[tuple[str, str]] = []  # (src_str, dst_str)
    for src_str, rel_str in tasks_core:
        dst_str = _resolve_root_path_str(_deploy_dir_str, rel_str,
                                         _dir_listing_cache,
                                         resolved_dir_cache=_resolved_dir_cache)
        resolved_tasks.append((src_str, dst_str))

    # Deduplicate destination directories with a set before creating them.
    needed_dirs: set[str] = set()
    for _, dst_str in resolved_tasks:
        needed_dirs.add(os.path.dirname(dst_str))
    _mkdir_leaves(needed_dirs)

    linked = 0
    done_count = 0

    def _do_core(item: tuple[str, str]) -> tuple[bool, str, OSError | None]:
        src, dst_str = item
        err = _do_link(src, dst_str, mode)
        return (True, dst_str, None) if err is None else (False, dst_str, err)

    _t_core_transfer = _time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=_deploy_workers()) as pool:
        for ok, rel_str, exc in pool.map(_do_core, resolved_tasks):
            done_count += 1
            if ok:
                linked += 1
            else:
                _log(f"  WARN: could not transfer {rel_str}: {exc}")
            if progress_fn is not None:
                progress_fn(done_count, total)
    print(f"  [TIMER] deploy_core — transfer {total} files: {_time.perf_counter() - _t_core_transfer:.3f}s")

    return linked


# ---------------------------------------------------------------------------
# Restore — undo a deploy
# ---------------------------------------------------------------------------

def restore_data_core(
    deploy_dir: Path,
    core_dir: Path | None = None,
    overwrite_dir: Path | None = None,
    staging_root: Path | None = None,
    strip_prefixes: set[str] | None = None,
    log_fn=None,
) -> int:
    """Undo a deploy: clear deploy_dir and move core_dir contents back.

    deploy_dir     — directory to restore (e.g. <game_path>/Data)
    core_dir       — vanilla backup to restore from; defaults to Data_Core/ sibling
    overwrite_dir  — if given, any file in deploy_dir that is not a deployed mod
                     file and not present in core_dir (i.e. created at runtime by
                     the game or a mod) is moved here before clearing, preserving
                     its relative path.  Existing files in overwrite_dir are
                     overwritten.  Pass Profiles/<game>/overwrite/.
    staging_root   — if given with strip_prefixes, files listed in filemap/modindex
                     whose staging source no longer exists (e.g. xEdit deleted the
                     original after saving an edited plugin) are rescued to
                     overwrite/ instead of being removed.  Pass the mod staging
                     root (e.g. Profiles/<game>/mods/).
    strip_prefixes — top-level folder names to try when resolving staging paths
                     (e.g. {"Data"} for Bethesda games).
    Returns the number of files restored.

    If core_dir does not exist (e.g. the deploy dir was empty at deploy time
    so move_to_core skipped creating it), the deploy dir is simply cleared and
    0 is returned — no error is raised.
    """
    _log = _safe_log(log_fn)
    core_dir = core_dir or _default_core(deploy_dir)

    if not core_dir.is_dir():
        _log(f"  No {core_dir.name}/ found — nothing to restore (skipping).")
        return 0

    # Rescue runtime-created files into overwrite/ before wiping deploy_dir.
    # A file is runtime-created if it:
    #   - is not a symlink (symlinks are deployed mod files)
    #   - has a single hard-link count (nlink > 1 means it is a deployed hardlink)
    #   - is not present in core_dir (not a vanilla file)
    #   - is not listed in filemap.txt (copied mod files have nlink==1 when their
    #     staging copy was replaced after deploy, breaking the hardlink)
    #   - is not listed in modindex.txt (catches cross-profile mod files not in
    #     the current filemap.txt — e.g. when switching profiles)
    #
    # Exception: If staging_root and strip_prefixes are provided, files that ARE
    # in filemap/modindex are still rescued when their staging source is missing.
    # This handles the xEdit flow: user edits plugin → xEdit saves → xEdit closes
    # and deletes the original from both Data and staging → the edited copy in
    # Data is the only remaining version and must be rescued to overwrite.
    # If the rescue walk runs it builds core_path as a side-effect, which
    # gives us the file count for free.  -1 is the sentinel for "rescue walk
    # didn't run, fall back to a dedicated count walk below".
    restored = -1
    if overwrite_dir is not None and deploy_dir.is_dir():
        # Build core_lower using os.walk — avoids per-file stat() from rglob+is_file.
        # Also capture (st_ino, st_size, st_mtime_ns) so we can detect when a
        # deployed vanilla file has been replaced by an external tool (e.g.
        # xEdit Quick Auto Clean writes a fresh file over the deployed symlink
        # or hardlink).  An "original" deploy shares the core inode (hardlink)
        # or matches size+mtime (copy).  A replaced file fails both checks.
        _t_rescue_start = _time.perf_counter()
        _core_str = str(core_dir)
        _core_plen = len(_core_str) + 1
        core_lower: set[str] = set()
        core_stat: dict[str, tuple[int, int, int]] = {}
        core_path: dict[str, str] = {}
        for _dp, _dns, _fns in os.walk(_core_str):
            for _fn in _fns:
                _cp = _dp + "/" + _fn
                _rel = _cp[_core_plen:].lower()
                core_lower.add(_rel)
                core_path[_rel] = _cp
                try:
                    _cs = os.lstat(_cp)
                    core_stat[_rel] = (_cs.st_ino, _cs.st_size, _cs.st_mtime_ns)
                except OSError:
                    pass
        filemap_path = overwrite_dir.parent / "filemap.txt"
        filemap_lower: set[str] = set()
        filemap_rel_to_mod: dict[str, str] = {}
        if filemap_path.is_file():
            with filemap_path.open(encoding="utf-8") as _fm:
                for _line in _fm:
                    _line = _line.rstrip("\n")
                    if "\t" in _line:
                        rel_str, mod_name = _line.split("\t", 1)
                        rel_lower = rel_str.lower()
                        filemap_lower.add(rel_lower)
                        filemap_rel_to_mod[rel_lower] = mod_name
        # Build a set of every file known to any mod in the index (all profiles,
        # all mods, enabled or disabled).  Runtime-created files won't appear here,
        # so any hit means "this is a mod file, don't rescue it".
        modindex_lower: set[str] = set()
        modindex_rel_to_mods: dict[str, list[str]] = {}
        try:
            from Utils.filemap import read_mod_index
            _index = read_mod_index(overwrite_dir.parent / "modindex.bin")
            if _index:
                for _mod_name, (_normal, _root) in _index.items():
                    if _mod_name == _OVERWRITE_NAME:
                        continue
                    for rel_key in _normal.keys():
                        modindex_lower.add(rel_key)
                        modindex_rel_to_mods.setdefault(rel_key, []).append(_mod_name)
        except Exception:
            pass
        _strip = {p.lower() for p in (strip_prefixes or set())}
        _staging = staging_root
        rescued = 0
        rescued_to_mod = 0
        rescued_to_overwrite = 0
        rescued_edited_vanilla = 0
        # Track rel_strs rescued to overwrite/ so we can update modindex.bin
        # by appending entries instead of re-walking the entire overwrite tree.
        rescued_overwrite_rels: list[str] = []
        _deploy_str = str(deploy_dir)
        _deploy_plen = len(_deploy_str) + 1
        _overwrite_str = str(overwrite_dir)
        _staging_str = str(_staging) if _staging else ""
        _lstat = os.lstat
        # Use os.scandir-based walk: DirEntry.is_symlink() and is_file() use
        # d_type from readdir on Linux — no extra syscall.  Only non-symlink
        # regular files need a real lstat() to check st_nlink.
        _scandir = os.scandir
        _walk_stack = [_deploy_str]
        while _walk_stack:
            _cur_dir = _walk_stack.pop()
            try:
                _scan_it = _scandir(_cur_dir)
            except OSError:
                continue
            with _scan_it:
                for _de in _scan_it:
                    if _de.is_dir(follow_symlinks=False):
                        _walk_stack.append(_de.path)
                        continue
                    if _de.is_symlink():
                        continue  # deployed mod symlink — free check via d_type
                    if not _de.is_file(follow_symlinks=False):
                        continue
                    src_str = _de.path
                    try:
                        st = _lstat(src_str)
                    except OSError:
                        continue
                    if st.st_nlink > 1:
                        continue  # deployed mod hardlink
                    rel_str = src_str[_deploy_plen:]
                    rel_lower = rel_str.lower()
                    if rel_lower in core_lower:
                        # Vanilla path — but the file might have been replaced by
                        # an external tool (e.g. xEdit Quick Auto Clean deletes
                        # the symlink/hardlink and writes a fresh file).  If the
                        # on-disk file no longer matches the core backup by inode
                        # or by (size, mtime), overwrite the core copy with the
                        # edited file so the rmtree+rename below restores the
                        # edited vanilla plugin back into Data/.
                        _cs = core_stat.get(rel_lower)
                        if _cs is not None:
                            _core_ino, _core_sz, _core_mt = _cs
                            if (st.st_ino == _core_ino or
                                (st.st_size == _core_sz and st.st_mtime_ns == _core_mt)):
                                continue  # untouched vanilla — restore from core
                            core_dst = core_path.get(rel_lower)
                            if core_dst is not None:
                                try:
                                    os.replace(src_str, core_dst)
                                    rescued += 1
                                    rescued_edited_vanilla += 1
                                except OSError:
                                    pass
                            continue
                        continue  # vanilla file — will be restored from core
                    # Check if we would skip as a known mod file
                    in_filemap = rel_lower in filemap_lower
                    in_modindex = rel_lower in modindex_lower
                    if in_filemap or in_modindex:
                        # xEdit orphan check: if staging source is missing, rescue the
                        # edited file (e.g. xEdit deleted original from staging on close)
                        if _staging and _strip:
                            mods_to_check: list[str] = []
                            if in_filemap:
                                m = filemap_rel_to_mod.get(rel_lower)
                                if m:
                                    mods_to_check.append(m)
                            if in_modindex:
                                for m in modindex_rel_to_mods.get(rel_lower, []):
                                    if m and m not in mods_to_check:
                                        mods_to_check.append(m)
                            staging_path: Path | None = None
                            target_mod: str | None = None
                            for mod_name in mods_to_check:
                                if mod_name == _OVERWRITE_NAME:
                                    mod_root = overwrite_dir
                                else:
                                    mod_root = _staging / mod_name
                                found = _get_staging_source_path(mod_root, rel_str, _strip)
                                if found is not None:
                                    staging_path = found
                                    target_mod = mod_name
                                    break
                            if staging_path is not None and target_mod is not None:
                                staging_path.parent.mkdir(parents=True, exist_ok=True)
                                shutil.move(src_str, str(staging_path))
                                rescued += 1
                                rescued_to_mod += 1
                                continue
                            # xEdit orphan: staging missing — put file back in original mod or overwrite
                            target_mod = (
                                filemap_rel_to_mod.get(rel_lower)
                                or (modindex_rel_to_mods.get(rel_lower) or [None])[0]
                            )
                            if target_mod:
                                if target_mod == _OVERWRITE_NAME:
                                    dst_str = _overwrite_str + "/" + rel_str
                                    rescued_to_overwrite += 1
                                    rescued_overwrite_rels.append(rel_str)
                                else:
                                    dst_str = _staging_str + "/" + target_mod + "/" + rel_str
                                    rescued_to_mod += 1
                                os.makedirs(os.path.dirname(dst_str), exist_ok=True)
                                shutil.move(src_str, dst_str)
                                rescued += 1
                                continue
                        else:
                            continue  # no staging check — skip as before
                    # Genuine runtime-generated file (never in a mod) — goes to overwrite
                    dst_str = _overwrite_str + "/" + rel_str
                    os.makedirs(os.path.dirname(dst_str), exist_ok=True)
                    shutil.move(src_str, dst_str)
                    rescued += 1
                    rescued_to_overwrite += 1
                    rescued_overwrite_rels.append(rel_str)
        if rescued:
            if rescued_to_mod:
                _log(f"  Rescued {rescued_to_mod} file(s) back to mod folder(s).")
            if rescued_to_overwrite:
                _log(f"  Rescued {rescued_to_overwrite} runtime-created file(s) → overwrite/.")
            if rescued_edited_vanilla:
                _log(f"  Preserved {rescued_edited_vanilla} edited vanilla file(s) (e.g. xEdit-cleaned).")
            # Update modindex.bin so the next build_filemap call immediately
            # sees the rescued files under [Overwrite] without a full rescan.
            # We append the rel_strs we recorded as we rescued — far cheaper
            # than rglob-ing the entire overwrite tree.
            if rescued_overwrite_rels:
                try:
                    from Utils.filemap import update_mod_index, read_mod_index
                    index_path = overwrite_dir.parent / "modindex.bin"
                    existing = read_mod_index(index_path) or {}
                    existing_normal, existing_root = existing.get(_OVERWRITE_NAME, ({}, {}))
                    new_normal: dict[str, str] = dict(existing_normal)
                    for _rel_str in rescued_overwrite_rels:
                        # Normalise separators for cross-platform safety
                        _rel_posix = _rel_str.replace("\\", "/")
                        new_normal[_rel_posix.lower()] = _rel_posix
                    update_mod_index(index_path, _OVERWRITE_NAME, new_normal, existing_root)
                except Exception:
                    pass
        print(f"  [TIMER] restore — rescue walk: {_time.perf_counter() - _t_rescue_start:.3f}s")
        # core_path was populated by the rescue walk above — one entry per
        # core file, so len() is our return-value count without a second walk.
        restored = len(core_path)

    # Fallback count walk — only runs when the rescue walk above was skipped
    # (overwrite_dir is None, or deploy_dir doesn't exist).
    if restored < 0:
        with _timer("restore — count core files"):
            _core_str2 = str(core_dir)
            restored = 0
            for _dp2, _dns2, _fns2 in os.walk(_core_str2):
                restored += len(_fns2)

    # Wipe deploy_dir and rename core_dir in its place — single rmtree + O(1)
    # rename on the same filesystem.  No need to clear first then rmtree again.
    with _timer("restore — rmtree + rename"):
        if deploy_dir.is_dir():
            shutil.rmtree(deploy_dir)
        _log(f"  Cleared {deploy_dir.name}/.")
        shutil.move(str(core_dir), str(deploy_dir))

    return restored


# ---------------------------------------------------------------------------
# Undeploy — remove a mod's deployed files from the game directory
# ---------------------------------------------------------------------------

def undeploy_mod_files(
    mod_names: list[str],
    deploy_dir: "Path | None",
    game_root: "Path | None",
    index_path: Path,
    log_fn=None,
) -> int:
    """Remove any files belonging to the given mods from the game's deploy
    directory and/or game root, using the modindex.bin to find them.

    Call this *before* deleting the staging folders so that hardlinks/copies
    that are still sitting in the game directory are cleaned up.  Without this
    step, restore_data_core() would classify the leftover files as
    runtime-generated and move them to overwrite/ as a false positive.

    mod_names  — list of mod folder names to undeploy
    deploy_dir — the game's mod data directory (e.g. <game_path>/Data/).
                 May be None if the game has no separate data dir.
    game_root  — the game's install root (used for root-deployed files).
                 May be None if unknown / game not configured.
    index_path — path to modindex.bin (typically <profile_root>/modindex.bin)
    log_fn     — optional logging callable

    Returns the total number of files removed.
    """
    _log = _safe_log(log_fn)

    # Load the index; nothing to do if it is absent.
    try:
        from Utils.filemap import read_mod_index
        index = read_mod_index(index_path)
    except Exception:
        index = None
    if not index:
        return 0

    removed = 0
    dirs_to_prune: set[Path] = set()

    # Collect every target up front, then unlink them in parallel.  Each task
    # is one lstat + (maybe) one unlink — the unlinks dominate cost across
    # thousands of files for a multi-mod undeploy.
    import stat as _stat
    targets: list[Path] = []

    for mod_name in mod_names:
        entry = index.get(mod_name)
        if entry is None:
            continue
        normal_files, root_files = entry

        if deploy_dir is not None and normal_files:
            for rel_str in normal_files.values():
                target = deploy_dir / rel_str
                if not _path_under_root(target, deploy_dir):
                    _log(f"  SKIP (path traversal): {rel_str}")
                    continue
                targets.append(target)

        if game_root is not None and root_files:
            for rel_str in root_files.values():
                target = game_root / rel_str
                if not _path_under_root(target, game_root):
                    _log(f"  SKIP (path traversal): {rel_str}")
                    continue
                targets.append(target)

    def _unlink_one(p: Path) -> tuple[int, Path | None, str | None]:
        try:
            st = os.lstat(p)
        except OSError:
            return 0, None, None
        if _stat.S_ISLNK(st.st_mode) or _stat.S_ISREG(st.st_mode):
            try:
                os.unlink(p)
                return 1, p.parent, None
            except OSError as exc:
                return 0, None, f"  WARN: could not remove deployed file {p}: {exc}"
        return 0, None, None

    if targets:
        import concurrent.futures
        from Utils.deploy_shared import _deploy_workers
        with concurrent.futures.ThreadPoolExecutor(max_workers=_deploy_workers()) as pool:
            for n, parent, warn in pool.map(_unlink_one, targets):
                removed += n
                if parent is not None:
                    dirs_to_prune.add(parent)
                if warn is not None:
                    _log(warn)

    # Prune empty directories left behind (deepest first).
    roots = set()
    if deploy_dir is not None:
        roots.add(deploy_dir)
    if game_root is not None:
        roots.add(game_root)
    for d in sorted(dirs_to_prune, key=lambda x: len(x.parts), reverse=True):
        if d in roots:
            continue
        try:
            d.rmdir()  # Only removes if empty
        except OSError:
            pass

    if removed:
        _log(f"  Undeployed {removed} file(s) for {len(mod_names)} mod(s).")
    return removed


__all__ = [
    "move_to_core",
    "deploy_filemap",
    "deploy_core",
    "restore_data_core",
    "undeploy_mod_files",
]
