"""Toolkit-neutral mod installation core (no Tk / no Qt).

A self-contained install pipeline the Qt UI calls: extract an archive, auto-strip
to a sensible mod root, copy into the game's staging folder, then update the mod
index (+ BSA index), modlist.txt and plugins.txt. FOMOD archives are installed
with their DEFAULT (recommended) selections — the interactive wizard is NOT run
here (deferred to a later Qt port); a plain non-FOMOD layout installs verbatim.

The heavy Tk-coupled `gui/install_mod.py` is the full-featured path (FOMOD/BAIN
wizards, replace dialogs, Nexus lookup). This module reuses the same neutral
`Utils.*` backend so behaviour matches for the common case.

Public API:
    install_archive(archive_path, game, profile_dir, *, log_fn, progress_fn,
                    preferred_name="") -> str | None   # installed mod name
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Callable, Optional

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int, Optional[str]], None]

# Sentinels returned by install_collection_archive when an interactive FOMOD/BAIN
# has no author selections and must be deferred to the end of a collection install
# (opened one-by-one in phase order). gui/install_mod.py imports these back so the
# Tk and Qt installers agree on the sentinel value.
FOMOD_DEFERRED = "__FOMOD_DEFERRED__"
BAIN_DEFERRED = "__BAIN_DEFERRED__"


# ---- case-insensitive copy core (moved from gui/install_mod.py, shared) ------
# These resolve FOMOD/archive paths case-insensitively against the real (case-
# sensitive Linux) filesystem and dedup by destination — the accumulated fixes
# for "wrong files installed" / duplicate-cased folders. gui/install_mod.py
# imports them back so there is ONE implementation.
def _resolve_src_case(src_root: Path, src_rel: str,
                      _cache: "dict[Path, dict[str, str]] | None" = None) -> Path:
    """src_root / src_rel with each component resolved case-insensitively against
    what exists on disk (FOMOD XML is Windows-cased)."""
    if _cache is None:
        _cache = {}
    current = src_root
    for part in src_rel.replace("\\", "/").strip("/").split("/"):
        if not part:
            continue
        if current not in _cache:
            try:
                _cache[current] = {p.name.lower(): p.name
                                   for p in current.iterdir() if current.is_dir()}
            except OSError:
                _cache[current] = {}
        current = current / _cache[current].get(part.lower(), part)
    return current


def _resolve_dst_case(dest_root: Path, dst_rel: str,
                      _cache: "dict[Path, dict[str, str]] | None" = None) -> Path:
    """dest_root / dst_rel with each component resolved case-insensitively so a
    FOMOD install doesn't create duplicate folders differing only in case."""
    if _cache is None:
        _cache = {}
    current = dest_root
    for part in dst_rel.replace("\\", "/").split("/"):
        if not part:
            continue
        if current not in _cache:
            try:
                _cache[current] = {p.name.lower(): p.name
                                   for p in current.iterdir() if current.is_dir()}
            except OSError:
                _cache[current] = {}
        current = current / _cache[current].get(part.lower(), part)
    return current


def _link_or_copy(src, dst) -> None:
    """Hardlink src→dst when same-fs (near-instant, zero extra disk), else copy."""
    try:
        os.link(src, dst)
        return
    except OSError:
        try:
            shutil.copy2(src, dst)
        except FileNotFoundError:
            shutil.copy2(src, dst)


def _copytree_case_insensitive(src: Path, dst: Path) -> int:
    """Recursive copy resolving each dst component case-insensitively against
    disk (a case-aware shutil.copytree(dirs_exist_ok=True)). Returns file count."""
    copied = 0
    dst.mkdir(parents=True, exist_ok=True)
    for entry in os.scandir(src):
        try:
            existing = {p.name.lower(): p.name for p in dst.iterdir()}
        except OSError:
            existing = {}
        child_dst = dst / existing.get(entry.name.lower(), entry.name)
        if entry.is_dir(follow_symlinks=False):
            copied += _copytree_case_insensitive(Path(entry.path), child_dst)
        elif entry.is_file(follow_symlinks=False):
            child_dst.parent.mkdir(parents=True, exist_ok=True)
            if child_dst.is_dir():
                shutil.rmtree(child_dst)
            elif child_dst.exists():
                child_dst.chmod(0o644)
                child_dst.unlink()
            _link_or_copy(entry.path, child_dst)
            copied += 1
    return copied


def _copy_file_list(file_list, src_root: str, dest_root: Path, log_fn) -> None:
    """Copy each (src_rel, dst_rel, is_folder) from src_root → dest_root with
    case-insensitive resolution + destination dedup (later wins = FOMOD priority).
    Folders via the recursive copytree; files in parallel."""
    from concurrent.futures import ThreadPoolExecutor

    folder_copied = 0
    file_entries: list = []
    _src_cache: dict = {}
    _dst_cache: dict = {}
    src_root_path = Path(src_root)

    for src_rel, dst_rel, is_folder in file_list:
        if src_rel and src_rel == dst_rel:
            src = src_root_path / src_rel.replace("\\", "/")
        elif src_rel:
            src = _resolve_src_case(src_root_path, src_rel, _src_cache)
        else:
            src = src_root_path
        dst = (_resolve_dst_case(dest_root, dst_rel, _dst_cache)
               if dst_rel else dest_root / dst_rel)
        if is_folder:
            if not dst_rel:
                dst = dest_root
            if src.is_dir():
                folder_copied += _copytree_case_insensitive(src, dst)
                _dst_cache.pop(dst.parent, None)
        else:
            if not dst_rel:
                dst = dest_root / src.name
            elif dst_rel.endswith("/") or dst_rel.endswith("\\"):
                dst = _resolve_dst_case(dest_root, dst_rel.rstrip("/\\"), _dst_cache) / src.name
            if src.is_file():
                file_entries.append((src, dst))

    if file_entries:
        by_dst: dict = {}
        for src, dst in file_entries:
            by_dst[str(dst).lower()] = (src, dst)
        file_entries = list(by_dst.values())

    dirs_seen: set = set()
    for _, dst in file_entries:
        d = dst.parent
        if d not in dirs_seen:
            d.mkdir(parents=True, exist_ok=True)
            dirs_seen.add(d)

    def _copy_one(src_dst):
        src, dst = src_dst
        if dst.is_dir():
            shutil.rmtree(dst)
        elif dst.exists() or dst.is_symlink():
            try:
                dst.unlink()
            except PermissionError:
                dst.chmod(0o644)
                dst.unlink()
        _link_or_copy(src, dst)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for _ in pool.map(_copy_one, file_entries, chunksize=256):
            pass
    copied = folder_copied + len(file_entries)
    log_fn(f"Copied {copied} item(s) to staging area.")


# ---- file-list staging pipeline (moved from gui/install_mod.py, shared) -------
# Builds a (src, dst, is_folder) list for a non-FOMOD archive and normalises it to
# the game's expected layout: strip prefixes → install-prefix → required-top-level
# check → auto-strip folders → auto-strip to a required file type → (else) ask for
# a prefix → post-strip. gui/install_mod.py imports these back so Tk + Qt run the
# SAME staging code (fixes "mod installed with wrong structure" e.g. CET).
def resolve_direct_files(extract_dir: str) -> list[tuple[str, str, bool]]:
    """Every file under *extract_dir* as a (src, dst, is_folder) tuple (src == dst,
    both relative to the root). A top-level ``fomod/`` folder is skipped (installer
    metadata, not game content)."""
    result: list[tuple[str, str, bool]] = []
    root = Path(extract_dir)
    for entry in root.rglob("*"):
        if entry.is_file():
            rel = str(entry.relative_to(root))
            first = rel.replace("\\", "/").split("/", 1)[0]
            if first.lower() == "fomod":
                continue
            result.append((rel, rel, False))
    return result


def unwrap_single_folder(extract_dir: str) -> str:
    """If *extract_dir* has exactly one subdirectory and no files, return that
    subdirectory (archives wrapped in a single ModName/ folder)."""
    root = Path(extract_dir)
    try:
        entries = list(root.iterdir())
    except OSError:
        return extract_dir
    if len(entries) == 1 and entries[0].is_dir():
        return str(entries[0])
    return extract_dir


