"""
deploy_shared.py
Shared primitives used by the deploy_* mode modules.

Extracted from deploy.py during the 2026-04 refactor. No behaviour changes —
the original deploy.py re-exports everything here via `from ... import *`.
"""

from __future__ import annotations

import concurrent.futures
import errno
import os
import shutil
import time as _time
from contextlib import contextmanager as _contextmanager
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

from Utils.app_log import safe_log as _safe_log
from Utils.atomic_write import atomic_writer
from Utils.path_utils import has_path_traversal as _has_traversal


def _mkdir_leaves(dirs: "set[str]") -> None:
    """Create all directories in *dirs*, skipping any that are a prefix of
    another (mkdir -p on a deep leaf also creates every ancestor).

    Reduces os.makedirs() calls by dropping redundant parents before the
    stat-heavy exist_ok=True check runs on each one.
    """
    if not dirs:
        return
    # A dir is redundant if any of its ancestor-or-self chain members
    # (besides itself) is also in *dirs* as a deeper path — equivalently,
    # any ancestor of a deeper dir is redundant. Build the set of all
    # strict ancestors of every dir, then keep only dirs not in that set.
    redundant: set[str] = set()
    for d in dirs:
        # Walk up: every parent of d is an ancestor and thus not a leaf
        # (if that parent also appears in dirs).
        p = d.rsplit("/", 1)[0]
        while p and p != d:
            if p in dirs:
                redundant.add(p)
            nxt = p.rsplit("/", 1)[0]
            if nxt == p:
                break
            p = nxt
    for d in dirs:
        if d not in redundant:
            os.makedirs(d, exist_ok=True)


def _deploy_workers() -> int:
    """Thread-pool size for parallel file transfers. Override with
    MOD_MANAGER_DEPLOY_WORKERS for quick benchmarking."""
    try:
        n = int(os.environ.get("MOD_MANAGER_DEPLOY_WORKERS", "16"))
        return max(1, n)
    except ValueError:
        return 16


@_contextmanager
def _timer(label: str):
    """Print elapsed wall-clock time for a labelled block to stderr."""
    t0 = _time.perf_counter()
    yield
    dt = _time.perf_counter() - t0
    print(f"  [TIMER] {label}: {dt:.3f}s")


def load_per_mod_strip_prefixes(profile_dir: Path) -> dict[str, list[str]]:
    """Load per-mod strip prefixes from profile_state.json (falls back to legacy file)."""
    from Utils.profile_state import read_mod_strip_prefixes
    return read_mod_strip_prefixes(profile_dir)


def load_separator_deploy_paths(profile_dir: Path) -> dict[str, dict]:
    """Load separator deploy paths from profile_state.json (falls back to legacy file)."""
    from Utils.profile_state import read_separator_deploy_paths
    return read_separator_deploy_paths(profile_dir)


def expand_separator_deploy_paths(
    sep_paths: dict[str, dict],
    entries,
) -> dict[str, Path]:
    """Convert sep_paths → {mod_name: Path} using modlist order.

    Only mods whose separator has a non-empty path override are included.
    entries — list[ModEntry] from read_modlist()
    """
    result: dict[str, Path] = {}
    current_override: Path | None = None
    for entry in entries:
        if entry.is_separator:
            raw_path = sep_paths.get(entry.name, {}).get("path", "")
            current_override = Path(raw_path) if raw_path else None
        else:
            if current_override is not None:
                result[entry.name] = current_override
    return result


def expand_separator_raw_deploy(
    sep_paths: dict[str, dict],
    entries,
) -> set[str]:
    """Return the set of mod names whose separator has 'raw deploy' enabled.

    When raw deploy is on, deployment rules (routing, strip) are ignored and
    files are placed as-is relative to the custom deploy directory.
    entries — list[ModEntry] from read_modlist()
    """
    result: set[str] = set()
    current_raw = False
    for entry in entries:
        if entry.is_separator:
            info = sep_paths.get(entry.name, {})
            current_raw = bool(info.get("raw", False))
        else:
            if current_raw:
                result.add(entry.name)
    return result


def _default_filemap_for(profile_dir: "Path") -> "Path | None":
    """Locate filemap.txt for a profile without help from the game handler.

    Mirrors ``BaseGame.get_effective_filemap_path()``: profile-specific-mods
    profiles keep their filemap next to the profile (``<profile_dir>/filemap.txt``),
    shared-staging profiles use the one at the profile root
    (``<profile_dir>/../../filemap.txt``).  Returns the first one that exists,
    or None if neither does.
    """
    for candidate in (profile_dir / "filemap.txt",
                      profile_dir.parent.parent / "filemap.txt"):
        if candidate.is_file():
            return candidate
    return None


def _reconstruct_custom_deploy_list(
    profile_dir: "Path",
    entries,
    filemap_path: "Path | None",
    log_fn,
) -> list[tuple[str, str | None]]:
    """Recompute the list of deployed custom-location paths from the filemap.

    Used by cleanup when ``custom_deploy_log.txt`` is missing (older deploys,
    profile dir moved, manual deletion, etc.).  Walks the active filemap and
    expands every entry whose mod sits under a separator with a custom deploy
    path, producing ``(dst, src)`` pairs.  ``src`` is the staging-side source
    path (used to verify the destination genuinely came from us via inode
    comparison) or ``None`` if it can't be located.
    """
    fm = filemap_path if filemap_path is not None else _default_filemap_for(profile_dir)
    if fm is None or not fm.is_file():
        return []
    sep_paths = load_separator_deploy_paths(profile_dir)
    if not sep_paths:
        return []
    per_mod_deploy = expand_separator_deploy_paths(sep_paths, entries)
    if not per_mod_deploy:
        return []
    # Profile dir layout: profile_dir/mods/<mod_name>/...
    staging_root = profile_dir / "mods"
    result: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    _dir_listing_cache: dict[str, dict[str, str]] = {}
    _resolved_dir_cache: dict[str, str] = {}
    try:
        with fm.open(encoding="utf-8") as f:
            for line in f:
                if "\t" not in line:
                    continue
                rel_str, mod_name = line.rstrip("\n").split("\t", 1)
                if _has_traversal(rel_str) or _has_traversal(mod_name):
                    continue
                eff_dir = per_mod_deploy.get(mod_name)
                if eff_dir is None:
                    continue
                dst = _resolve_root_path_str(
                    str(eff_dir), rel_str, _dir_listing_cache,
                    resolved_dir_cache=_resolved_dir_cache,
                )
                src_candidate = str(staging_root / mod_name / rel_str)
                src: str | None = src_candidate if os.path.isfile(src_candidate) else None
                if dst not in seen:
                    seen.add(dst)
                    result.append((dst, src))
    except OSError as exc:
        log_fn(f"  WARN: could not read filemap for cleanup fallback: {exc}")
        return []
    if result:
        log_fn(f"  custom_deploy_log.txt missing — reconstructed {len(result)} entry/entries from filemap.")
    return result