def apply_strip_prefixes_to_file_list(
    file_list: list[tuple[str, str, bool]],
    strip_prefixes: set[str],
) -> list[tuple[str, str, bool]]:
    """Strip leading path segments matching *strip_prefixes* (case-insensitive),
    repeatedly, until the first segment is not in the set."""
    if not strip_prefixes:
        return file_list
    strip_lower = {p.lower() for p in strip_prefixes}
    result: list[tuple[str, str, bool]] = []
    for src_rel, dst_rel, is_folder in file_list:
        had_trailing = dst_rel.endswith("/") or dst_rel.endswith("\\")
        d = dst_rel.replace("\\", "/").strip("/")
        while "/" in d:
            first, remainder = d.split("/", 1)
            if first.lower() in strip_lower:
                d = remainder
            else:
                break
        if had_trailing and d:
            d = d + "/"
        result.append((src_rel, d, is_folder))
    return result


def check_mod_top_level(file_list: list[tuple[str, str, bool]],
                        required: set[str]) -> bool:
    """True if at least one file's top-level folder matches a required name."""
    for _, dst_rel, _ in file_list:
        top = dst_rel.replace("\\", "/").split("/")[0].lower()
        if top in required:
            return True
    return False


def try_auto_strip_top_level(
    file_list: list[tuple[str, str, bool]],
    required: set[str],
    max_strip_depth: int = 20,
) -> tuple[list[tuple[str, str, bool]], bool]:
    """Strip leading path segments until at least one file's top-level folder is in
    *required*. Returns (new_list, True) on success, else (original, False)."""
    required_lower = {r.lower() for r in required}
    if check_mod_top_level(file_list, required_lower):
        return (file_list, True)
    for strip_depth in range(1, max_strip_depth + 1):
        new_list: list[tuple[str, str, bool]] = []
        has_required = False
        for src_rel, dst_rel, is_folder in file_list:
            parts = dst_rel.replace("\\", "/").strip("/").split("/")
            if len(parts) <= strip_depth:
                continue
            new_dst = "/".join(parts[strip_depth:])
            top = parts[strip_depth].lower()
            if top in required_lower:
                has_required = True
            new_list.append((src_rel, new_dst, is_folder))
        if has_required and new_list:
            return (new_list, True)
    return (file_list, False)


def check_mod_top_level_file_types(
    file_list: list[tuple[str, str, bool]],
    required_exts: set[str],
) -> bool:
    """True if at least one top-level file (no sub-folder) has a required ext."""
    exts_lower = {e.lower() for e in required_exts}
    for _, dst_rel, is_folder in file_list:
        if is_folder:
            continue
        dst_rel = dst_rel.replace("\\", "/").strip("/")
        if "/" not in dst_rel:
            ext = Path(dst_rel).suffix.lower()
            if ext in exts_lower:
                return True
    return False


def try_auto_strip_for_file_types(
    file_list: list[tuple[str, str, bool]],
    required_exts: set[str],
    max_strip_depth: int = 20,
) -> tuple[list[tuple[str, str, bool]], bool]:
    """Strip leading segments until a top-level file has a required ext. Returns
    (new_list, True) on success, else (original, False)."""
    if check_mod_top_level_file_types(file_list, required_exts):
        return (file_list, True)
    exts_lower = {e.lower() for e in required_exts}
    for strip_depth in range(1, max_strip_depth + 1):
        new_list: list[tuple[str, str, bool]] = []
        has_required = False
        for src_rel, dst_rel, is_folder in file_list:
            parts = dst_rel.replace("\\", "/").strip("/").split("/")
            if len(parts) <= strip_depth:
                continue
            new_dst = "/".join(parts[strip_depth:])
            if not is_folder and len(parts) == strip_depth + 1:
                ext = Path(new_dst).suffix.lower()
                if ext in exts_lower:
                    has_required = True
            new_list.append((src_rel, new_dst, is_folder))
        if has_required and new_list:
            return (new_list, True)
    return (file_list, False)


def stage_file_list(game, extract_dir: str, *, is_root_install: bool = False,
                    mod_name: str = "", on_need_prefix=None,
                    log_fn: LogFn = lambda _m: None
                    ) -> list[tuple[str, str, bool]] | None:
    """Build + normalise the (src, dst, is_folder) list for a non-FOMOD mod under
    *extract_dir*, applying the game's structure rules EXACTLY like the Tk
    installer (gui/install_mod.py lines ~2454–2570):

      strip_prefixes → mod_install_prefix → required-top-level check →
      auto-strip folders → auto-strip to a required file type →
      (unrecognised, not install-as-is) on_need_prefix() → post-strip.

    *on_need_prefix(required, file_list, mod_name) -> str | None* supplies the
    prefix interactively (the toolkit shows a dialog); returning None cancels the
    install (this function returns None). With no callback, an unrecognised mod is
    installed as-is. Returns the final file_list (or None if cancelled)."""
    file_list = resolve_direct_files(extract_dir)

    strip_prefixes = getattr(game, "mod_folder_strip_prefixes", set())
    if strip_prefixes and not is_root_install:
        file_list = apply_strip_prefixes_to_file_list(file_list, strip_prefixes)

    required = getattr(game, "mod_required_top_level_folders", set())
    required_lower = {r.lower() for r in required}

    install_prefix = getattr(game, "mod_install_prefix", "")
    if install_prefix and not is_root_install:
        install_prefix = install_prefix.strip().strip("/").replace("\\", "/")
        prefix_parts = install_prefix.lower().split("/")
        new_file_list = []
        for s, d, f in file_list:
            d_parts = d.replace("\\", "/").split("/")
            d_parts_lower = [p.lower() for p in d_parts]
            if d_parts_lower[0] in required_lower:
                new_file_list.append((s, d, f))
                continue
            match_len = 0
            for i in range(len(prefix_parts), 0, -1):
                if d_parts_lower[:i] == prefix_parts[-i:]:
                    match_len = i
                    break
            missing = "/".join(install_prefix.split("/")[:len(prefix_parts) - match_len])
            if missing:
                new_file_list.append((s, f"{missing}/{d}", f))
            else:
                new_file_list.append((s, d, f))
        file_list = new_file_list
        log_fn(f"Auto-prefixed mod files under '{install_prefix}/' (where needed).")

    required_file_types = getattr(game, "mod_required_file_types", set())
    auto_strip = getattr(game, "mod_auto_strip_until_required", False)
    install_as_is = getattr(game, "mod_install_as_is_if_no_match", False)
    did_auto_strip = False

    if required and not check_mod_top_level(file_list, required):
        if auto_strip:
            file_list, did_auto_strip = try_auto_strip_top_level(file_list, required)
            if did_auto_strip:
                log_fn("Auto-stripped top-level folder(s) so mod matches expected structure.")
        if not did_auto_strip and required_file_types:
            if check_mod_top_level_file_types(file_list, required_file_types):
                did_auto_strip = True
                log_fn("Mod contains recognised top-level file type(s) — skipping prefix check.")
            elif auto_strip:
                file_list, did_auto_strip = try_auto_strip_for_file_types(
                    file_list, required_file_types)
                if did_auto_strip:
                    log_fn("Auto-stripped top-level folder(s) to expose recognised file type(s).")
        if not did_auto_strip:
            if install_as_is:
                log_fn("Mod structure unrecognised — installing as-is (no prefix applied).")
            else:
                prefix = on_need_prefix(required, file_list, mod_name) if on_need_prefix else None
                if prefix is None and on_need_prefix is not None:
                    log_fn("Install cancelled — mod structure not mapped.")
                    return None
                if prefix:
                    prefix = prefix.strip().strip("/").replace("\\", "/")
                    file_list = [(s, f"{prefix}/{d}", f) for s, d, f in file_list]
                    log_fn(f"Remapped mod files under '{prefix}/'.")
    elif (not required and required_file_types
          and not check_mod_top_level_file_types(file_list, required_file_types)):
        if auto_strip:
            file_list, did_auto_strip = try_auto_strip_for_file_types(
                file_list, required_file_types)
            if did_auto_strip:
                log_fn("Auto-stripped top-level folder(s) to expose recognised file type(s).")
        if not did_auto_strip:
            if install_as_is:
                log_fn("Mod structure unrecognised — installing as-is (no prefix applied).")
            else:
                prefix = on_need_prefix(set(), file_list, mod_name) if on_need_prefix else None
                if prefix is None and on_need_prefix is not None:
                    log_fn("Install cancelled — mod structure not mapped.")
                    return None
                if prefix:
                    prefix = prefix.strip().strip("/").replace("\\", "/")
                    file_list = [(s, f"{prefix}/{d}", f) for s, d, f in file_list]
                    log_fn(f"Remapped mod files under '{prefix}/'.")

    post_strip_prefixes = getattr(game, "mod_folder_strip_prefixes_post", set())
    if post_strip_prefixes and not is_root_install:
        file_list = apply_strip_prefixes_to_file_list(file_list, post_strip_prefixes)

    return file_list