def cleanup_custom_deploy_dirs(
    profile_dir: "Path | None",
    entries,
    log_fn=None,
    filemap_path: "Path | None" = None,
) -> int:
    """Remove files deployed to custom separator locations and restore originals.

    Reads custom_deploy_log.txt written by deploy_filemap() and deletes every
    file listed there, then restores any originals from custom_deploy_backup/.

    Returns the number of files removed.
    """
    _log = _safe_log(log_fn)

    if profile_dir is None:
        return 0

    # Auto-discover the filemap when the caller didn't supply one, so handlers
    # that haven't been updated still get a working fallback path.
    if filemap_path is None:
        filemap_path = _default_filemap_for(profile_dir)

    # Locate log: search profile_dir and two levels up (profile root)
    log_path: Path | None = None
    for candidate_dir in (profile_dir, profile_dir.parent.parent):
        c = candidate_dir / "custom_deploy_log.txt"
        if c.is_file():
            log_path = c
            break

    # Each entry: (absolute_dst, optional_staging_src). The src is only
    # populated by the filemap-fallback path and used to confirm a regular
    # file genuinely came from our staging (same inode) before deleting it.
    file_list: list[tuple[str, str | None]]
    fallback_mode: bool
    if log_path is not None:
        fallback_mode = False
        file_list = [(p, None) for p in log_path.read_text(encoding="utf-8").splitlines() if p]
        backup_dir = log_path.parent / "custom_deploy_backup"
    else:
        # No log on disk (e.g. deploy happened with an older build, log got
        # cleared mid-restore, or the profile dir moved). Fall back to
        # reconstructing the deployed file list from the current filemap +
        # separator deploy paths.
        fallback_mode = True
        file_list = _reconstruct_custom_deploy_list(
            profile_dir, entries, filemap_path, _log
        )
        backup_dir = profile_dir / "custom_deploy_backup"
        if not file_list and not backup_dir.is_dir():
            return 0

    removed = 0
    skipped_unknown = 0
    dirs_to_prune: set[Path] = set()
    # Seed stop_dirs with the user-configured custom deploy roots — those are
    # always-existing parents we must never remove. Subfolders we created
    # underneath them are fair game for empty-dir pruning so the destination
    # is left clean.
    stop_dirs: set[Path] = set()
    try:
        _sep_paths_for_stops = load_separator_deploy_paths(profile_dir)
        for _info in _sep_paths_for_stops.values():
            _p = _info.get("path", "") if isinstance(_info, dict) else ""
            if isinstance(_p, str) and _p:
                stop_dirs.add(Path(_p))
    except Exception:
        pass

    import stat as _stat
    for abs_str, src_str in file_list:
        # Entries are absolute filesystem paths by design — only reject ``..``
        # segments, not the leading ``/``.
        _norm = abs_str.replace("\\", "/")
        if ".." in _norm.split("/"):
            _log(f"  WARN: skipping suspicious path in custom_deploy_log: {abs_str!r}")
            continue
        target = Path(abs_str)
        try:
            tgt_stat = os.lstat(abs_str)
        except OSError:
            continue
        is_symlink = _stat.S_ISLNK(tgt_stat.st_mode)
        is_regular = _stat.S_ISREG(tgt_stat.st_mode)
        if not (is_symlink or is_regular):
            continue

        if fallback_mode and is_regular:
            # In fallback mode we have no proof this regular file came from
            # our deploy. Only delete if it shares an inode with the staging
            # source (i.e. it's a hardlink we created). Otherwise leave it
            # alone — it may be a pre-existing vanilla file that the original
            # deploy failed to back up.
            same_inode = False
            if src_str:
                try:
                    src_stat = os.stat(src_str)
                    same_inode = (
                        src_stat.st_dev == tgt_stat.st_dev
                        and src_stat.st_ino == tgt_stat.st_ino
                    )
                except OSError:
                    pass
            if not same_inode:
                skipped_unknown += 1
                _log(f"  Skipped (no backup, not a known link) — {target}")
                continue

        try:
            target.unlink()
            removed += 1
            dirs_to_prune.add(target.parent)
        except OSError as exc:
            _log(f"  WARN: could not remove custom-deployed {target}: {exc}")

    # Restore any originals that were backed up before deployment.  Backups
    # under custom_deploy_backup/ mirror absolute filesystem paths, so we
    # rebuild the destination from the filesystem root.
    restored = _restore_backup_dir(
        backup_dir, Path("/"), _log,
        check_traversal=False, swallow_errors=True,
    )

    _prune_empty_dirs(dirs_to_prune, stop_dirs)

    if log_path is not None:
        try:
            log_path.unlink()
        except OSError:
            pass

    if removed:
        _log(f"  Removed {removed} file(s) from custom deployment location(s).")
    if restored:
        _log(f"  Restored {restored} original file(s) to custom deployment location(s).")
    if skipped_unknown:
        _log(
            f"  Skipped {skipped_unknown} pre-existing file(s) at custom deploy "
            f"location(s) — no backup found, left untouched to avoid data loss."
        )
    return removed


def restore_custom_deploy_backup_for_path(
    filemap_path: "Path | None",
    custom_path: Path,
    log_fn=None,
) -> int:
    """Restore backed-up originals whose location is under custom_path.

    Called when a separator with a custom deploy location is removed while the
    game is still deployed — the backup files for that location must be put back
    immediately rather than waiting for the next full restore.

    Also removes the corresponding entries from custom_deploy_log.txt so that
    the full cleanup later does not try to delete the restored originals.

    Returns the number of files restored.
    """
    _log = _safe_log(log_fn)

    if filemap_path is None:
        return 0

    profile_dir = filemap_path.parent
    backup_dir  = profile_dir / "custom_deploy_backup"
    log_path    = profile_dir / "custom_deploy_log.txt"

    if not backup_dir.is_dir():
        return 0

    # The backup mirrors absolute paths: backup_dir / <abs-path-minus-anchor>
    # Files whose original location is under custom_path will be under:
    #   backup_dir / custom_path.relative_to(custom_path.anchor)
    try:
        backup_subtree = backup_dir / custom_path.relative_to(custom_path.anchor)
    except ValueError:
        return 0

    if not backup_subtree.is_dir():
        return 0

    restored = 0
    for bak_src in backup_subtree.rglob("*"):
        if not bak_src.is_file():
            continue
        rel = bak_src.relative_to(backup_dir)
        if any(part.endswith(_MOVE_TMP_SUFFIX) for part in rel.parts):
            continue  # interrupted-move partial — cleaned with the subtree below
        orig = Path("/") / rel
        try:
            _move_crash_safe(bak_src, orig)
            restored += 1
            _log(f"  Restored {orig.name} from custom_deploy_backup/")
        except OSError as exc:
            _log(f"  WARN: could not restore {orig}: {exc}")

    # Clean up the now-empty backup subtree.
    shutil.rmtree(backup_subtree, ignore_errors=True)

    # Remove entries for this path from the deploy log so full cleanup won't
    # try to delete the now-restored originals.
    if log_path.is_file() and restored:
        try:
            lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l]
            kept = [l for l in lines if not Path(l).is_relative_to(custom_path)]
            if len(kept) < len(lines):
                if kept:
                    log_path.write_text("\n".join(kept), encoding="utf-8")
                else:
                    log_path.unlink()
        except OSError:
            pass

    if restored:
        _log(f"  Restored {restored} original file(s) for removed separator.")
    return restored


def _prune_empty_dirs(dirs: "set[Path]", stop_dirs: "set[Path] | None" = None) -> None:
    """Remove empty directories bottom-up, stopping at (and never removing) stop_dirs."""
    _stop = stop_dirs or set()
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        current = d
        while current not in _stop:
            try:
                if current.is_dir() and not any(current.iterdir()):
                    current.rmdir()
                    current = current.parent
                else:
                    break
            except OSError:
                break


def _restore_backup_dir(
    backup_dir: Path,
    target_root: Path,
    log_fn=None,
    *,
    label: str | None = None,
    check_traversal: bool = True,
    swallow_errors: bool = False,
) -> int:
    """Move every file under *backup_dir* back into *target_root* (preserving
    relative path), then remove *backup_dir*.

    Used by every restore path that has a sibling backup directory holding
    pre-deployment originals (Root_Backup/, custom_rules_backup/,
    custom_deploy_backup/, …).

    Parameters
    ----------
    backup_dir
        Directory whose tree mirrors *target_root* (or absolute paths, if
        target_root is ``Path("/")``).  No-op if missing or empty.
    target_root
        Where files are moved back to.  Pass ``Path("/")`` for the
        custom-deploy case where backups mirror absolute filesystem paths.
    label
        Tag used in log messages (defaults to ``backup_dir.name``).  Passed
        through unchanged so callers can keep their existing wording.
    check_traversal
        When True (the default), skip any backup whose reconstructed
        destination would escape *target_root*.  Set False for the
        absolute-path case (custom_deploy_backup), where ``target_root`` is
        the root of the filesystem and the check is meaningless.
    swallow_errors
        When True, catch ``OSError`` per file and log a warning rather than
        propagating.  Used by the cleanup paths that must keep going even if
        a single file can't be restored.

    Returns the number of files restored.
    """
    _log = _safe_log(log_fn)
    if not backup_dir.is_dir():
        return 0

    tag = label if label is not None else f"{backup_dir.name}/"
    restored = 0
    _bak_files: list[Path] = []
    for _dp, _dns, _fns in os.walk(str(backup_dir)):
        # Interrupted cross-device moves leave *.mm_tmp partials — never
        # restore those; drop them so they don't survive into the next deploy.
        for _dn in [d for d in _dns if d.endswith(_MOVE_TMP_SUFFIX)]:
            _dns.remove(_dn)
            shutil.rmtree(os.path.join(_dp, _dn), ignore_errors=True)
        for _fn in _fns:
            if _fn.endswith(_MOVE_TMP_SUFFIX):
                _rm_any(os.path.join(_dp, _fn))
                continue
            _bak_files.append(Path(_dp) / _fn)
    for bak_src in _bak_files:
        rel = bak_src.relative_to(backup_dir)
        orig = target_root / rel
        if check_traversal and not _path_under_root(orig, target_root):
            _log(f"  SKIP: path traversal blocked — {rel}")
            continue
        try:
            _move_crash_safe(bak_src, orig)
            restored += 1
            _log(f"  Restored {rel} from {tag}")
        except OSError as exc:
            if not swallow_errors:
                raise
            _log(f"  WARN: could not restore {orig}: {exc}")

    shutil.rmtree(backup_dir, ignore_errors=True)
    return restored


class LinkMode(Enum):
    HARDLINK = auto()
    SYMLINK  = auto()
    COPY     = auto()


def expand_separator_merge_dirs(
    sep_paths: dict[str, dict],
    entries,
) -> set[str]:
    """Return the set of mod names whose separator has 'merge folders' enabled.

    When merge is on, the wholesale-folder-replace pass in deploy_filemap is
    skipped for that mod's top-level folders — files are still backed up and
    deployed individually, so file-level overwrites are still reversible.
    """
    result: set[str] = set()
    current_merge = False
    for entry in entries:
        if entry.is_separator:
            current_merge = bool(sep_paths.get(entry.name, {}).get("merge", False))
        else:
            if current_merge:
                result.add(entry.name)
    return result


def expand_separator_link_modes(
    sep_paths: dict[str, dict],
    entries,
) -> dict[str, LinkMode]:
    """Return {mod_name: LinkMode} for mods whose separator overrides the link mode.

    Mods whose separator has no `mode` key (or "default") are omitted, signalling
    "inherit the global deploy mode". Recognised values: "hardlink", "symlink";
    anything else inherits the global mode.
    """
    result: dict[str, LinkMode] = {}
    current_mode: LinkMode | None = None
    for entry in entries:
        if entry.is_separator:
            raw = sep_paths.get(entry.name, {}).get("mode", "")
            raw_l = (raw or "").strip().lower()
            if raw_l == "hardlink":
                current_mode = LinkMode.HARDLINK
            elif raw_l == "symlink":
                current_mode = LinkMode.SYMLINK
            else:
                current_mode = None
        else:
            if current_mode is not None:
                result[entry.name] = current_mode
    return result


@dataclass
class CustomRule:
    """A file-routing rule that sends matched files to a game-root-relative
    destination directory.

    Matching is by extension, leading folder name, or both (both must match).

    dest       — path relative to the game install root (e.g. "pak_mods", "")
    extensions — lowercase file extensions to match (e.g. [".pak"]).
                 Empty list means no extension filter.
    folders    — lowercase first-path-segment names to match (e.g. ["natives"]).
                 Empty list means no folder filter.
    loose_only — when True, the rule only matches files that are not inside
                 any folder (i.e. files at the mod root with no directory
                 components in their relative path).  Default False.
    companion_extensions — lowercase file extensions (e.g. [".ini"]) whose
                 owners ride along with a primary match.  When this rule
                 matches a file, any sibling in the same folder with the same
                 basename stem and one of these extensions is also routed to
                 the same destination.  Used for formats like RDR2's ASI
                 plugins where "Foo.asi" and "Foo.ini" must both live at the
                 game root even though the ``.ini`` extension is too generic
                 to route unconditionally.
    flatten    — when True, folder matches drop all directory components
                 below the matched folder so the file lands flat under
                 ``dest``.  Default False (preserve subfolders, the historical
                 behaviour).
    include_siblings — when True, a single match drags the *containing
                 folder* of the matched file along with it: every sibling
                 file under that folder (from the same mod) is routed to
                 ``dest`` too, preserving its path relative to the
                 containing folder.  ``flatten`` is ignored for these
                 matches; the containing folder's name is kept under
                 ``dest`` so files stay grouped.  Useful for mods like
                 ``PD2-AdvancedCrosshairs/mod.txt`` where matching
                 ``mod.txt`` should also bring the whole mod folder along.
    to_prefix  — when True, ``dest`` is resolved relative to the game's
                 Proton/Wine prefix root (the ``pfx/`` directory) instead
                 of the game install root.  Use for files that belong
                 inside the virtual Windows filesystem (e.g.
                 ``drive_c/users/steamuser/AppData/...``).  Requires the
                 caller to pass ``prefix_root`` to ``deploy_custom_rules``;
                 rules with ``to_prefix=True`` are skipped when no prefix
                 is available.
    mirror_dests — additional destination dirs (resolved under the same
                 base as ``dest``) that every matched file is also placed
                 into.  Used for Steam/GOG dual My Games folders where the
                 same save must land in both variants.
    exclude_extensions — lowercase file extensions this rule must NOT claim,
                 even when the folder/filename criteria match.  Used by BG3's
                 ``mods`` folder rule so loose files under Mods/ route to the
                 game Data dir while ``Mods/Foo.pak`` stays with the normal
                 deploy into the Larian AppData Mods folder.

    Placement behaviour:
    - extension-only match: file placed as game_root/dest/<filename> (flat)
    - folder match (with or without extension): file placed as
      game_root/dest/<original rel_path> (full path preserved)
    - filename match: file placed flat as game_root/dest/<filename>
    - companion match: file placed using the same rule as its primary owner
    """
    dest: str
    extensions: list[str] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)
    filenames: list[str] = field(default_factory=list)
    loose_only: bool = False
    companion_extensions: list[str] = field(default_factory=list)
    flatten: bool = False
    include_siblings: bool = False
    to_prefix: bool = False
    mirror_dests: list[str] = field(default_factory=list)
    exclude_extensions: list[str] = field(default_factory=list)


def _default_core(deploy_dir: Path) -> Path:
    """Return the default backup directory for deploy_dir."""
    return deploy_dir.parent / f"{deploy_dir.name}_Core"