# ---------------------------------------------------------------- temp location
def _uncompressed_size(path: str) -> int:
    """Best-effort uncompressed size in bytes. ZIP reads central-directory sizes;
    otherwise a 15× compressed-size estimate (handles texture packs)."""
    try:
        compressed = os.path.getsize(path)
    except OSError:
        compressed = 0
    if path.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(path, "r") as zf:
                total = sum(m.file_size for m in zf.infolist())
            if total > 0:
                return total
        except Exception:
            pass
    return compressed * 15


def _is_small_fs(path: str, limit_gib: int = 8) -> bool:
    """True if *path*'s mount is small enough to be a tmpfs ramdisk (e.g. /tmp)."""
    try:
        st = os.statvfs(path)
        return st.f_blocks * st.f_frsize < limit_gib * 1024 ** 3
    except OSError:
        return False


def _free_bytes(path: str) -> int:
    try:
        st = os.statvfs(path)
        return st.f_frsize * st.f_bavail
    except OSError:
        return 0


def _choose_extract_parent(archive_path: str, staging_root: Path,
                           log_fn: LogFn) -> Path | None:
    """Pick a temp-dir PARENT that can hold the extraction. Default /tmp is often
    a small tmpfs ramdisk (Steam Deck: ~1GB) — if the archive won't fit there
    with headroom, extract NEXT TO the staging folder (real disk) instead. This
    mirrors the Tk app's reroute logic — without it large mods fail with
    'No space left on device'. Returns the parent dir, or None to use /tmp."""
    need = _uncompressed_size(archive_path)
    headroom = 512 * 1024 * 1024
    tmp = tempfile.gettempdir()
    if need + headroom < _free_bytes(tmp):
        return None   # /tmp has room
    # /tmp too small (commonly a ramdisk) → use the staging filesystem (real disk).
    disk_parent = staging_root.parent if staging_root else None
    if disk_parent is not None:
        try:
            disk_parent.mkdir(parents=True, exist_ok=True)
            if need + headroom < _free_bytes(str(disk_parent)):
                log_fn(f"Extracting to disk ({disk_parent}) — /tmp too small for "
                       f"~{need // (1024 * 1024)} MB.")
                return disk_parent
            log_fn(f"Warning: extract target {disk_parent} may also be low on "
                   "space.")
            return disk_parent
        except OSError:
            pass
    return None


# ---------------------------------------------------------------- extraction
def _debackslash_extracted_tree(extract_dir: str, log_fn: LogFn) -> int:
    """Repair an extraction that kept Windows backslash path separators.

    Some ZIPs (commonly packed by PowerShell's ``Compress-Archive``, or Nexus
    mods zipped on Windows) store member names with ``\\`` separators, which
    violates the ZIP spec. Native extractors (7z/bsdtar) and Python's
    ``zipfile`` then create *flat* files whose names literally contain
    backslashes — e.g. a single file called ``r6\\scripts\\foo.reds`` — instead
    of a nested folder tree. Downstream staging resolves paths with forward
    slashes, so those flat entries never match and the mod stages 0 files
    ("nothing staged"). This sweep relocates every backslash-named entry to its
    proper nested location. Returns the number of entries moved. Idempotent and
    cheap when no backslash names exist (the common case).

    Faithful port of ``gui/install_mod.py:_debackslash_extracted_tree``.
    """
    root = Path(extract_dir)
    # Collect first so we don't mutate the tree mid-walk. Deepest paths first so
    # files move before we try to clean up their (now-empty) flat parents.
    try:
        offenders = [p for p in root.rglob("*") if "\\" in p.name]
    except OSError:
        return 0
    if not offenders:
        return 0
    moved = 0
    for entry in sorted(offenders, key=lambda p: len(str(p)), reverse=True):
        if not entry.exists():
            continue
        rel = str(entry.relative_to(root)).replace("\\", "/")
        target = root / rel
        if target == entry:
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry.is_dir():
                # A backslash-named directory: merge its contents, then drop it.
                target.mkdir(parents=True, exist_ok=True)
                for child in entry.iterdir():
                    dest = target / child.name
                    if not dest.exists():
                        shutil.move(str(child), str(dest))
                try:
                    entry.rmdir()
                except OSError:
                    pass
            else:
                if target.exists():
                    target.unlink()
                shutil.move(str(entry), str(target))
                moved += 1
        except OSError:
            continue
    if moved and log_fn is not None:
        log_fn(f"Normalised {moved} Windows backslash path(s) from archive.")
    return moved