# Errnos that mean "hardlink can't work here" rather than a real failure:
#   EXDEV  — src and dst on different filesystems (SD card, external drive)
#   EPERM  — filesystem doesn't support hardlinks (exFAT, some FUSE mounts)
#   ENOTSUP/EOPNOTSUPP — explicit "operation not supported" from the FS
#   EMLINK — link count exceeded (rare, but unrecoverable for hardlink)
# On any of these we fall back to symlink, then copy.
_HARDLINK_FALLBACK_ERRNOS = frozenset(
    e for e in (
        getattr(errno, "EXDEV", None),
        getattr(errno, "EPERM", None),
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "EMLINK", None),
    ) if e is not None
)

# Errnos that mean "symlink can't work here" rather than a real failure:
#   EPERM  — filesystem doesn't support symlinks (exFAT, FAT32, most CIFS)
#   ENOSYS/ENOTSUP/EOPNOTSUPP — explicit "operation not supported" from the FS
# On any of these we fall back to copy. EEXIST and friends still raise.
_SYMLINK_FALLBACK_ERRNOS = frozenset(
    e for e in (
        getattr(errno, "EPERM", None),
        getattr(errno, "ENOSYS", None),
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
    ) if e is not None
)

_hardlink_fallback_notified = False
_symlink_fallback_notified = False


def _notify_symlink_fallback(exc: OSError) -> None:
    """Emit a one-time stderr note when symlink → copy fallback kicks in."""
    global _symlink_fallback_notified
    if _symlink_fallback_notified:
        return
    _symlink_fallback_notified = True
    import sys
    sys.stderr.write(
        f"[deploy] symlink unsupported on this path ({exc.strerror or exc}); "
        f"falling back to copy. This usually means the game lives on a "
        f"filesystem without symlink support (exFAT/FAT32/SMB).\n"
    )


def _notify_hardlink_fallback(exc: OSError) -> None:
    """Emit a one-time stderr note when hardlink → symlink fallback kicks in.

    Printed to stderr (not the app log) so it's visible when debugging but
    doesn't spam the UI log per-file. Further fallbacks in the same session
    are silent.
    """
    global _hardlink_fallback_notified
    if _hardlink_fallback_notified:
        return
    _hardlink_fallback_notified = True
    import sys
    sys.stderr.write(
        f"[deploy] hardlink unsupported on this path ({exc.strerror or exc}); "
        f"falling back to symlink/copy. This usually means the game and mod "
        f"staging live on different filesystems.\n"
    )