def _extract_archive(archive_path: str, dest_dir: str, log_fn: LogFn) -> bool:
    """Extract *archive_path* into *dest_dir*. Native 7z → bsdtar → py7zr →
    Python zipfile/tarfile, mirroring gui.install_mod's fallback chain. After a
    successful native/zip extraction, backslash-named members are normalised
    into a real tree (see _debackslash_extracted_tree)."""
    ext = Path(archive_path).suffix.lower()

    # tar.* and plain .tar → tarfile directly.
    if ext in (".tar", ".gz", ".bz2", ".xz", ".tgz") or \
            archive_path.lower().endswith((".tar.gz", ".tar.bz2", ".tar.xz")):
        try:
            with tarfile.open(archive_path, "r:*") as tf:
                tf.extractall(dest_dir)
            log_fn("Extracted with tarfile.")
            return True
        except Exception as exc:
            log_fn(f"tarfile failed ({exc}).")
            # fall through to the generic extractors

    def _ok() -> bool:
        # Native extractors (7z/bsdtar) and Python zipfile reproduce Windows
        # backslash member names as literal flat filenames; repair them into a
        # real tree so staging can resolve the paths (fixes "nothing staged").
        _debackslash_extracted_tree(dest_dir, log_fn)
        return True

    _7z = (shutil.which("7zzs") or shutil.which("7zz")
           or shutil.which("7z") or shutil.which("7za"))
    if _7z:
        r = subprocess.run(
            [_7z, "x", archive_path, f"-o{dest_dir}", "-y", "-mmt=on"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if r.returncode == 0:
            log_fn("Extracted with 7z.")
            return _ok()
        log_fn(f"7z failed ({r.stderr.strip()}), trying bsdtar…")
    if shutil.which("bsdtar"):
        r = subprocess.run(
            ["bsdtar", "-xf", archive_path, "-C", dest_dir],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if r.returncode == 0 and any(os.scandir(dest_dir)):
            log_fn("Extracted with bsdtar.")
            return _ok()
        log_fn(f"bsdtar failed ({r.stderr.strip()}), trying py7zr…")
    try:
        import py7zr
        with py7zr.SevenZipFile(archive_path, "r") as z:
            z.extractall(dest_dir)
        log_fn("Extracted with py7zr.")
        return _ok()
    except Exception as exc:
        log_fn(f"py7zr failed ({exc}), trying zipfile…")
    try:
        with zipfile.ZipFile(archive_path, "r") as z:
            z.extractall(dest_dir)
        log_fn("Extracted with zipfile.")
        return _ok()
    except Exception as exc:
        log_fn(f"zipfile failed ({exc}).")
    return False


# ---------------------------------------------------------------- root detection
def _single_root_unwrap(extract_dir: Path) -> Path:
    """If the archive extracted into a single wrapper folder (and nothing else),
    descend ONE level — matches Tk's `unwrap_single_folder`. Must NOT loop down a
    chain: e.g. CET ships everything under `bin/x64/`, and descending the whole
    `bin`→`x64` chain would strip the required `bin/` structure (then the mod
    installs to the wrong place / triggers the prefix dialog)."""
    try:
        children = [c for c in extract_dir.iterdir() if c.name not in (".", "..")]
    except OSError:
        return extract_dir
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return extract_dir


def _find_fomod_archive(extract_dir: Path) -> Path | None:
    """Return the path to a `.fomod` file inside *extract_dir* if the archive
    is just a wrapper around one (optionally peeling a single top-level
    folder). A `.fomod` is itself a renamed 7z/zip — Nexus packages older
    Fallout/Oblivion mods this way and they need a second extraction pass
    before FOMOD detection can find `fomod/ModuleConfig.xml` (Tk parity)."""
    root = _single_root_unwrap(extract_dir)
    try:
        entries = list(root.iterdir())
    except OSError:
        return None
    fomods = [p for p in entries if p.is_file() and p.suffix.lower() == ".fomod"]
    if len(fomods) == 1:
        siblings = [p for p in entries if p != fomods[0]]
        if all(s.is_file() and s.suffix.lower() in
               (".txt", ".md", ".rtf", ".jpg", ".png", ".pdf") for s in siblings):
            return fomods[0]
    return None


# ------------------------------------------------------------ prepare / finish
class PreparedInstall:
    """An extracted-but-not-yet-staged archive. `extract_dir` lives until
    `cleanup()` (so an interactive FOMOD wizard can read images + the config and
    then call `finish_install`). When `fomod_base`/`fomod_config` are set the
    caller should run the wizard; otherwise it's a plain install."""
    def __init__(self, archive: Path, game, profile_dir: Path, mod_name: str,
                 extract_dir: Path, src_root: Path,
                 fomod_base: Path | None, fomod_config, prebuilt_meta=None,
                 on_need_prefix=None):
        self.archive = archive
        self.game = game
        self.profile_dir = profile_dir
        self.mod_name = mod_name
        self.extract_dir = extract_dir
        self.src_root = src_root
        self.fomod_base = fomod_base
        self.fomod_config = fomod_config
        # Optional NexusModMeta supplied by the caller (e.g. the Nexus browser,
        # which knows the real mod_id/file_id) — written verbatim instead of
        # parsing the (sometimes wrong) archive filename.
        self.prebuilt_meta = prebuilt_meta
        # Optional callback on_need_prefix(required, file_list, mod_name) -> str|None
        # invoked when a non-FOMOD mod's structure doesn't match the game and
        # auto-strip fails — the toolkit shows the Set-Prefix dialog. None = the
        # neutral default (install as-is).
        self.on_need_prefix = on_need_prefix
        # BAIN complex-package detection (mutually exclusive with FOMOD — a
        # non-FOMOD archive is probed for a BAIN sub-package layout at prepare
        # time so the caller can show the picker before finish_install stages it).
        # bain_subpkgs is a list[BainSubPackage] (or None); bain_root is the
        # single-folder-unwrapped extract dir the sub-package paths are relative
        # to. readme_text / saved_bain_selections feed the picker.
        self.bain_subpkgs = None
        self.bain_root = None
        self.readme_text = None
        self.saved_bain_selections = None
        # FOMOD wizard context (set at prepare time when a FOMOD is detected):
        # saved selections from the previous install of this mod (global config
        # JSON, restores + green-highlights prior choices — Tk parity) and the
        # (installed, active, loose) file sets its conditions evaluate against.
        self.saved_fomod_selections = None
        self.fomod_context = (set(), set(), set())
        # Set by finish_install when the user chose to Replace an existing mod:
        # keep its modlist position + carry its endorsed flag onto the new install.
        self._preserve_position = False
        self._preserved_endorsed = False

    def is_fomod(self) -> bool:
        return self.fomod_base is not None and self.fomod_config is not None

    def is_bain(self) -> bool:
        return bool(self.bain_subpkgs)

    def cleanup(self):
        shutil.rmtree(self.extract_dir, ignore_errors=True)


def prepare_archive(archive_path: str, game, profile_dir: Path, *,
                    log_fn: LogFn, progress_fn: Optional[ProgressFn] = None,
                    preferred_name: str = "", prebuilt_meta=None,
                    on_need_prefix=None) -> PreparedInstall | None:
    """Extract *archive_path* to a kept temp dir and detect FOMOD. The caller
    either runs the wizard (is_fomod) then `finish_install(prepared, selections)`,
    or just calls `finish_install(prepared, None)` for a plain/default install.
    Returns None on failure (and cleans up)."""
    archive = Path(archive_path)
    if not archive.is_file():
        log_fn(f"Install: archive not found: {archive_path}")
        return None
    staging_root = game.get_effective_mod_staging_path()
    if staging_root is None:
        log_fn("Install: no staging folder configured.")
        return None
    mod_name = preferred_name or _clean_mod_name(archive.stem, game)

    def _p(done, total, phase=None):
        if progress_fn is not None:
            progress_fn(done, total, phase)

    _p(0, 0, "Extracting")
    # Pick a temp parent big enough — /tmp is a small ramdisk on the Deck, so a
    # large mod must extract to the staging disk instead (Tk parity).
    parent = _choose_extract_parent(str(archive), Path(staging_root), log_fn)
    extract_dir = Path(tempfile.mkdtemp(prefix="mm_install_",
                                        dir=str(parent) if parent else None))
    if not _extract_archive(str(archive), str(extract_dir), log_fn):
        log_fn("Install failed: could not extract the archive.")
        shutil.rmtree(extract_dir, ignore_errors=True)
        return None

    # A `.fomod`-wrapper archive needs a second extraction pass before FOMOD
    # detection can find fomod/ModuleConfig.xml (Tk parity).
    fomod_wrapper = _find_fomod_archive(extract_dir)
    if fomod_wrapper is not None:
        log_fn(f"Archive contains a .fomod wrapper — extracting {fomod_wrapper.name}…")
        inner_dir = Path(tempfile.mkdtemp(prefix="mm_install_",
                                          dir=str(extract_dir.parent)))
        if _extract_archive(str(fomod_wrapper), str(inner_dir), log_fn):
            shutil.rmtree(extract_dir, ignore_errors=True)
            extract_dir = inner_dir
        else:
            log_fn("Install failed: could not extract the inner .fomod archive.")
            shutil.rmtree(inner_dir, ignore_errors=True)
            shutil.rmtree(extract_dir, ignore_errors=True)
            return None

    src_root = _single_root_unwrap(extract_dir)
    # Shared Tk detection: case-insensitive fomod/moduleconfig.xml lookup with
    # wrapper-peel + bounded BFS. (Rebuilding the config path with a lowercase
    # "fomod" broke uppercase FOMOD/ dirs on case-sensitive filesystems and
    # misrouted such archives to the BAIN probe.)
    from Utils.fomod_parser import (detect_fomod, detect_scripted_fomod,
                                    parse_module_config)
    fomod_base: Path | None = None
    config = None
    fomod_result = detect_fomod(str(extract_dir))
    if fomod_result is not None:
        mod_root, config_path = fomod_result
        try:
            config = parse_module_config(config_path)
            fomod_base = Path(mod_root)
        except Exception as exc:
            log_fn(f"FOMOD parse failed ({exc}); will install verbatim.")
    elif detect_scripted_fomod(str(extract_dir)):
        log_fn("WARNING: This is a scripted (C#) FOMOD installer, which this "
               "manager cannot run. All files will be installed without "
               "showing any install options. If you need to choose optional "
               "components, look for an XML-based or manually-packaged "
               "version of this mod on Nexus.")
    prepared = PreparedInstall(archive, game, profile_dir, mod_name,
                               extract_dir, src_root, fomod_base, config,
                               prebuilt_meta=prebuilt_meta,
                               on_need_prefix=on_need_prefix)
    if fomod_base is not None:
        prepared.saved_fomod_selections = _read_saved_fomod_selections(
            game, mod_name, log_fn)
        try:
            prepared.fomod_context = _collection_plugin_context(game, profile_dir)
        except Exception:
            pass

    # BAIN is mutually exclusive with FOMOD (Tk parity): only probe an archive
    # with no FOMOD installer at all (a detected-but-unparseable FOMOD installs
    # verbatim, it must not fall into the BAIN picker). Detect a complex BAIN
    # layout so the caller can show the picker.
    if fomod_result is None and getattr(game, "supports_bain", True):
        try:
            from Utils.bain_installer import detect_bain, bain_unwrap_single_folder
            bain_root = bain_unwrap_single_folder(str(extract_dir))
            subpkgs = detect_bain(
                bain_root, extra_exts=getattr(game, "plugin_extensions", None))
        except Exception as exc:
            log_fn(f"BAIN detection failed ({exc}); will install verbatim.")
            subpkgs = None
            bain_root = str(extract_dir)
        if subpkgs:
            prepared.bain_subpkgs = subpkgs
            prepared.bain_root = bain_root
            prepared.readme_text = _read_bain_readme(bain_root)
            prepared.saved_bain_selections = _read_saved_bain_selections(
                game, mod_name, log_fn)
            log_fn(f"BAIN package detected — {len(subpkgs)} sub-package(s).")
    return prepared


def finish_install(prepared: "PreparedInstall", fomod_selections, *,
                   log_fn: LogFn, progress_fn: Optional[ProgressFn] = None,
                   on_exists=None, bain_selections=None) -> str | None:
    """Stage the prepared archive. *fomod_selections* is the wizard's
    {step_idx: {group: [plugins]}} dict (or None → FOMOD defaults / plain copy).
    *bain_selections* is the BAIN picker's ``{"selected": [name, ...]}`` dict for
    a BAIN package (or None → its default "00"-prefixed sub-packages).
    Always cleans up the extract dir. Returns the installed mod name (None on
    cancel).

    *on_exists* — optional callback invoked when the destination mod folder
    already exists: ``on_exists(mod_name, conflict) -> str`` returning one of
    ``"replace"`` (wipe + reinstall, keep modlist position + endorsed flag),
    ``"rename:<newname>"`` (install as a NEW mod), or ``"cancel"``. *conflict* is
    True on a re-prompt when a chosen rename target is itself taken. When
    *on_exists* is None the existing folder is silently replaced (collection /
    quick-update path)."""
    p = prepared
    staging_root = Path(p.game.get_effective_mod_staging_path())
    staging_root.mkdir(parents=True, exist_ok=True)
    dest_root = staging_root / p.mod_name

    def _pp(done, total, phase=None):
        if progress_fn is not None:
            progress_fn(done, total, phase)

    # Existing same-named folder: ask the caller what to do (replace / rename /
    # cancel), or — with no callback — silently replace (collection installs).
    if dest_root.exists():
        if on_exists is None:
            log_fn(f"Replacing existing mod folder: {p.mod_name}")
            shutil.rmtree(dest_root, ignore_errors=True)
        else:
            conflict = False
            while dest_root.exists():
                action = on_exists(p.mod_name, conflict)
                if action == "cancel" or not action:
                    log_fn(f"Install cancelled — '{p.mod_name}' already exists.")
                    p.cleanup()
                    return None
                if action == "replace":
                    # Carry the old install's endorsed flag onto the new one.
                    try:
                        from Nexus.nexus_meta import read_meta
                        p._preserved_endorsed = bool(
                            read_meta(dest_root / "meta.ini").endorsed)
                    except Exception:
                        p._preserved_endorsed = False
                    log_fn(f"Replacing existing mod folder: {p.mod_name}")
                    shutil.rmtree(dest_root, ignore_errors=True)
                    p._preserve_position = True
                    break
                if action.startswith("rename:"):
                    new_name = action.split(":", 1)[1].strip()
                    if not new_name or new_name == p.mod_name:
                        conflict = True
                        continue
                    p.mod_name = new_name
                    dest_root = staging_root / p.mod_name
                    # Loop: if the new name is ALSO taken, re-prompt (conflict).
                    conflict = dest_root.exists()
                    # rename installs as a NEW mod (no position preserve).
                    continue
                # Unknown action → treat as cancel (safe default).
                p.cleanup()
                return None

    cancelled = False
    bain_selected: "list[str] | None" = None
    try:
        if p.is_fomod():
            # Persist the wizard's choices (restored + highlighted next time).
            # None = headless defaults install → nothing to remember (Tk parity).
            if fomod_selections is not None:
                _persist_fomod_selection(p.game, p.mod_name, fomod_selections)
            ok = _install_fomod(p.fomod_base, p.fomod_config, dest_root,
                                fomod_selections, log_fn, _pp,
                                context=p.fomod_context)
            if not ok:
                log_fn("FOMOD resolve failed — installing all files verbatim.")
                _copy_tree(p.src_root, dest_root, log_fn, _pp)
        elif p.is_bain():
            # BAIN: merge the selected sub-packages (later ones override earlier),
            # with paths relative to the unwrapped bain_root — mirroring the
            # collection install path.
            from Utils.bain_installer import resolve_bain_files
            if bain_selections is not None and isinstance(
                    bain_selections.get("selected"), list):
                bain_selected = list(bain_selections["selected"])
            else:
                bain_selected = [pkg.name for pkg in p.bain_subpkgs
                                 if pkg.default_selected]
                log_fn("BAIN: using default sub-package selection.")
            file_list = resolve_bain_files(p.bain_subpkgs, set(bain_selected))
            log_fn(f"BAIN: {len(bain_selected)} sub-package(s), "
                   f"{len(file_list)} file(s) to install.")
            dest_root.mkdir(parents=True, exist_ok=True)
            _copy_file_list(file_list, p.bain_root, dest_root, log_fn)
        else:
            # Non-FOMOD: build + normalise the file list to the game's expected
            # structure (strip/required-top-level/auto-strip/prefix), EXACTLY like
            # the Tk installer, then copy. This is what makes e.g. CET land under
            # bin/x64 instead of installing verbatim. is_root_install mirrors Tk:
            # it's THIS mod's root_folder meta flag (default False), NOT a game flag.
            is_root = bool(getattr(p.prebuilt_meta, "root_folder", False)
                           if p.prebuilt_meta is not None else False)
            # Stage from the RAW extract dir (like Tk's direct-install path uses
            # `extract_dir`), NOT the single-folder-unwrapped src_root: e.g. CET
            # ships everything under bin/x64/, and staging from an unwrapped root
            # would have stripped the required bin/ folder.
            stage_root = str(p.extract_dir)
            file_list = stage_file_list(
                p.game, stage_root, is_root_install=is_root,
                mod_name=p.mod_name, on_need_prefix=p.on_need_prefix, log_fn=log_fn)
            if file_list is None:
                cancelled = True
            else:
                dest_root.mkdir(parents=True, exist_ok=True)
                _copy_file_list(file_list, stage_root, dest_root, log_fn)
    finally:
        p.cleanup()

    if cancelled:
        shutil.rmtree(dest_root, ignore_errors=True)
        return None

    if not dest_root.is_dir() or not any(dest_root.iterdir()):
        log_fn(f"Install failed: nothing was staged for '{p.mod_name}'.")
        try:
            dest_root.rmdir()
        except OSError:
            pass
        return None

    _write_install_meta(dest_root, p.archive, p.game, log_fn,
                        prebuilt_meta=getattr(p, "prebuilt_meta", None),
                        endorsed=getattr(p, "_preserved_endorsed", False),
                        is_bain=bain_selected is not None)
    # Persist the BAIN sub-package selection (global + profile) so a re-install
    # restores the user's choices (Tk parity).
    if bain_selected is not None:
        _persist_bain_selection(p.game, p.mod_name, {"selected": bain_selected})
    _pp(0, 0, "Indexing")
    _update_indexes(p.game, p.profile_dir, p.mod_name, dest_root, log_fn)
    _add_to_modlist(p.profile_dir, p.mod_name, log_fn,
                    preserve_position=getattr(p, "_preserve_position", False))
    _add_plugins(p.game, p.profile_dir, dest_root, log_fn)
    log_fn(f"Installed '{p.mod_name}'.")
    return p.mod_name


def install_archive(archive_path: str, game, profile_dir: Path, *,
                    log_fn: LogFn, progress_fn: Optional[ProgressFn] = None,
                    preferred_name: str = "") -> str | None:
    """Non-interactive install (FOMOD → default selections). Convenience wrapper
    over prepare_archive + finish_install."""
    prepared = prepare_archive(archive_path, game, profile_dir, log_fn=log_fn,
                               progress_fn=progress_fn, preferred_name=preferred_name)
    if prepared is None:
        return None
    return finish_install(prepared, None, log_fn=log_fn, progress_fn=progress_fn)


# ---------------------------------------------------------- collection installs
def _collection_plugin_context(game, profile_dir: "Path | None"
                               ) -> "tuple[set[str], set[str], set[str]]":
    """Build the (installed_files, active_files, loose_files) sets a collection
    FOMOD needs to evaluate its conditions — a tkinter-free port of the set-up
    block in ``gui/install_mod.py`` (~1747-1794). Reads plugins.txt/loadorder.txt/
    filemap.txt next to the profile, then seeds vanilla/DLC plugins (loaded
    implicitly by the engine, so never in plugins.txt)."""
    installed_files: set[str] = set()
    active_files: set[str] = set()
    loose_files: set[str] = set()
    if profile_dir is not None:
        try:
            from Utils.plugins import read_plugins, read_loadorder
            for entry in read_plugins(profile_dir / "plugins.txt"):
                installed_files.add(entry.name.lower())
                if entry.enabled:
                    active_files.add(entry.name.lower())
            for name in read_loadorder(profile_dir / "loadorder.txt"):
                installed_files.add(name.lower())
        except Exception:
            pass
        # <fileDependency> nodes can reference arbitrary asset paths; MO2 checks
        # the whole virtual tree, so mirror the filemap's relative paths.
        try:
            with open(profile_dir / "filemap.txt", "r", encoding="utf-8") as fmf:
                for line in fmf:
                    rel = line.split("\t", 1)[0].strip()
                    if rel:
                        loose_files.add(rel.replace("\\", "/").lower())
        except OSError:
            pass
    if game is not None:
        try:
            from gui.game_helpers import _vanilla_plugins_for_game
            for vname_lower in _vanilla_plugins_for_game(game).keys():
                installed_files.add(vname_lower)
                active_files.add(vname_lower)
        except Exception:
            pass
    return installed_files, active_files, loose_files


def install_collection_archive(
        archive_path: str, game, profile_dir: Path, *,
        log_fn: LogFn, progress_fn: Optional[ProgressFn] = None,
        preferred_name: str = "",
        prebuilt_meta=None,
        fomod_auto_selections: "dict | None" = None,
        bain_auto_selections: "dict | None" = None,
        overwrite_existing: "bool | None" = None,
        skip_index_update: bool = True,
        defer_interactive_fomod: bool = False,
        defer_interactive_bain: bool = False,
        resolve_fomod=None,
        resolve_bain=None,
        on_installed=None) -> "str | None":
    """Install ONE collection mod from a downloaded archive — the tkinter-free
    equivalent of ``gui/install_mod.py:install_mod_from_archive`` for the paths a
    collection install exercises (FOMOD with author selections or deferred, BAIN,
    dinput/root_folder, plain). Reuses the shared neutral staging/meta/modlist
    helpers in this module.

    Returns the installed folder name, or the ``FOMOD_DEFERRED`` / ``BAIN_DEFERRED``
    sentinel when an interactive installer has no author selections and
    ``defer_interactive_*`` is set (the orchestrator processes those at the end,
    one-by-one, passing ``resolve_fomod`` / ``resolve_bain`` to show the wizard),
    or ``None`` on failure / user-cancel.

    resolve_fomod(config, fomod_base, mod_name, installed, active, loose,
                  saved_selections) -> dict|None
    resolve_bain(subpackages, mod_root, mod_name) -> {"selected":[...]}|None
        (return None to cancel that mod). ``on_installed(is_fomod: bool)`` fires
        after a successful stage so the orchestrator's archive-keep logic works.
    """
    archive = Path(archive_path)
    if not archive.is_file():
        log_fn(f"Collection install: archive not found: {archive_path}")
        return None
    staging_root = game.get_effective_mod_staging_path()
    if staging_root is None:
        log_fn("Collection install: no staging folder configured.")
        return None
    staging_root = Path(staging_root)

    # Extract + FOMOD-detect via the shared prepare step (kept temp dir).
    prepared = prepare_archive(
        str(archive), game, profile_dir, log_fn=log_fn, progress_fn=progress_fn,
        preferred_name=preferred_name, prebuilt_meta=prebuilt_meta)
    if prepared is None:
        return None

    def _pp(done, total, phase=None):
        if progress_fn is not None:
            progress_fn(done, total, phase)

    is_fomod_install = False
    file_list: "list[tuple[str, str, bool]] | None" = None
    stage_src_root = str(prepared.extract_dir)   # copier's src root
    cancelled = False

    try:
        # ---- FOMOD --------------------------------------------------------
        if prepared.is_fomod():
            config = prepared.fomod_config
            fomod_base = prepared.fomod_base
            # resolve_files returns paths relative to the FOMOD BASE (the folder
            # containing fomod/ModuleConfig.xml), NOT the raw extract dir. When
            # the archive wraps the FOMOD in a top-level folder (e.g. "WTNC
            # Config/fomod/…"), fomod_base != extract_dir, so the copier must
            # stage from fomod_base or every source path is off by the wrapper
            # prefix → 0 files staged. (This is what _install_fomod does for the
            # manual path; the collection path previously left stage_src_root as
            # extract_dir, silently dropping nested-FOMOD collection mods.)
            stage_src_root = str(fomod_base)
            installed_files, active_files, loose_files = _collection_plugin_context(
                game, profile_dir)
            try:
                from Utils.fomod_installer import (
                    resolve_files, check_module_dependencies)
                ok, msg = check_module_dependencies(
                    config, installed_files, active_files, loose_files)
                if not ok:
                    log_fn("WARNING: this mod's <moduleDependencies> gate is not "
                           "satisfied — it may not work until these are met:")
                    for line in (msg or "").splitlines():
                        log_fn(f"  {line}")
            except Exception:
                resolve_files = None  # type: ignore

            if fomod_auto_selections is None and defer_interactive_fomod:
                log_fn("FOMOD installer detected — deferring until dependencies "
                       "are installed.")
                prepared.cleanup()
                return FOMOD_DEFERRED

            if fomod_auto_selections is not None:
                log_fn("FOMOD installer detected — applying collection author's "
                       "choices automatically.")
                final_selections = fomod_auto_selections
            elif resolve_fomod is not None:
                log_fn("FOMOD installer detected — opening wizard...")
                saved_sel = _read_saved_fomod_selections(
                    game, prepared.mod_name, log_fn)
                final_selections = resolve_fomod(
                    config, fomod_base, prepared.mod_name,
                    installed_files, active_files, loose_files, saved_sel)
                if final_selections is None:
                    log_fn("FOMOD install cancelled.")
                    cancelled = True
                else:
                    # Interactive wizard result → also remember it globally for
                    # the next install of this mod (Tk parity; the profile
                    # mirror is written below for every non-cancelled path).
                    _persist_fomod_selection(game, prepared.mod_name,
                                             final_selections, profile=False)
            else:
                # No auto-selections and no resolver → FOMOD defaults (parity with
                # the non-interactive single-mod fallback).
                log_fn("FOMOD installer detected — using default/recommended options.")
                final_selections = None

            if not cancelled:
                _write_profile_fomod_selection(game, prepared.mod_name, final_selections)
                try:
                    if final_selections is None:
                        file_list = _default_fomod_file_list(
                            config, installed_files, active_files, loose_files, log_fn)
                    else:
                        file_list = resolve_files(
                            config, final_selections, installed_files,
                            active_files, loose_files)
                    is_fomod_install = True
                    log_fn(f"FOMOD complete — {len(file_list or [])} file(s) to install.")
                except Exception as exc:
                    log_fn(f"FOMOD resolve failed ({exc}) — installing verbatim.")
                    file_list = None
                    is_fomod_install = False

        # ---- BAIN ---------------------------------------------------------
        elif getattr(game, "supports_bain", True):
            from Utils.bain_installer import (
                detect_bain, resolve_bain_files, bain_unwrap_single_folder)
            bain_root = bain_unwrap_single_folder(str(prepared.extract_dir))
            bain_subpkgs = detect_bain(
                bain_root, extra_exts=getattr(game, "plugin_extensions", None))
            if bain_subpkgs:
                stage_src_root = bain_root
                default_names = [p.name for p in bain_subpkgs if p.default_selected]
                log_fn(f"BAIN package detected — {len(bain_subpkgs)} sub-package(s).")
                if bain_auto_selections is None and defer_interactive_bain:
                    log_fn("BAIN installer detected — deferring until other mods "
                           "are installed.")
                    prepared.cleanup()
                    return BAIN_DEFERRED
                if bain_auto_selections is not None:
                    selected = bain_auto_selections.get("selected", [])
                    log_fn("BAIN: applying exported selection automatically.")
                elif resolve_bain is not None:
                    result = resolve_bain(bain_subpkgs, bain_root, prepared.mod_name)
                    if result is None:
                        log_fn("BAIN install cancelled.")
                        cancelled = True
                        selected = []
                    else:
                        selected = result.get("selected", [])
                else:
                    selected = default_names
                    log_fn("BAIN: non-interactive install — using default selection.")
                if not cancelled:
                    _write_profile_bain_selection(
                        game, prepared.mod_name, {"selected": selected})
                    file_list = resolve_bain_files(bain_subpkgs, set(selected))
                    log_fn(f"BAIN complete — {len(selected)} sub-package(s), "
                           f"{len(file_list)} file(s) to install.")

        if cancelled:
            return None

        # ---- stage --------------------------------------------------------
        dest_root = staging_root / prepared.mod_name
        _preserved_endorsed = False
        if dest_root.exists():
            # Collections pre-disambiguate folder names, so a collision means a
            # genuine replace: silent when overwrite_existing is True/None.
            try:
                from Nexus.nexus_meta import read_meta
                _preserved_endorsed = bool(read_meta(dest_root / "meta.ini").endorsed)
            except Exception:
                _preserved_endorsed = False
            log_fn(f"Replacing existing mod folder: {prepared.mod_name}")
            shutil.rmtree(dest_root, ignore_errors=True)

        staging_root.mkdir(parents=True, exist_ok=True)
        if file_list is not None:
            # FOMOD / BAIN produced an explicit src→dst list.
            dest_root.mkdir(parents=True, exist_ok=True)
            _copy_file_list(file_list, stage_src_root, dest_root, log_fn)
        else:
            # Plain (non-FOMOD/BAIN) mod: normalise structure like the Tk direct
            # path. dinput mods (prebuilt_meta.root_folder) install verbatim.
            is_root = bool(getattr(prebuilt_meta, "root_folder", False))
            staged = stage_file_list(
                game, stage_src_root, is_root_install=is_root,
                mod_name=prepared.mod_name, on_need_prefix=None, log_fn=log_fn)
            if staged is None:
                # stage_file_list only returns None when a prefix was required
                # but no resolver was supplied — for a collection mod that means
                # the structure wasn't recognised. Log the extract contents so a
                # live re-run pinpoints why (the "missing mods" investigation).
                try:
                    _preview = []
                    for _r, _ds, _fs in os.walk(stage_src_root):
                        for _f in _fs:
                            _preview.append(os.path.relpath(
                                os.path.join(_r, _f), stage_src_root))
                            if len(_preview) >= 12:
                                break
                        if len(_preview) >= 12:
                            break
                    log_fn(f"Collection install: '{prepared.mod_name}' — "
                           f"stage_file_list returned None (structure not "
                           f"recognised). Extract root={stage_src_root}; "
                           f"first files: {_preview}")
                except Exception:
                    pass
                cancelled = True
            else:
                dest_root.mkdir(parents=True, exist_ok=True)
                if not staged:
                    log_fn(f"Collection install: '{prepared.mod_name}' — "
                           f"stage_file_list produced an EMPTY file list "
                           f"(0 files to copy).")
                _copy_file_list(staged, stage_src_root, dest_root, log_fn)
    finally:
        prepared.cleanup()

    if cancelled:
        shutil.rmtree(dest_root, ignore_errors=True)
        return None
    if not dest_root.is_dir() or not any(dest_root.iterdir()):
        log_fn(f"Collection install: nothing staged for '{prepared.mod_name}' "
               f"(file_list={'explicit' if file_list is not None else 'auto'}).")
        try:
            dest_root.rmdir()
        except OSError:
            pass
        return None

    _write_install_meta(dest_root, archive, game, log_fn,
                        prebuilt_meta=prebuilt_meta, endorsed=_preserved_endorsed)
    if not skip_index_update:
        _pp(0, 0, "Indexing")
        _update_indexes(game, profile_dir, prepared.mod_name, dest_root, log_fn)
    _add_to_modlist(profile_dir, prepared.mod_name, log_fn, preserve_position=False)
    _add_plugins(game, profile_dir, dest_root, log_fn)
    log_fn(f"Installed '{prepared.mod_name}'.")
    _fire_on_installed(on_installed, is_fomod_install)
    return prepared.mod_name


def _fire_on_installed(cb, is_fomod: bool) -> None:
    """Call *cb* as either ``cb(is_fomod)`` or ``cb()`` (parity with Tk's
    ``_fire_on_installed``)."""
    if cb is None:
        return
    try:
        import inspect
        params = inspect.signature(cb).parameters
        if params:
            cb(is_fomod)
        else:
            cb()
    except (TypeError, ValueError):
        try:
            cb()
        except Exception:
            pass
    except Exception:
        pass


def _default_fomod_file_list(config, installed_files, active_files, loose_files,
                             log_fn: LogFn) -> "list[tuple[str, str, bool]]":
    """Resolve a FOMOD's file list using its default/recommended selections
    (threading flag state through steps), passing the collection context sets."""
    from Utils.fomod_installer import (
        resolve_files, get_default_selections, update_flags)
    selections: dict = {}
    flag_state: dict = {}
    for i, step in enumerate(getattr(config, "steps", []) or []):
        sels = get_default_selections(step, flag_state, installed_files)
        selections[str(i)] = sels
        flag_state = update_flags(step, sels, flag_state)
    return resolve_files(config, selections, installed_files, active_files, loose_files)


def _write_profile_fomod_selection(game, mod_name: str, selections) -> None:
    """Mirror FOMOD selections into ``<profile>/fomod/<mod>.json`` (Tk parity —
    profile-scoped, never the global config, so collection choices don't clobber
    the user's manual selections). No-op when selections is None (defaults)."""
    if selections is None:
        return
    pdir = getattr(game, "_active_profile_dir", None)
    if not pdir:
        return
    try:
        import json
        pfomod = Path(pdir) / "fomod"
        pfomod.mkdir(parents=True, exist_ok=True)
        with open(pfomod / f"{mod_name}.json", "w", encoding="utf-8") as f:
            json.dump(selections, f, indent=2)
    except OSError:
        pass


def _write_profile_bain_selection(game, mod_name: str, result) -> None:
    """Mirror BAIN selection into ``<profile>/bain/<mod>.json`` (Tk parity)."""
    pdir = getattr(game, "_active_profile_dir", None)
    if not pdir:
        return
    try:
        import json
        pbain = Path(pdir) / "bain"
        pbain.mkdir(parents=True, exist_ok=True)
        with open(pbain / f"{mod_name}.json", "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
    except OSError:
        pass


def _read_bain_readme(bain_root: str) -> "str | None":
    """Return the text of a package readme at *bain_root* (``package.txt`` or
    ``readme.txt``, case-insensitive) — shown alongside the BAIN picker. A Wrye
    Bash installer *script* (``wizard.txt``) is deliberately NOT treated as a
    readme (Tk parity)."""
    try:
        root_files = {e.name.lower(): e.path
                      for e in os.scandir(bain_root) if e.is_file()}
    except OSError:
        return None
    for rn in ("package.txt", "readme.txt"):
        rp = root_files.get(rn)
        if rp:
            try:
                return Path(rp).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
    return None


def _read_saved_fomod_selections(game, mod_name: str, log_fn: LogFn) -> "dict | None":
    """Load a previously-saved FOMOD selection for *mod_name* (global config) so
    the wizard restores + highlights the user's last choices (Tk parity)."""
    game_name = getattr(game, "name", "")
    if not game_name:
        return None
    try:
        import json
        from Utils.config_paths import get_fomod_selections_path
        sel_path = get_fomod_selections_path(game_name, mod_name)
        if sel_path.is_file():
            with open(sel_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            log_fn("Restored previous FOMOD selections.")
            return data
    except (OSError, ValueError):
        pass
    return None


def _persist_fomod_selection(game, mod_name: str, selections,
                             profile: bool = True) -> None:
    """Write the wizard's selections to the global per-game JSON (restored on
    the next install of this mod) and optionally mirror them into the profile
    (Tk parity: interactive installs write both; the collection orchestrator
    mirrors to the profile itself, so it passes profile=False)."""
    if selections is None:
        return
    game_name = getattr(game, "name", "")
    if game_name:
        try:
            import json
            from Utils.config_paths import get_fomod_selections_path
            sel_path = get_fomod_selections_path(game_name, mod_name)
            with open(sel_path, "w", encoding="utf-8") as f:
                json.dump(selections, f, indent=2)
        except OSError:
            pass
    if profile:
        _write_profile_fomod_selection(game, mod_name, selections)


def _read_saved_bain_selections(game, mod_name: str, log_fn: LogFn) -> "dict | None":
    """Load a previously-saved BAIN selection for *mod_name* (global config) so
    the picker restores the user's last choices (Tk parity)."""
    game_name = getattr(game, "name", "")
    if not game_name:
        return None
    try:
        import json
        from Utils.config_paths import get_bain_selections_path
        sel_path = get_bain_selections_path(game_name, mod_name)
        if sel_path.is_file():
            with open(sel_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            log_fn("Restored previous BAIN selections.")
            return data
    except (OSError, ValueError, Exception):
        pass
    return None


def _persist_bain_selection(game, mod_name: str, result) -> None:
    """Persist a BAIN selection to BOTH the global config
    (``get_bain_selections_path``) and the profile (``<profile>/bain/<mod>.json``),
    matching the Tk installer."""
    _write_profile_bain_selection(game, mod_name, result)
    game_name = getattr(game, "name", "")
    if not game_name:
        return
    try:
        import json
        from Utils.config_paths import get_bain_selections_path
        sel_path = get_bain_selections_path(game_name, mod_name)
        sel_path.parent.mkdir(parents=True, exist_ok=True)
        with open(sel_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------- helpers
def _clean_mod_name(stem: str, game) -> str:
    """Best-effort folder name from the archive stem.

    Uses the same derivation as the Tk installer: ``_suggest_mod_names(stem)[0]``
    — the *least-destructive* candidate, which strips only the Nexus id/version
    tail (and the new underscore ``_<version>_<slug>`` tail / mod.io UUID tail)
    while preserving the actual title, including meaningful parentheses and
    edition tags (``(SE)``, ``(CP)``, ``(Black)`` …).  ``_strip_title_metadata``
    is deliberately NOT used here: it aggressively removes those and was demoted
    from the Tk default because it silently destroyed real titles.
    """
    name = stem
    try:
        from gui.mod_name_utils import _suggest_mod_names, sanitize_mod_folder_name
        suggestions = _suggest_mod_names(stem)
        name = (suggestions[0] if suggestions else stem) or stem
        name = sanitize_mod_folder_name(name) or name
    except Exception:
        # mod_name_utils may pull Tk transitively — fall back to a basic clean.
        import re
        name = re.sub(r"-\d+-.*$", "", stem).strip() or stem
        name = re.sub(r'[<>:"/\\|?*]', "_", name).rstrip(". ")
    return name or stem


def _copy_tree(src_root: Path, dest_root: Path, log_fn: LogFn, _p) -> None:
    files = [p for p in src_root.rglob("*") if p.is_file()]
    total = len(files)
    dest_root.mkdir(parents=True, exist_ok=True)
    for i, f in enumerate(files, 1):
        rel = f.relative_to(src_root)
        # Skip installer-only fomod/ metadata in a plain copy.
        if rel.parts and rel.parts[0].lower() == "fomod":
            continue
        target = dest_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        if i % 25 == 0 or i == total:
            _p(i, total, "Copying files")


def _install_fomod(fomod_base: Path, config, dest_root: Path,
                   selections, log_fn: LogFn, _p,
                   context: "tuple[set, set, set] | None" = None) -> bool:
    """Stage a FOMOD's files. *selections* is the wizard's
    {step_idx_str: {group_name: [plugin_names]}} dict; None → the FOMOD's own
    default selections. *context* is the (installed, active, loose) file sets
    its conditions evaluate against. Uses the neutral `resolve_files` to map
    src→dst and apply requiredInstallFiles + conditional installs. Returns
    False on failure (caller falls back to verbatim copy)."""
    try:
        from Utils.fomod_installer import (
            resolve_files, get_default_selections, update_flags)
    except Exception as exc:
        log_fn(f"FOMOD installer unavailable ({exc}).")
        return False
    installed, active, loose = context or (set(), set(), set())

    if not selections:
        # Build default selections per step, threading flag state through.
        selections = {}
        flag_state: dict = {}
        for i, step in enumerate(getattr(config, "steps", []) or []):
            sels = get_default_selections(step, flag_state, installed,
                                          active, loose)
            selections[str(i)] = sels
            flag_state = update_flags(step, sels, flag_state)
        log_fn("FOMOD: using default/recommended options.")

    try:
        # [(src_rel, dst_rel, is_folder)]
        files = resolve_files(config, selections, installed, active, loose)
    except Exception as exc:
        log_fn(f"FOMOD resolve_files failed ({exc}).")
        return False
    if not files:
        return False

    _p(0, 0, "Installing FOMOD files")
    # Use the SHARED, proven copier (case-insensitive resolution + dst dedup +
    # priority "later wins"). fomod_base is the src root; resolve_files already
    # produced the src→dst mapping.
    _copy_file_list(files, str(fomod_base), dest_root, log_fn)
    return any(dest_root.iterdir())


def _write_install_meta(dest_root: Path, archive: Path, game, log_fn: LogFn,
                        prebuilt_meta=None, endorsed: bool = False,
                        is_bain: bool = False) -> None:
    try:
        from Nexus.nexus_meta import (
            write_meta, resolve_nexus_meta_for_archive, NexusModMeta)
        from datetime import datetime
        meta_path = dest_root / "meta.ini"
        meta = None
        domain = getattr(game, "nexus_game_domain", None) or getattr(game, "game_id", "")
        if prebuilt_meta is not None:
            # The caller (Nexus browser) knows the real mod_id/file_id — use it
            # verbatim instead of parsing the archive filename, which can be
            # wrong for mods whose name/version embeds digits.
            meta = prebuilt_meta
        else:
            try:
                meta = resolve_nexus_meta_for_archive(archive, domain, log_fn=log_fn)
            except Exception:
                meta = None
        if meta is None:
            meta = NexusModMeta()
        # Always stamp the local install fields.
        meta.installation_file = archive.name
        if not getattr(meta, "installed", ""):
            meta.installed = datetime.now().isoformat(timespec="seconds")
        # Carry the endorsed flag from a replaced install (Tk parity).
        if endorsed:
            meta.endorsed = True
        # Stamp the BAIN install-method flag so the modlist BAIN filter / is_bain
        # set picks it up (Tk parity).
        if is_bain:
            meta.is_bain = True
        write_meta(meta_path, meta)
    except Exception as exc:
        log_fn(f"meta.ini write skipped ({exc}).")


def _update_indexes(game, profile_dir: Path, mod_name: str, dest_root: Path,
                    log_fn: LogFn) -> None:
    try:
        from Utils.filemap import _scan_dir, update_mod_index
        strip = frozenset(s.lower() for s in (getattr(game, "strip_prefixes", None) or []))
        exts = frozenset(e.lower() for e in (getattr(game, "install_extensions", None) or []))
        root = frozenset(s.lower() for s in (getattr(game, "root_deploy_folders", None) or []))
        # _scan_dir returns (name, normal, root, extras); take the first three.
        scan = _scan_dir(mod_name, str(dest_root), strip, exts, root)
        normal_files, root_files = scan[1], scan[2]
        # The index MUST live where build_filemap reads it — next to the
        # effective filemap (= staging.parent / game root), NOT the profile dir.
        # Writing it to the profile dir leaves a fresh install invisible to the
        # filemap rebuild → no conflicts detected (the bug this fixes).
        try:
            index_dir = game.get_effective_filemap_path().parent
        except Exception:
            index_dir = profile_dir
        index_path = index_dir / "modindex.bin"
        norm_case = getattr(game, "normalize_folder_case", True)
        update_mod_index(index_path, mod_name, normal_files, root_files,
                         normalize_folder_case=norm_case)
        archive_exts = frozenset(getattr(game, "archive_extensions", frozenset()) or frozenset())
        if archive_exts:
            from Utils.bsa_filemap import update_bsa_index
            update_bsa_index(index_dir / "bsa_index.bin", mod_name, dest_root, archive_exts)
    except Exception as exc:
        log_fn(f"index update skipped ({exc}) — next rebuild will rescan.")


def _add_to_modlist(profile_dir: Path, mod_name: str, log_fn: LogFn,
                    preserve_position: bool = False) -> None:
    try:
        if preserve_position:
            # Replacing an existing mod — keep its load-order position.
            from Utils.modlist import ensure_mod_preserving_position
            ensure_mod_preserving_position(
                profile_dir / "modlist.txt", mod_name, enabled=True)
        else:
            from Utils.modlist import prepend_mod
            prepend_mod(profile_dir / "modlist.txt", mod_name, enabled=True)
    except Exception as exc:
        log_fn(f"modlist update failed ({exc}).")


def _add_plugins(game, profile_dir: Path, dest_root: Path, log_fn: LogFn) -> None:
    exts = [e.lower() for e in (getattr(game, "plugin_extensions", []) or [])]
    if not exts:
        return
    try:
        from Utils.plugins import append_plugin
        star = getattr(game, "plugins_use_star_prefix", True)
        plugins_path = profile_dir / "plugins.txt"
        for entry in sorted(dest_root.iterdir()):
            if entry.is_file() and entry.suffix.lower() in exts:
                append_plugin(plugins_path, entry.name, enabled=True, star_prefix=star)
    except Exception as exc:
        log_fn(f"plugins update skipped ({exc}).")