def _transfer(src: Path, dst: Path, mode: LinkMode) -> None:
    """Transfer a single file from src to dst using the requested mode.

    If HARDLINK fails because src and dst are on different filesystems (or
    the filesystem doesn't support hardlinks), automatically fall back to
    symlink, then to copy. This lets users keep mods on one drive and the
    game on another without silently losing files.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if mode is LinkMode.HARDLINK:
        try:
            os.link(src, dst)
            return
        except OSError as exc:
            if exc.errno not in _HARDLINK_FALLBACK_ERRNOS:
                raise
            _notify_hardlink_fallback(exc)
        # Cross-FS or unsupported — try symlink, then copy.
        try:
            os.symlink(src, dst)
            return
        except OSError:
            shutil.copy2(src, dst)
            return
    if mode is LinkMode.SYMLINK:
        try:
            os.symlink(src, dst)
            return
        except OSError as exc:
            if exc.errno not in _SYMLINK_FALLBACK_ERRNOS:
                raise
            _notify_symlink_fallback(exc)
        shutil.copy2(src, dst)
    else:
        shutil.copy2(src, dst)


# Suffix for in-flight cross-device move targets. A crash mid-copy leaves
# only a *.mm_tmp leftover, never a partial file/folder at the real path —
# restore walkers skip and clean these.
_MOVE_TMP_SUFFIX = ".mm_tmp"


def _move_crash_safe(src: "Path | str", dst: "Path | str") -> None:
    """Move a file or directory, surviving interruption across devices.

    Same-device moves are a single atomic rename. Cross-device (EXDEV), the
    data is copied to a sibling ``<name>.mm_tmp`` first, renamed into place,
    and only then is the source deleted — shutil.move's copy-then-delete can
    leave a partial copy at the destination that a later restore would trust.
    """
    src_str, dst_str = str(src), str(dst)
    os.makedirs(os.path.dirname(dst_str), exist_ok=True)
    try:
        os.rename(src_str, dst_str)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    tmp = dst_str + _MOVE_TMP_SUFFIX
    _rm_any(tmp)
    src_st = os.lstat(src_str)
    import stat as _stat_m
    if _stat_m.S_ISDIR(src_st.st_mode):
        shutil.copytree(src_str, tmp, symlinks=True)
        os.rename(tmp, dst_str)
        shutil.rmtree(src_str)
    else:
        shutil.copy2(src_str, tmp, follow_symlinks=False)
        os.replace(tmp, dst_str)
        os.unlink(src_str)


def _rm_any(path: str) -> None:
    """Remove a leftover file, symlink, or directory tree; missing is fine."""
    try:
        st = os.lstat(path)
    except OSError:
        return
    import stat as _stat_m
    if _stat_m.S_ISDIR(st.st_mode):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.unlink(path)
        except OSError:
            pass


def _clear_dir(directory: Path) -> int:
    """Delete all files inside directory and remove empty subdirectories.
    Returns the number of files deleted.  The directory itself is kept.

    Uses shutil.rmtree + recreate rather than per-file unlink — significantly
    faster for large directories (avoids repeated stat/unlink/rmdir syscalls).
    """
    if not directory.is_dir():
        return 0
    # os.walk classifies entries via readdir d_type — no stat per entry like
    # rglob + is_file.
    count = sum(len(fns) for _dp, _dns, fns in os.walk(str(directory)))
    if count == 0:
        return 0
    shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    return count


_OVERWRITE_NAME = "[Overwrite]"


def _resolve_source(
    mod_name: str,
    rel_str: str,
    rel_lower: str,
    overwrite_dir: Path,
    staging_root: Path,
    overwrite_str: str,
    staging_str: str,
    sorted_strip: list[str],
    per_mod_strip: dict[str, list[str]],
    nocase_cache: dict,
    mod_index_cache: "dict[Path, dict[str, Path]] | None" = None,
) -> str | None:
    """Resolve the on-disk source path for a filemap entry.

    Returns the source path as a string, or None if not found.
    Tries (in order): direct stat, mod-index O(1) lookup, case-insensitive
    walk, strip-prefix combinations, per-mod strip prefixes.
    """
    _isfile = os.path.isfile

    # Fast path: direct string join + stat
    if mod_name == _OVERWRITE_NAME:
        candidate = overwrite_str + "/" + rel_str
    else:
        candidate = staging_str + "/" + mod_name + "/" + rel_str
    if _isfile(candidate):
        return candidate

    # Slow path
    mod_root = overwrite_dir if mod_name == _OVERWRITE_NAME else staging_root / mod_name
    src: Path | None = None

    if mod_index_cache is not None:
        if mod_root not in mod_index_cache:
            mod_index_cache[mod_root] = _build_mod_index(mod_root)
        src = mod_index_cache[mod_root].get(rel_lower)

    if src is None:
        src = _resolve_nocase(mod_root, rel_str, cache=nocase_cache)

    if src is None and sorted_strip:
        for p1 in sorted_strip:
            src = _resolve_nocase(mod_root, p1 + "/" + rel_str, cache=nocase_cache)
            if src is not None:
                break
            for p2 in sorted_strip:
                src = _resolve_nocase(mod_root, p1 + "/" + p2 + "/" + rel_str, cache=nocase_cache)
                if src is not None:
                    break
            if src is not None:
                break

    if src is None and mod_name != _OVERWRITE_NAME:
        mod_strip = per_mod_strip.get(mod_name)
        if mod_strip:
            path_prefixes = [p for p in mod_strip if "/" in p]
            for p in path_prefixes:
                src = _resolve_nocase(mod_root, p + "/" + rel_str, cache=nocase_cache)
                if src is not None:
                    break
            if src is None:
                segment_list = [p for p in mod_strip if "/" not in p]
                prefix_path = ""
                for seg in segment_list:
                    prefix_path = prefix_path + seg + "/" if prefix_path else seg + "/"
                    src = _resolve_nocase(mod_root, prefix_path + rel_str, cache=nocase_cache)
                    if src is not None:
                        break

    return str(src) if src is not None else None


def _do_link(src: str, dst: str, mode: LinkMode) -> OSError | None:
    """Transfer a single file. Returns None on success, or the OSError.

    HARDLINK auto-falls-back to symlink then copy when the filesystem
    refuses the hardlink (EXDEV for cross-device, EPERM/ENOTSUP for FS
    types like exFAT that don't support hardlinks). Without this, users
    whose game is on a different drive from their mod staging would see
    per-file WARN lines and silently broken deployments.
    """
    return _do_link_ex(src, dst, mode)[1]


def _do_link_ex(src: str, dst: str, mode: LinkMode) -> tuple["LinkMode | None", OSError | None]:
    """Like _do_link but also reports the mode that actually succeeded.

    Returns (effective_mode, None) on success — effective_mode reflects the
    real transfer used after any hardlink → symlink → copy fallback, so the
    caller can report a per-mode breakdown. Returns (None, OSError) on failure.
    """
    try:
        if mode is LinkMode.HARDLINK:
            try:
                os.link(src, dst)
                return LinkMode.HARDLINK, None
            except OSError as exc:
                if exc.errno not in _HARDLINK_FALLBACK_ERRNOS:
                    return None, exc
                _notify_hardlink_fallback(exc)
            try:
                os.symlink(src, dst)
                return LinkMode.SYMLINK, None
            except OSError:
                shutil.copy2(src, dst)
                return LinkMode.COPY, None
        if mode is LinkMode.SYMLINK:
            try:
                os.symlink(src, dst)
                return LinkMode.SYMLINK, None
            except OSError as exc:
                if exc.errno not in _SYMLINK_FALLBACK_ERRNOS:
                    return None, exc
                _notify_symlink_fallback(exc)
            shutil.copy2(src, dst)
            return LinkMode.COPY, None
        shutil.copy2(src, dst)
        return mode, None
    except OSError as e:
        return None, e


def _restore_from_log(
    log_path: Path,
    target_root: Path,
    backup_dir: "Path | None",
    log_fn,
    *,
    prune_dirs: bool = True,
) -> int:
    """Shared restore logic: read log, delete placed files, restore backups.

    log_path    — file listing relative paths (one per line) that were deployed
    target_root — directory the files were deployed into
    backup_dir  — directory holding backed-up originals (or None)
    prune_dirs  — if True, remove empty directories left behind

    Returns the number of files removed from target_root.
    """
    _log = _safe_log(log_fn)

    if not log_path.is_file():
        return 0

    placed = [p for p in log_path.read_text(encoding="utf-8").splitlines() if p]
    removed = 0

    # Pre-filter for path traversal (cheap, serial) so the worker pool only
    # does syscalls.  lstat + S_ISLNK/S_ISREG check + unlink in one worker call
    # keeps each task to two syscalls; previously is_file()+is_symlink()+unlink
    # was three.
    import stat as _stat
    _target_str = str(target_root)
    safe_targets: list[str] = []
    for rel_str in placed:
        dst = target_root / rel_str
        if not _path_under_root(dst, target_root):
            _log(f"  SKIP: path traversal blocked — {rel_str}")
            continue
        safe_targets.append(_target_str + "/" + rel_str)

    def _unlink_one(p: str) -> int:
        try:
            st = os.lstat(p)
        except OSError:
            return 0
        if _stat.S_ISLNK(st.st_mode) or _stat.S_ISREG(st.st_mode):
            try:
                os.unlink(p)
                return 1
            except OSError:
                return 0
        return 0

    if safe_targets:
        with concurrent.futures.ThreadPoolExecutor(max_workers=_deploy_workers()) as pool:
            for n in pool.map(_unlink_one, safe_targets):
                removed += n

    # Restore backed-up originals.
    if backup_dir is not None:
        _restore_backup_dir(backup_dir, target_root, _log)

    log_path.unlink()

    # Prune empty directories left behind.
    if prune_dirs:
        dirs_to_check: set[Path] = set()
        for rel_str in placed:
            p = (target_root / rel_str).parent
            while p != target_root and p != target_root.parent:
                dirs_to_check.add(p)
                p = p.parent
        for d in sorted(dirs_to_check, key=lambda x: len(x.parts), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass

    return removed


def _wrapper_chains(
    mod_root_str: str,
    strip_set: "set[str]",
    max_depth: int = 3,
) -> "list[tuple[str, set[str]]]":
    """Discover wrapper-folder chains that filemap's scan would have stripped.

    Returns [(chain_rel, names_lower), ...] where the first entry is always
    ("", <entry names at mod root>) and each further entry is a wrapper chain
    like "Data" or "Data/oblivion" (actual on-disk casing) with the lowercase
    entry names directly inside it.  A couple of scandir calls per mod — no
    tree walk.
    """
    out: list[tuple[str, set[str]]] = []
    stack: list[tuple[str, str, int]] = [(mod_root_str, "", 0)]
    while stack:
        dir_str, chain, depth = stack.pop()
        names: set[str] = set()
        try:
            with os.scandir(dir_str) as it:
                for e in it:
                    nl = e.name.lower()
                    names.add(nl)
                    if depth < max_depth and nl in strip_set:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                stack.append((
                                    e.path,
                                    (chain + "/" + e.name) if chain else e.name,
                                    depth + 1,
                                ))
                        except OSError:
                            pass
        except OSError:
            pass
        out.append((chain, names))
    return out


def _prebuild_mod_indexes(
    tab_lines: list[str],
    overwrite_dir: Path,
    staging_root: Path,
    mod_index_cache: dict,
    *,
    profile_dir: "Path | None" = None,
    strip_prefixes: "set[str] | None" = None,
    per_mod_strip_prefixes: "dict[str, list[str]] | None" = None,
) -> None:
    """Pre-build per-mod file indexes for all mods referenced in the filemap.

    Fast path: synthesize on-disk paths from Profiles/<game>/modindex.bin
    (already built by filemap.py) — no filesystem walk.  The index stores
    *stripped* rel paths, so when strip prefixes are in play the actual file
    may sit behind a wrapper folder (e.g. Data/); those wrapper chains are
    rediscovered with a couple of scandir calls per mod and each entry is
    mapped back to its physical location by checking its first path segment
    against the cached directory listings.

    Slow path: os.walk each mod folder (index missing/stale, or per-mod
    *path*-style strip prefixes whose semantics we don't mirror here).
    Misses in a synthesized index fall back to _resolve_source per file, so
    the fast path is always safe.
    """
    mod_names: set[str] = set()
    for ln in tab_lines:
        tab_pos = ln.find("\t")
        if tab_pos > 0:
            mod_names.add(ln[tab_pos + 1:])

    index_from_disk: dict | None = None
    if profile_dir is not None:
        try:
            from Utils.filemap import read_mod_index
            index_from_disk = read_mod_index(profile_dir / "modindex.bin")
        except Exception:
            index_from_disk = None

    _global_strip = {s.lower() for s in strip_prefixes} if strip_prefixes else set()
    _per_mod = per_mod_strip_prefixes or {}
    _isfile = os.path.isfile
    walk_targets: list[Path] = []

    for mn in mod_names:
        if _has_traversal(mn):
            continue
        mr = overwrite_dir if mn == _OVERWRITE_NAME else staging_root / mn
        if mr in mod_index_cache:
            continue

        entry = index_from_disk.get(mn) if index_from_disk is not None else None
        per_mod_list = _per_mod.get(mn) or []

        if entry is None or any("/" in p for p in per_mod_list):
            walk_targets.append(mr)
            continue

        normal, root = entry
        mr_str = str(mr)
        strip_set = _global_strip | {s.lower() for s in per_mod_list}
        built: dict[str, str] = {}

        chains = _wrapper_chains(mr_str, strip_set) if strip_set else []
        if len(chains) <= 1:
            # No wrapper folders on disk — nothing was stripped for this mod,
            # so the index rel paths are the on-disk paths.
            for rel_lower, rel_str in normal.items():
                built[rel_lower] = mr_str + "/" + rel_str
            for rel_lower, rel_str in root.items():
                built[rel_lower] = mr_str + "/" + rel_str
            mod_index_cache[mr] = built
            continue

        for src_map in (normal, root):
            for rel_lower, rel_str in src_map.items():
                slash = rel_str.find("/")
                seg = (rel_str[:slash] if slash > 0 else rel_str).lower()
                hits = [chain for chain, names in chains if seg in names]
                if not hits:
                    continue  # stale entry — per-file fallback handles it
                if len(hits) == 1:
                    chain = hits[0]
                    built[rel_lower] = (
                        mr_str + "/" + chain + "/" + rel_str if chain
                        else mr_str + "/" + rel_str
                    )
                    continue
                # Same first segment exists at multiple wrapper levels —
                # verify which physical file is real.
                for chain in hits:
                    cand = (
                        mr_str + "/" + chain + "/" + rel_str if chain
                        else mr_str + "/" + rel_str
                    )
                    if _isfile(cand):
                        built[rel_lower] = cand
                        break
        mod_index_cache[mr] = built

    if not walk_targets:
        return
    if len(walk_targets) == 1:
        mod_index_cache[walk_targets[0]] = _build_mod_index(walk_targets[0])
        return
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(_deploy_workers(), len(walk_targets))
    ) as pool:
        for mr, built_idx in zip(walk_targets, pool.map(_build_mod_index, walk_targets)):
            mod_index_cache[mr] = built_idx


def _resolve_root_path(base: Path, rel: Path,
                       dir_cache: "dict[Path, dict[str, str]] | None" = None,
                       core_base: "Path | None" = None) -> Path:
    """Resolve *rel* under *base*, matching existing directory names
    case-insensitively so that mod folders (e.g. ``R6/``) merge into whatever
    casing the game already has on disk (e.g. ``r6/``).  Segments that don't
    yet exist use the casing from the filemap.

    Only directory segments are normalised; the final filename is kept as-is.
    dir_cache maps parent Path → {lower_name: actual_name} to avoid repeated
    iterdir() calls across many files with the same directory structure.

    core_base — optional sibling backup dir (e.g. Data_Core/) consulted when
    a segment isn't found in *base*.  This preserves vanilla folder casing
    (e.g. ``Scripts/``) even when *base* is empty at deploy time.
    """
    current = base
    core_current = core_base
    parts = rel.parts
    for part in parts[:-1]:   # directory segments only
        part_lower = part.lower()
        # Check if a directory with this name (any case) already exists.
        matched: str | None = None
        if dir_cache is not None:
            if current not in dir_cache:
                try:
                    if current.is_dir():
                        dir_cache[current] = {
                            e.name.lower(): e.name
                            for e in current.iterdir()
                            if e.is_dir()
                        }
                    else:
                        dir_cache[current] = {}
                except OSError:
                    dir_cache[current] = {}
            matched = dir_cache[current].get(part_lower)
        else:
            if current.is_dir():
                try:
                    for entry in current.iterdir():
                        if entry.is_dir() and entry.name.lower() == part_lower:
                            matched = entry.name
                            break
                except OSError:
                    pass
        # Fall back to core_base to preserve vanilla folder casing
        # (e.g. Data_Core/Scripts → use "Scripts" not "scripts").
        if matched is None and core_current is not None:
            if dir_cache is not None:
                if core_current not in dir_cache:
                    try:
                        if core_current.is_dir():
                            dir_cache[core_current] = {
                                e.name.lower(): e.name
                                for e in core_current.iterdir()
                                if e.is_dir()
                            }
                        else:
                            dir_cache[core_current] = {}
                    except OSError:
                        dir_cache[core_current] = {}
                matched = dir_cache[core_current].get(part_lower)
            else:
                try:
                    for entry in core_current.iterdir():
                        if entry.is_dir() and entry.name.lower() == part_lower:
                            matched = entry.name
                            break
                except OSError:
                    pass
        chosen = matched if matched is not None else part
        current = current / chosen
        core_current = (core_current / chosen) if core_current is not None else None
    return current / parts[-1]


def _resolve_root_path_str(base_str: str, rel_str: str,
                           dir_listing_cache: "dict[str, dict[str, str]]",
                           core_base_str: "str | None" = None,
                           resolved_dir_cache: "dict[str, str] | None" = None) -> str:
    """Fast string-based variant of _resolve_root_path for bulk deploy.

    Instead of creating Path objects per call, works entirely with strings
    and caches the fully-resolved directory path so files sharing the same
    parent directory skip all resolution after the first.

    dir_listing_cache — maps dir_path_str → {lower_name: actual_name}
    resolved_dir_cache — maps (base_str + "\\0" + dir_parts_lower) → resolved_dir_str.
    The base is part of the key so one cache can safely serve resolutions
    under several roots (deploy dir, per-separator custom dirs, game root).
    """
    # Split rel_str into directory part and filename
    slash_pos = rel_str.rfind("/")
    if slash_pos < 0:
        # No directory component — file directly under base
        return base_str + "/" + rel_str

    dir_part = rel_str[:slash_pos]
    filename = rel_str[slash_pos + 1:]
    dir_lower = dir_part.lower()

    # Check resolved dir cache first — covers the common case where many
    # files share the same directory.
    cache_key = base_str + "\x00" + dir_lower
    if resolved_dir_cache is not None:
        cached = resolved_dir_cache.get(cache_key)
        if cached is not None:
            return cached + "/" + filename

    # Walk each directory segment, resolving case
    parts = dir_part.split("/")
    current = base_str
    core_current = core_base_str
    _isdir = os.path.isdir
    _scandir = os.scandir

    for part in parts:
        part_lower = part.lower()
        matched: str | None = None

        listing = dir_listing_cache.get(current)
        if listing is None:
            listing = {}
            if _isdir(current):
                try:
                    with _scandir(current) as it:
                        for e in it:
                            if e.is_dir(follow_symlinks=False):
                                listing[e.name.lower()] = e.name
                except OSError:
                    pass
            dir_listing_cache[current] = listing
        matched = listing.get(part_lower)

        if matched is None and core_current is not None:
            core_listing = dir_listing_cache.get(core_current)
            if core_listing is None:
                core_listing = {}
                if _isdir(core_current):
                    try:
                        with _scandir(core_current) as it:
                            for e in it:
                                if e.is_dir(follow_symlinks=False):
                                    core_listing[e.name.lower()] = e.name
                    except OSError:
                        pass
                dir_listing_cache[core_current] = core_listing
            matched = core_listing.get(part_lower)

        chosen = matched if matched is not None else part
        current = current + "/" + chosen
        core_current = (core_current + "/" + chosen) if core_current is not None else None

    if resolved_dir_cache is not None:
        resolved_dir_cache[cache_key] = current

    return current + "/" + filename


# ---------------------------------------------------------------------------
# Snapshot helpers (shared by root and game-root modes)
# ---------------------------------------------------------------------------

# Snapshot of the game root written at deploy time; consumed by restore to
# identify runtime-generated files (files that appeared after deploy).
_FILEMAP_SNAPSHOT_NAME = "deploy_snapshot.txt"


def _normalize_exclude_dirs(exclude_dirs):
    """Return a set of lowercased, forward-slash relative dir paths, or None.

    Accepts plain top-level names ("Data") or nested paths ("BepInEx/plugins").
    """
    if not exclude_dirs:
        return None
    return {d.replace("\\", "/").strip("/").lower() for d in exclude_dirs}


def _write_deploy_snapshot(
    game_root: Path,
    snapshot_path: Path,
    log_fn=None,
    exclude_dirs=None,
) -> int:
    """Walk game_root and record every file's relative path, one per line.

    Written atomically via a .tmp sibling then renamed.  Returns the number
    of files recorded, or 0 on error (the deploy is never aborted).

    The format is one rel_path per line.  Older snapshots also recorded
    `\\tmtime_ns\\tsize` columns; _load_deploy_snapshot ignores anything past
    the first tab, so the trailing columns were dead cost (one extra stat
    per file) and have been dropped.  Old snapshots remain readable.

    Symlinks are recorded too (not just regular files): a deploy places mod
    files as symlinks, and on restore those paths are handed back to a vanilla
    file (e.g. a custom-rules-routed vanilla restored from custom_rules_backup/).
    If the deployed symlink path were omitted here, _move_runtime_files would
    see the restored vanilla as a brand-new file and wrongly sweep it into
    overwrite/.  Recording symlinks keeps every deploy-time path "known".

    exclude_dirs — dir paths (case-insensitive, relative to game_root) to skip;
    standard games pass their deploy subfolder so its files stay on the
    Data_Core path.  Nested paths like "BepInEx/plugins" are supported.
    """
    _log = _safe_log(log_fn)
    excluded = _normalize_exclude_dirs(exclude_dirs)
    count = 0
    game_root_str = str(game_root)
    prefix_len = len(game_root_str) + 1          # +1 for trailing separator
    try:
        with atomic_writer(snapshot_path, "w") as fh:
            fh.write("# deploy_snapshot v2\n")
            stack = [game_root_str]
            while stack:
                cur = stack.pop()
                try:
                    with os.scandir(cur) as it:
                        for entry in it:
                            if entry.is_dir(follow_symlinks=False):
                                if (excluded is not None
                                        and entry.path[prefix_len:].replace(
                                            "\\", "/").lower() in excluded):
                                    continue
                                stack.append(entry.path)
                            elif (entry.is_file(follow_symlinks=False)
                                  or entry.is_symlink()):
                                fh.write(entry.path[prefix_len:])
                                fh.write("\n")
                                count += 1
                except OSError:
                    pass
        _log(f"  Snapshot: recorded {count} files in game root.")
    except OSError as exc:
        _log(f"  WARN: could not write deploy snapshot: {exc}")
        return 0
    return count


def _load_deploy_snapshot(snapshot_path: Path) -> set[str]:
    """Return a set of lowercased relative paths from a deploy snapshot file.

    Returns an empty set if the file is missing or unreadable — callers treat
    this as "no snapshot available" and skip runtime-file detection.
    """
    if not snapshot_path.is_file():
        return set()
    try:
        known: set[str] = set()
        with snapshot_path.open(encoding="utf-8") as fh:
            for line in fh:
                if line[0] == "#":
                    continue
                tab = line.find("\t")
                known.add(line[:tab].lower() if tab != -1 else line.rstrip("\n").lower())
        return known
    except OSError:
        return set()


# Per-restore log written at the destination root (overwrite/ or Root_Folder/).
# Excluded from the filemap scan via filemap._EXCLUDE_NAMES.
OVERWRITE_LOG_NAME = ".mm_overwrite_log.txt"
_OVERWRITE_LOG_MAX_SECTIONS = 200


def _append_overwrite_log(dest_dir: Path, rels: "list[str]", log_fn=None) -> None:
    """Append one timestamped section listing *rels* to dest_dir's overwrite log.

    Best-effort: OSErrors are swallowed so logging can never break a restore."""
    log_path = dest_dir / OVERWRITE_LOG_NAME
    ts = _time.strftime("%Y-%m-%d %H:%M:%S")
    header = f"# {ts} — {len(rels)} file(s) moved on restore"
    section = "\n".join([header, *sorted(rels)]) + "\n\n"
    try:
        try:
            existing = log_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        combined = existing + section
        headers = [i for i, ln in enumerate(combined.splitlines())
                   if ln.startswith("# ")]
        if len(headers) > _OVERWRITE_LOG_MAX_SECTIONS:
            lines = combined.splitlines()
            cut = headers[len(headers) - _OVERWRITE_LOG_MAX_SECTIONS]
            combined = "\n".join(lines[cut:]) + "\n"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(combined, encoding="utf-8")
    except OSError as e:
        if log_fn is not None:
            log_fn(f"  WARN: could not write overwrite log: {e}")


def _move_runtime_files(
    game_root: Path,
    snapshot_path: Path,
    dest_dir: Path,
    log_fn=None,
    exclude_dirs=None,
) -> int:
    """Move files that appeared after deploy (runtime-generated) to dest_dir.

    Compares the current game_root contents against the deploy snapshot.
    Files present now but absent from the snapshot are moved to dest_dir
    preserving their relative path.  dest_dir is usually the [Overwrite] mod
    folder, but standard-deployed games pass Root_Folder/ so the captured
    files re-deploy to the game root next time instead of the data folder.
    Vanilla files (present in snapshot) are left untouched.
    Symlinks are skipped entirely.

    exclude_dirs — must match the value passed to _write_deploy_snapshot so the
    excluded subtree is never treated as runtime-generated.

    Safety net: if the on-disk game root barely overlaps the snapshot (the
    deploy path or sub-folder setting was changed while deployed, so restore
    is walking a different directory than the snapshot describes), the move is
    skipped entirely rather than wiping the whole folder into dest_dir.

    Returns the number of files moved.
    """
    _log = _safe_log(log_fn)
    known = _load_deploy_snapshot(snapshot_path)
    if not known:
        _log("  WARN: deploy snapshot empty or unreadable — skipping runtime file detection.")
        return 0

    excluded = _normalize_exclude_dirs(exclude_dirs)
    game_root_str = str(game_root)
    prefix_len = len(game_root_str) + 1
    overwrite_str = str(dest_dir)

    # Safety net: a deploy snapshot is taken of the game root resolved at deploy
    # time.  If the game-path or sub-folder setting was changed while deployed,
    # restore walks a *different* directory than the snapshot describes, so
    # almost nothing matches `known` and every vanilla file would be wrongly
    # classed as runtime-generated and moved to overwrite/ (wiping the game
    # folder).  Compare the on-disk files against the snapshot first: if the
    # overlap is implausibly low for a non-empty game root, the snapshot does
    # not describe this directory — bail out rather than destroy the install.
    candidate_rels: list[str] = []
    matched_known = 0
    stack = [game_root_str]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        if (excluded is not None
                                and entry.path[prefix_len:].replace(
                                    "\\", "/").lower() in excluded):
                            continue
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        rel = entry.path[prefix_len:]
                        if rel.lower() in known:
                            matched_known += 1
                        else:
                            candidate_rels.append(rel)
        except OSError:
            pass

    total_files = matched_known + len(candidate_rels)
    # Require at least a 10% overlap with the snapshot before trusting it.  A
    # genuine restore leaves nearly every vanilla file matching the snapshot
    # (only a handful of runtime files differ), so real overlap is ~100%; a
    # mismatched directory sits near 0%.  The 10% floor and the 20-file minimum
    # avoid false alarms on tiny game roots while still catching the wholesale
    # mismatch that the path-change bug produces.
    _MIN_FILES_FOR_CHECK = 20
    _MIN_OVERLAP = 0.10
    if (total_files >= _MIN_FILES_FOR_CHECK
            and matched_known / total_files < _MIN_OVERLAP):
        _log(
            "  WARN: deploy snapshot does not match the current game folder "
            f"({matched_known}/{total_files} files known) — the game path or "
            "sub-folder setting may have changed since deploy. Skipping "
            "runtime-file move to avoid wiping the game folder into overwrite/. "
            "Restore from a matching configuration if mod files remain."
        )
        return 0

    made_dirs: set[str] = set()
    emptied_dirs: set[Path] = set()
    moved_rels: list[str] = []
    moved = 0
    for rel in candidate_rels:
        src_path = game_root_str + "/" + rel
        dst = overwrite_str + "/" + rel
        if os.path.exists(dst):
            _log(f"  WARN: overwrite/{rel} already exists — skipping.")
            continue
        dst_dir = os.path.dirname(dst)
        if dst_dir not in made_dirs:
            os.makedirs(dst_dir, exist_ok=True)
            made_dirs.add(dst_dir)
        try:
            _move_crash_safe(src_path, dst)
        except OSError:
            continue
        emptied_dirs.add(Path(src_path).parent)
        moved_rels.append(rel)
        moved += 1

    # Prune any directories left empty after moving runtime files out
    if emptied_dirs:
        _prune_empty_dirs(emptied_dirs, stop_dirs={game_root})

    if moved_rels:
        _append_overwrite_log(dest_dir, moved_rels, _log)

    return moved


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _path_under_root(path: Path, root: Path) -> bool:
    """Return True if path is under root (no path traversal).

    Checks the unresolved path first so that symlinks whose targets live
    outside root (e.g. symlinks into staging) are not incorrectly blocked.
    """
    try:
        path.relative_to(root)
        return True
    except ValueError:
        pass
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _get_staging_source_path(
    mod_root: Path,
    rel_str: str,
    strip_prefixes: set[str],
    index_cache: "dict[Path, dict] | None" = None,
) -> Path | None:
    """Return the path of the given file in the mod staging folder, or None if absent.

    Tries rel_str directly, then strip_prefix/rel_str for each prefix (e.g.
    mods/ModName/Data/Plugin.esp when rel_str is Plugin.esp and strip has "data").
    index_cache, when given, memoizes the per-mod file index across calls —
    building it walks the whole mod folder, far too expensive to repeat per file.
    """
    if not mod_root.is_dir():
        return None
    rel_lower = rel_str.lower()
    if index_cache is not None:
        idx = index_cache.get(mod_root)
        if idx is None:
            idx = _build_mod_index(mod_root)
            index_cache[mod_root] = idx
    else:
        idx = _build_mod_index(mod_root)
    hit = idx.get(rel_lower)
    if hit is None:
        for prefix in sorted(strip_prefixes):
            candidate = (prefix + "/" + rel_str).lower()
            hit = idx.get(candidate)
            if hit is not None:
                break
            for prefix2 in strip_prefixes:
                if prefix2 == prefix:
                    continue
                candidate2 = (prefix + "/" + prefix2 + "/" + rel_str).lower()
                hit = idx.get(candidate2)
                if hit is not None:
                    break
            if hit is not None:
                break
    return Path(hit) if hit is not None else None


def _build_mod_index(mod_root: Path) -> "dict[str, str | Path]":
    """Build a case-insensitive index of all files under mod_root.

    Returns dict mapping lowercase rel_path -> full path (str) for O(1) lookup.
    Uses os.walk to avoid stat() per file (walk separates dirs from files).
    """
    out: dict[str, str | Path] = {}
    _root_str = str(mod_root)
    _root_plen = len(_root_str) + 1  # +1 for the trailing "/"
    try:
        for dirpath, _dirnames, filenames in os.walk(_root_str):
            for name in filenames:
                full_str = dirpath + "/" + name
                rel_lower = full_str[_root_plen:].lower()
                out[rel_lower] = full_str
    except OSError:
        pass
    return out


def _resolve_nocase(root: Path, rel_str: str,
                    cache: dict[Path, dict[str, list[Path]]] | None = None) -> Path | None:
    """Resolve a relative path case-insensitively under root.

    Each path segment is matched case-insensitively against the real
    filesystem entries so that a canonical rel_str (e.g. "Scripts/foo.pex")
    will find the actual file even if the mod folder uses "scripts/foo.pex".

    When a directory contains multiple entries whose names differ only in case
    (e.g. both "Textures/" and "textures/"), *all* are explored so the correct
    file is found regardless of which casing the filemap recorded.

    An optional *cache* dict maps directory Paths to {lowercase_name: [entries]}
    dicts so that repeated lookups in the same directory avoid re-scanning.

    Returns the resolved Path if it exists, or None.
    """
    if cache is None:
        cache = {}
    parts = rel_str.replace("\\", "/").split("/")
    # Stack entries: (current_dir, parts_index)
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, idx = stack.pop()
        if idx == len(parts):
            if current.is_file():
                return current
            continue
        part_lower = parts[idx].lower()
        listing = cache.get(current)
        if listing is None:
            listing = {}
            try:
                for e in current.iterdir():
                    key = e.name.lower()
                    if key not in listing:
                        listing[key] = []
                    listing[key].append(e)
            except OSError:
                pass
            cache[current] = listing
        candidates = listing.get(part_lower)
        if not candidates:
            continue
        for candidate in candidates:
            # Skip directory symlinks to prevent traversal outside root
            if idx + 1 < len(parts) and candidate.is_symlink():
                continue
            stack.append((candidate, idx + 1))
    return None


__all__ = [
    # Public classes/enums
    "LinkMode",
    "CustomRule",
    # Public helpers
    "load_per_mod_strip_prefixes",
    "load_separator_deploy_paths",
    "expand_separator_deploy_paths",
    "expand_separator_raw_deploy",
    "expand_separator_link_modes",
    "expand_separator_merge_dirs",
    "cleanup_custom_deploy_dirs",
    "restore_custom_deploy_backup_for_path",
    # Private helpers (re-exported via façade for back-compat)
    "_mkdir_leaves",
    "_deploy_workers",
    "_timer",
    "_prune_empty_dirs",
    "_restore_backup_dir",
    "_default_core",
    "_transfer",
    "_clear_dir",
    "_OVERWRITE_NAME",
    "_resolve_source",
    "_do_link",
    "_do_link_ex",
    "_restore_from_log",
    "_prebuild_mod_indexes",
    "_resolve_root_path",
    "_resolve_root_path_str",
    "_FILEMAP_SNAPSHOT_NAME",
    "_write_deploy_snapshot",
    "_load_deploy_snapshot",
    "_move_runtime_files",
    "_append_overwrite_log",
    "OVERWRITE_LOG_NAME",
    "_path_under_root",
    "_get_staging_source_path",
    "_build_mod_index",
    "_resolve_nocase",
    # Re-exported stdlib/project imports used by other deploy_* modules
    "_safe_log",
    "_has_traversal",
    "_time",
    "_contextmanager",
]
