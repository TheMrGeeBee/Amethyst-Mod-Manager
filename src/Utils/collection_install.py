"""Toolkit-neutral automatic (premium) Nexus collection install (no Tk / no Qt).

A faithful port of ``gui/collections_dialog.py:_run_install`` — the premium
download→install pipeline — with the Tk widget calls replaced by a callback
interface and the Tk-only ``install_mod_from_archive`` replaced by the neutral
``Utils.mod_install.install_collection_archive``. The heavy backend
(``fomod_installer``/``bain_installer``/``nexus_download``/``collection_reset``/
``nexus_meta``/``loot_sorter``) is reused verbatim.

Load order is driven by ``collection.json`` from the collection archive: the
``mods`` array (via ``_resolve_collection_priorities``) defines install order,
the ``plugins`` array defines plugins.txt order. Both are written after all mods
install.

The caller (Qt) constructs :class:`CollectionInstallCallbacks` (each a single
``Signal.emit`` marshaling to a UI-thread slot) + :class:`CollectionInstallControl`
(cancel/pause/stop Events) and runs :func:`run_collection_install` on a daemon
thread. Interactive FOMOD/BAIN mods with no author selections are DEFERRED to the
end and resolved one-by-one via ``callbacks.resolve_fomod`` / ``resolve_bain``.

v1 wires the NEW-profile path (the primary flow). Append/update reconcile helpers
are ported but not yet wired (see ``overwrite_existing`` / ``update_context``).
"""

from __future__ import annotations

import json
import queue as _queue
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from Utils.collection_reset import (
    _resolve_collection_priorities, _apply_collection_groups)
from Utils.config_paths import get_download_cache_dir_for_game, list_all_cache_dirs
from Utils.download_locations import (
    is_default_downloads_disabled, load_extra_download_locations)
from Utils.download_scheduler import order_by_size, run_double_ended
from Utils.extract_budget import ExtractionMemoryBudget, get_uncompressed_size
from Utils.mod_install import (
    install_collection_archive, FOMOD_DEFERRED, BAIN_DEFERRED)
from Utils.modlist import read_modlist, write_modlist, ModEntry
from Utils.plugins import write_plugins, write_loadorder, PluginEntry
from Utils.ui_config import (
    load_collection_settings, load_clear_archive_after_install,
    load_keep_fomod_archives)
from Nexus.nexus_download import (
    DownloadResult, _find_cached_archive, delete_archive_and_sidecar,
    _get_downloads_dir)
from Nexus.nexus_meta import build_meta_from_download


# Module-level registries so a Resume/reconnect can find an in-flight install by
# slug (mirrors the Tk _ACTIVE_INSTALLS / _PAUSED_INSTALLS; harmless, tk-free).
_ACTIVE_INSTALLS: "dict[str, dict]" = {}
_PAUSED_INSTALLS: "dict[str, dict]" = {}


# ---------------------------------------------------------------------------
# Pure map helpers moved verbatim from gui/collections_dialog.py (imported back
# there to keep ONE implementation). No Tk.
# ---------------------------------------------------------------------------
def _build_collision_suffix_map(
    schema_mods: "list[dict]",
    schema_file_id_to_logical: "dict[int, str]",
    schema_pos_to_name: "dict[int, str]",
    schema_file_id_to_pos: "dict[int, int]",
) -> "dict[int, str]":
    """Return file_id → suffix to append when multiple collection entries from
    different mod pages would otherwise install into the same folder. Returns ""
    for non-colliding entries; the suffix string for colliders."""
    base_to_fids: dict[str, list[int]] = {}
    fid_to_base: dict[int, str] = {}
    fid_to_mod_id: dict[int, int] = {}
    for sm in schema_mods:
        src = sm.get("source") or {}
        fid = src.get("fileId")
        if fid is None:
            continue
        fid = int(fid)
        logical = schema_file_id_to_logical.get(fid, "") or ""
        schema_name = schema_pos_to_name.get(
            schema_file_id_to_pos.get(fid, -1), "") or ""
        base = (logical or schema_name or sm.get("name") or "").strip()
        if not base:
            continue
        fid_to_base[fid] = base
        mid = src.get("modId")
        if mid:
            fid_to_mod_id[fid] = int(mid)
        base_to_fids.setdefault(base.lower(), []).append(fid)

    result: dict[int, str] = {}
    for fid, base in fid_to_base.items():
        siblings = base_to_fids.get(base.lower(), [])
        if len(siblings) <= 1:
            result[fid] = ""
            continue
        sibling_mod_ids = {fid_to_mod_id.get(s) for s in siblings}
        sibling_mod_ids.discard(None)
        if len(sibling_mod_ids) <= 1:
            result[fid] = ""
            continue
        mod_id = fid_to_mod_id.get(fid)
        result[fid] = f" ({mod_id})" if mod_id else ""
    return result


def _fomod_choices_from_collection(choices: dict) -> "dict[str, dict[str, list[str]]]":
    """Convert a collection.json FOMOD ``choices`` block to the saved_selections
    format ``{str(step_idx): {group: [plugins]}}`` that ``resolve_files`` expects."""
    result: dict = {}
    for step_idx, step in enumerate(choices.get("options", [])):
        groups: dict = {}
        for group in step.get("groups", []):
            group_name = group.get("name", "")
            plugin_names = [c["name"] for c in group.get("choices", []) if c.get("name")]
            if plugin_names:
                groups[group_name] = plugin_names
        if groups:
            result[str(step_idx)] = groups
    return result


# ---------------------------------------------------------------------------
# Callback / control interface (the Qt caller wires each to a Signal.emit).
# ---------------------------------------------------------------------------
def _noop(*_a, **_k):
    return None


@dataclass
class CollectionInstallCallbacks:
    on_status: Callable[[str], None] = _noop            # status line text
    on_progress: Callable[["float | None"], None] = _noop  # 0..1 or None=hide
    on_agg_download: Callable[[int, int, float], None] = _noop  # bytes cur,total,MB/s
    on_display_total: Callable[[int], None] = _noop     # true collection size (bytes)
    # RED — active downloads
    on_dl_mod_start: Callable[[int, str, int], None] = _noop   # file_id,name,size
    on_dl_mod_update: Callable[[int, int, int], None] = _noop  # file_id,cur,tot
    on_dl_mod_finish: Callable[[int], None] = _noop            # file_id
    # GREEN — extracting/queued
    on_extract_queue: Callable[[int, str], None] = _noop       # file_id,name
    on_extract_add: Callable[[int, str], None] = _noop
    on_extract_remove: Callable[[int], None] = _noop
    on_row_installed: Callable[[int], None] = _noop            # file_id landed
    # logging / lifecycle
    on_log: Callable[[str], None] = _noop
    on_done: Callable[[int, int, int, str], None] = _noop      # installed,skipped,total,profile
    on_paused: Callable[[int, str], None] = _noop              # installed,profile
    on_cancelled: Callable[[object], None] = _noop             # profile_dir (Path)
    # interactive resolvers (BLOCK the worker; caller marshals a wizard)
    resolve_fomod: "Callable | None" = None   # (config, base, name, inst, act, loose, saved) -> dict|None
    resolve_bain: "Callable | None" = None     # (subpkgs, root, name) -> {"selected":[...]}|None


@dataclass
class CollectionInstallControl:
    cancel: threading.Event = field(default_factory=threading.Event)
    pause: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)  # set by BOTH pause & cancel


# ---------------------------------------------------------------------------
# Cancel cleanup (neutral body of _do_cancel_cleanup; the Tk topbar tail is the
# caller's job).
# ---------------------------------------------------------------------------
def cleanup_cancelled_install(game, profile_dir: "Path | None", *,
                              clear_cache: bool = False, log_fn=_noop) -> None:
    """Restore any deployed files, delete the collection profile dir, and
    optionally clear this game's download cache."""
    import shutil
    if profile_dir is not None and Path(profile_dir).is_dir() and game is not None \
            and getattr(game, "is_configured", lambda: True)():
        try:
            game.set_active_profile_dir(Path(profile_dir))
            game.load_paths()
            if hasattr(game, "restore"):
                game.restore()
        except Exception as exc:
            log_fn(f"Cancel: restore failed: {exc}")
        try:
            from Utils.deploy import restore_root_folder
            root_folder_dir = game.get_effective_root_folder_path()
            game_root = game.get_game_path()
            if root_folder_dir.is_dir() and game_root:
                restore_root_folder(
                    root_folder_dir, game_root,
                    data_deploy_dirs=game.root_restore_protect_dirs()
                    if hasattr(game, "root_restore_protect_dirs") else None,
                )
        except Exception as exc:
            log_fn(f"Cancel: restore_root_folder failed: {exc}")
        try:
            game.set_active_profile_dir(None)
            game.load_paths()
        except Exception:
            pass
    if profile_dir is not None and Path(profile_dir).is_dir():
        try:
            shutil.rmtree(str(profile_dir))
            log_fn(f"Cancel: deleted profile dir {profile_dir}")
        except Exception as exc:
            log_fn(f"Cancel: failed to delete profile dir: {exc}")
    if clear_cache:
        try:
            game_cache = get_download_cache_dir_for_game(getattr(game, "name", "") or "")
            if game_cache and game_cache.is_dir():
                for item in game_cache.iterdir():
                    try:
                        if item.is_file() or item.is_symlink():
                            item.unlink()
                        elif item.is_dir():
                            shutil.rmtree(str(item), ignore_errors=True)
                    except Exception:
                        pass
                log_fn("Cancel: cleared download cache")
        except Exception as exc:
            log_fn(f"Cancel: failed to clear download cache: {exc}")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def run_collection_install(
        *, game, api, downloader, mods: list, download_link_path: str,
        profile_dir: Path, old_profile_dir: "Path | None",
        collection_slug: str, revision_number: "int | None" = None,
        collection_total_size: int = 0,
        collection_schema_cache: "dict | None" = None,
        overwrite_existing: "bool | None" = None,
        skipped_fids: "set[int] | None" = None,
        skipped_mods: "list | None" = None,
        skip_existing: bool = False,
        with_bundled: bool = True,
        update_context: "dict | None" = None,
        callbacks: "CollectionInstallCallbacks | None" = None,
        control: "CollectionInstallControl | None" = None) -> None:
    """Download then install every mod in *mods* in collection-defined order.

    Faithful port of ``CollectionsDialog._run_install`` — see module docstring.
    ``overwrite_existing``: None=new-profile install (the wired v1 path); a bool
    selects the append path (ported but not yet exercised by the Qt caller).
    ``update_context``: when set (a collection UPDATE — continue semantics, so
    ``overwrite_existing`` stays None), the final modlist write uses the
    order-preserving ``_reconcile_update_modlist`` (snapshot + schema-neighbour
    insertion) instead of the new-profile reconcile.
    """
    cb = callbacks or CollectionInstallCallbacks()
    ctl = control or CollectionInstallControl()
    log = cb.on_log
    game_domain = (getattr(game, "nexus_game_domain", None)
                   or getattr(game, "game_id", "") or "")

    _slug = collection_slug or ""
    _install_state: dict = {"status": "", "installed_fids": set(), "done": False,
                            "profile_dir": profile_dir}
    if _slug:
        _ACTIVE_INSTALLS[_slug] = _install_state

    def _set_status(msg: str) -> None:
        _install_state["status"] = msg
        cb.on_status(msg)

    def _set_progress(value):
        cb.on_progress(value)

    game.set_active_profile_dir(profile_dir)
    game.load_paths()
    modlist_path = profile_dir / "modlist.txt"
    plugins_path = profile_dir / "plugins.txt"
    staging_path = game.get_effective_mod_staging_path()
    installed = 0
    skipped = 0
    total = len(mods)

    _is_append_run = overwrite_existing is not None
    _append_pre_existing: "set[str]" = set()
    if _is_append_run and modlist_path.is_file():
        try:
            _append_pre_existing = {
                e.name.lower() for e in read_modlist(modlist_path)
                if not e.is_separator
            }
        except Exception:
            _append_pre_existing = set()

    # ------------------------------------------------------------------
    # Step 1: fetch/parse collection.json for authoritative order
    # ------------------------------------------------------------------
    collection_schema: dict = {}
    if collection_schema_cache:
        collection_schema = collection_schema_cache
        log("Collection install: reusing cached collection.json")
    if download_link_path and not collection_schema:
        _set_status("Downloading collection manifest…")
        try:
            collection_schema = api.get_collection_archive_json(download_link_path)
            log(f"Collection install: parsed collection.json "
                f"({len(collection_schema.get('mods', []))} mod entries, "
                f"{len(collection_schema.get('plugins', []))} plugins)")
        except Exception as exc:
            log(f"Collection install: could not download collection.json: {exc} — "
                "continuing with GraphQL order")

    if collection_schema:
        try:
            (profile_dir / "collection.json").write_text(
                json.dumps(collection_schema, indent=2), encoding="utf-8")
            log(f"Collection install: saved manifest to {profile_dir / 'collection.json'}")
        except Exception as exc:
            log(f"Collection install: could not save manifest: {exc}")

    schema_mods: list[dict] = collection_schema.get("mods", [])
    schema_file_id_to_pos: dict[int, int] = _resolve_collection_priorities(collection_schema)
    schema_pos_to_name: dict[int, str] = {}
    schema_file_id_to_logical: dict[int, str] = {}
    schema_file_id_to_mod_id: dict[int, int] = {}
    schema_file_id_to_install_type: dict[int, str] = {}
    schema_file_id_to_phase: dict[int, int] = {}
    fomod_by_file_id: dict[int, dict] = {}
    bain_by_file_id: dict[int, dict] = {}
    _raw_logical: dict[int, str] = {}
    _raw_name: dict[int, str] = {}
    for schema_mod in schema_mods:
        src = schema_mod.get("source") or {}
        fid = src.get("fileId")
        if fid is not None:
            fid = int(fid)
            _raw_logical[fid] = src.get("logicalFilename") or ""
            _raw_name[fid] = schema_mod.get("name") or ""
    _logical_counts: dict[str, int] = {}
    for raw in _raw_logical.values():
        if raw:
            _logical_counts[raw] = _logical_counts.get(raw, 0) + 1

    for pos, schema_mod in enumerate(schema_mods):
        src = schema_mod.get("source") or {}
        fid = src.get("fileId")
        if fid is not None:
            fid = int(fid)
            topo_pos = schema_file_id_to_pos.get(fid, pos)
            schema_pos_to_name[topo_pos] = schema_mod.get("name") or ""
            raw_logical = _raw_logical.get(fid, "")
            schema_name = _raw_name.get(fid, "")
            if raw_logical and _logical_counts.get(raw_logical, 0) > 1:
                logical = schema_name or raw_logical
            else:
                logical = raw_logical or schema_name
            schema_file_id_to_logical[fid] = logical
            mid = src.get("modId")
            if mid:
                schema_file_id_to_mod_id[fid] = int(mid)
            _det_type = ((schema_mod.get("details") or {}).get("type") or "").strip()
            if _det_type:
                schema_file_id_to_install_type[fid] = _det_type
            try:
                schema_file_id_to_phase[fid] = int(schema_mod.get("phase") or 0)
            except (TypeError, ValueError):
                schema_file_id_to_phase[fid] = 0
            choices = schema_mod.get("choices") or {}
            if choices.get("type") == "fomod":
                fomod_by_file_id[fid] = _fomod_choices_from_collection(choices)
            elif choices.get("type") == "fomod_selections":
                fomod_by_file_id[fid] = choices["selections"]
            elif choices.get("type") == "bain_selections":
                bain_by_file_id[fid] = choices["selections"]

    def _sort_key(m):
        return schema_file_id_to_pos.get(m.file_id, len(schema_mods))

    ordered_mods = sorted(mods, key=_sort_key)

    schema_file_id_to_suffix: dict[int, str] = _build_collision_suffix_map(
        schema_mods, schema_file_id_to_logical, schema_pos_to_name,
        schema_file_id_to_pos)

    # ------------------------------------------------------------------
    # Step 2 pre-scan staging for already-installed mods
    # ------------------------------------------------------------------
    already_installed_by_ids: dict[tuple[int, int], str] = {}
    already_installed_by_fid: dict[int, str] = {}
    staging_lower_map: dict[str, str] = {}

    _profile_mod_names: set[str] = set()
    if modlist_path.is_file():
        try:
            for entry in read_modlist(modlist_path):
                _profile_mod_names.add(entry.name.lower())
        except Exception:
            pass

    import configparser as _cp
    if staging_path.exists():
        for mod_dir in staging_path.iterdir():
            if not mod_dir.is_dir():
                continue
            if mod_dir.name.lower() in _profile_mod_names:
                staging_lower_map[mod_dir.name.lower()] = mod_dir.name
            meta_ini = mod_dir / "meta.ini"
            if not meta_ini.is_file():
                continue
            try:
                _parser = _cp.ConfigParser()
                _parser.read(str(meta_ini), encoding="utf-8")
                fid_str = _parser.get("General", "fileid", fallback="").strip()
                mid_str = _parser.get("General", "modid", fallback="").strip()
                if fid_str and fid_str != "0":
                    if skip_existing and mod_dir.name.lower() not in _profile_mod_names:
                        continue
                    _fid = int(fid_str)
                    _mid = int(mid_str) if mid_str.isdigit() else 0
                    if _mid > 0:
                        already_installed_by_ids[(_mid, _fid)] = mod_dir.name
                    else:
                        already_installed_by_fid[_fid] = mod_dir.name
            except Exception:
                pass

    def _match_existing(mod) -> str:
        _mid = schema_file_id_to_mod_id.get(mod.file_id, 0) or getattr(mod, "mod_id", 0) or 0
        if _mid > 0 and (_mid, mod.file_id) in already_installed_by_ids:
            return already_installed_by_ids[(_mid, mod.file_id)]
        return already_installed_by_fid.get(mod.file_id, "")

    def _name_candidates(mod) -> "list[str]":
        from Utils.mod_name_utils import _suggest_mod_names
        logical = schema_file_id_to_logical.get(mod.file_id, "") or ""
        schema_name = schema_pos_to_name.get(
            schema_file_id_to_pos.get(mod.file_id, -1), "") or ""
        candidates: list[str] = []
        name_sources = (logical, schema_name) if (logical or schema_name) \
            else (mod.mod_name or "",)
        for raw in name_sources:
            if raw:
                for s in _suggest_mod_names(raw):
                    if s and s not in candidates:
                        candidates.append(s)
        return candidates

    # Remove staging folders for unticked optional mods
    if skipped_fids and skipped_mods:
        import shutil as _shutil_skip
        _removed_folders: list[str] = []
        for mod in skipped_mods:
            if not mod.file_id or mod.file_id not in skipped_fids:
                continue
            folder_name = _match_existing(mod)
            if not folder_name:
                for candidate in _name_candidates(mod):
                    key = candidate.lower()
                    if key in staging_lower_map:
                        folder_name = staging_lower_map[key]
                        break
            if folder_name:
                skip_dir = staging_path / folder_name
                if skip_dir.is_dir():
                    log(f"Collection install: removing unticked optional mod "
                        f"'{folder_name}' (file_id={mod.file_id})")
                    try:
                        _shutil_skip.rmtree(skip_dir)
                        _removed_folders.append(folder_name)
                    except Exception as exc:
                        log(f"Collection install: failed to remove '{folder_name}': {exc}")
        if _removed_folders and modlist_path.is_file():
            try:
                _removed_set = set(_removed_folders)
                entries = [e for e in read_modlist(modlist_path)
                           if e.name not in _removed_set]
                write_modlist(modlist_path, entries)
            except Exception:
                pass

    install_order: list[tuple[int, str]] = []
    to_download: list = []

    # Per-mod outcome tracker for the end-of-install verification summary. Maps
    # file_id -> {name, status, detail}. Every mod that should end up staged is
    # recorded so we can loudly report any that silently fell out of the pipeline
    # (the "N mods missing" bug). status ∈ existing/queued/installed/deferred/
    # download_failed/stage_empty/error/no_file_id.
    _mod_outcomes: "dict[int, dict]" = {}

    def _record_outcome(mod, status, detail=""):
        fid = getattr(mod, "file_id", 0) or 0
        _mod_outcomes[fid] = {"name": getattr(mod, "mod_name", "") or "",
                              "mod_id": getattr(mod, "mod_id", 0) or 0,
                              "status": status, "detail": detail}

    # Classify: already-installed (skip) vs needs downloading
    for mod in ordered_mods:
        if not mod.file_id:
            log(f"Collection install: skipping '{mod.mod_name}' — no file ID")
            _record_outcome(mod, "no_file_id")
            skipped += 1
            continue
        existing_folder: str = _match_existing(mod)
        if not existing_folder:
            for candidate in _name_candidates(mod):
                key = candidate.lower()
                if key in staging_lower_map:
                    existing_folder = staging_lower_map[key]
                    break
        if existing_folder:
            log(f"Collection install: '{mod.mod_name}' already installed as "
                f"'{existing_folder}' — skipping")
            _record_outcome(mod, "existing", existing_folder)
            if not skip_existing:
                install_order.append((_sort_key(mod), existing_folder))
            installed += 1
        else:
            _record_outcome(mod, "queued")
            to_download.append(mod)

    # ------------------------------------------------------------------
    # Step 2 pipeline: download + install concurrently (producer/consumer).
    # ------------------------------------------------------------------
    _col_cfg = load_collection_settings()
    _DL_WORKERS = _col_cfg["max_concurrent"]
    _INSTALL_WORKERS = _col_cfg.get("max_extract_workers", 4)
    # Decouple downloads from installs: size the hand-off queue so all download
    # workers can deposit a finished archive without blocking even when every
    # install worker is busy extracting. Downloaded archives live on disk; queue
    # items are cheap (mod, result) tuples, and _mem_budget still caps concurrent
    # extraction — so a generous queue lets the 8 download slots stay saturated
    # (matching Tk's observed behaviour) instead of stalling in bursts. Was
    # max(_INSTALL_WORKERS + 1, 5), which blocked producers once installs (slow
    # per-archive 7z spawns for many tiny mods) fell behind.
    _PIPELINE_QUEUE_SIZE = max(_DL_WORKERS + _INSTALL_WORKERS + 8, 32)
    _DONE_SENTINEL = None
    import os as _os_col
    _COL_TIMING = bool(_os_col.environ.get("MM_COL_TIMING"))

    _dl_results: dict[int, tuple] = {}
    _dl_lock = threading.Lock()
    _dl_done = 0
    _dl_total = len(to_download)

    _to_download_fids = {getattr(m, "file_id", None) for m in to_download}
    _total_bytes = sum(getattr(m, "size_bytes", 0) or 0 for m in ordered_mods)
    # The real collection size (installed/uncompressed = totalSize + assetsSizeBytes,
    # from get_collection_detail) is what the detail header shows and what the user
    # expects to see. The download bar tracks compressed archive bytes (_total_bytes),
    # which is much smaller, so surface the true size separately for the label.
    if collection_total_size > 0:
        cb.on_display_total(int(collection_total_size))
    _dl_bytes_done = sum(
        getattr(m, "size_bytes", 0) or 0 for m in ordered_mods
        if getattr(m, "file_id", None) not in _to_download_fids)
    _per_mod_prev: dict[int, int] = {}

    # Aggregate-download speed state (replaces the Tk after()-timer poll).
    import time as _time_mod
    _agg_state = {"prev_bytes": 0, "prev_time": _time_mod.monotonic(), "speed": 0.0,
                  "last_emit": 0.0}
    # Progress-emit throttle: NexusDownloader calls progress_cb per read (~every
    # few KB). Emitting a Signal per chunk (×N concurrent downloads) floods the Qt
    # event loop and the X server's shared-memory backing store → the desktop can
    # freeze (xcb_shm_create_segment failures). Cap emissions to ~10/sec each, as
    # the Tk version did via a 200ms after()-timer poll.
    _EMIT_INTERVAL = 0.1
    _dl_last_emit: dict[int, float] = {}

    _col_cancel = ctl.cancel
    _col_pause = ctl.pause
    _col_stop = ctl.stop
    _dl_finished = threading.Event()

    if _slug:
        _ACTIVE_INSTALLS[_slug]["cancel"] = _col_cancel
        _ACTIVE_INSTALLS[_slug]["pause"] = _col_pause
        _ACTIVE_INSTALLS[_slug]["stop"] = _col_stop

    _mem_budget = ExtractionMemoryBudget(max_workers=_INSTALL_WORKERS)
    _archive_use_count: dict[str, int] = {}
    _external_archive_paths: set[str] = set()

    _install_lock = threading.Lock()
    _install_counters = {"installed": 0, "skipped": 0, "done": 0}
    _install_results: dict[int, str] = dict(already_installed_by_fid)
    _install_results.update(
        {fid: folder for (_mid, fid), folder in already_installed_by_ids.items()})
    _fomod_deferred: list = []
    _bain_deferred: list = []

    _install_queue: _queue.Queue = _queue.Queue(maxsize=_PIPELINE_QUEUE_SIZE)

    def _agg_push(force: bool = False):
        now = _time_mod.monotonic()
        # Throttle emissions to ~10/sec (speed is still averaged over 0.5s).
        if not force and now - _agg_state["last_emit"] < _EMIT_INTERVAL:
            return
        _agg_state["last_emit"] = now
        with _dl_lock:
            agg = _dl_bytes_done
            total = _total_bytes
        dt = now - _agg_state["prev_time"]
        if dt >= 0.5:
            _agg_state["speed"] = (agg - _agg_state["prev_bytes"]) / dt
            _agg_state["prev_bytes"] = agg
            _agg_state["prev_time"] = now
        cb.on_agg_download(agg, total, _agg_state["speed"] / (1024 * 1024))

    def _build_prebuilt_meta(mod, effective_domain):
        try:
            _effective_mod_id = schema_file_id_to_mod_id.get(mod.file_id, 0) or mod.mod_id
            pmeta = build_meta_from_download(
                game_domain=effective_domain, mod_id=_effective_mod_id,
                file_id=mod.file_id, archive_name=mod.file_name or "",
                from_collection=_slug)
            pmeta.nexus_name = mod.mod_name or ""
            pmeta.author = mod.mod_author or ""
            pmeta.version = mod.version or ""
            if getattr(mod, "category_id", 0):
                pmeta.category_id = mod.category_id
            if getattr(mod, "category_name", ""):
                pmeta.category_name = mod.category_name
            if schema_file_id_to_install_type.get(mod.file_id, "").lower() == "dinput":
                pmeta.root_folder = True
            return pmeta
        except Exception:
            return None

    def _preferred_name(mod):
        logical = schema_file_id_to_logical.get(mod.file_id, "") or ""
        schema_name = schema_pos_to_name.get(
            schema_file_id_to_pos.get(mod.file_id, -1), "") or ""
        pref = logical or schema_name or mod.mod_name or ""
        return pref + schema_file_id_to_suffix.get(mod.file_id, "")

    # ---- download producer -------------------------------------------
    def _download_one(mod):
        nonlocal _dl_done, _dl_bytes_done
        mod_domain = (getattr(mod, "domain_name", "") or "").strip() or game_domain
        if _col_stop.is_set():
            with _dl_lock:
                _dl_done += 1
            _install_queue.put((mod, None, mod_domain))
            return

        def _progress_cb(cur, tot, _fid=mod.file_id, _mod=mod):
            nonlocal _dl_bytes_done, _total_bytes
            with _dl_lock:
                prev = _per_mod_prev.get(_fid, 0)
                delta = max(cur - prev, 0)
                _per_mod_prev[_fid] = cur
                _dl_bytes_done += delta
                is_first = prev == 0 and cur > 0
                # A mod's declared size is often unknown (0) or an estimate; the
                # real content-length (`tot`) or bytes seen so far may exceed it.
                # Grow the aggregate denominator so the download bar stays within
                # 0–100% instead of pegging early / overshooting.
                _declared = getattr(_mod, "size_bytes", 0) or 0
                _real = max(tot if tot and tot > 0 else 0, cur)
                if _real > _declared:
                    _total_bytes += _real - _declared
                    _mod.size_bytes = _real
            if is_first:
                cb.on_dl_mod_start(_fid, _mod.mod_name or _mod.file_name or "",
                                   getattr(_mod, "size_bytes", 0) or 0)
            # Throttle per-mod bar updates to ~10/sec per file (always emit the
            # final 100% so the bar never sticks short).
            _now = _time_mod.monotonic()
            _complete = tot > 0 and cur >= tot
            if is_first or _complete or _now - _dl_last_emit.get(_fid, 0.0) >= _EMIT_INTERVAL:
                _dl_last_emit[_fid] = _now
                cb.on_dl_mod_update(_fid, cur, tot)
            _agg_push(force=is_first)

        result = None
        effective_domain = mod_domain

        # Check system downloads + custom locations before downloading.
        _ext_dirs: list[Path] = list(list_all_cache_dirs(getattr(game, "name", "") or ""))
        _ext_seen: set = {p.resolve() for p in _ext_dirs}
        if _col_cfg.get("check_download_locations", True):
            if not is_default_downloads_disabled():
                _sys_dl = _get_downloads_dir()
                if _sys_dl.resolve() not in _ext_seen and _sys_dl.is_dir():
                    _ext_dirs.append(_sys_dl)
                    _ext_seen.add(_sys_dl.resolve())
            for _xl in load_extra_download_locations():
                _xp = Path(_xl).expanduser().resolve()
                if _xp not in _ext_seen and Path(_xl).is_dir():
                    _ext_dirs.append(Path(_xl).expanduser())
                    _ext_seen.add(_xp)
        for _ext_dir in _ext_dirs:
            _ext_found, _ext_complete = _find_cached_archive(
                _ext_dir, mod.file_name or mod.mod_name or "",
                getattr(mod, "size_bytes", 0) or 0, mod.mod_id, mod.file_id,
                expected_md5=getattr(mod, "md5", "") or "")
            if _ext_found and _ext_complete:
                log(f"Collection install: '{mod.mod_name}' found in {_ext_dir} — "
                    "using local copy, skipping download")
                result = DownloadResult(
                    success=True, file_path=_ext_found, file_name=_ext_found.name,
                    bytes_downloaded=_ext_found.stat().st_size, game_domain=mod_domain,
                    mod_id=mod.mod_id, file_id=mod.file_id)
                with _install_lock:
                    _external_archive_paths.add(str(_ext_found))
                break

        try:
            if result is None:
                result = downloader.download_file(
                    game_domain=mod_domain, mod_id=mod.mod_id, file_id=mod.file_id,
                    progress_cb=_progress_cb, cancel=_col_stop,
                    known_file_name=mod.file_name or "",
                    expected_size_bytes=getattr(mod, "size_bytes", 0) or 0,
                    dest_dir=get_download_cache_dir_for_game(getattr(game, "name", "") or ""))
        except Exception as exc:
            import traceback as _tb
            log(f"Collection install: download exception for '{mod.mod_name}' "
                f"(mod_id={mod.mod_id}, file_id={mod.file_id}): {exc}\n{_tb.format_exc()}")

        mod_size = getattr(mod, "size_bytes", 0) or 0
        if mod_size > 0 and _per_mod_prev.get(mod.file_id, 0) == 0:
            _progress_cb(mod_size, mod_size)

        with _dl_lock:
            _dl_done += 1
            _dl_results[mod.file_id] = (result, effective_domain)
            done = _dl_done
        with _install_lock:
            if result and result.success and result.file_path:
                _akey = str(result.file_path)
                _archive_use_count[_akey] = _archive_use_count.get(_akey, 0) + 1
            _inst_done = _install_counters["done"]
        _set_status(f"Downloaded {done}/{_dl_total}, installed {_inst_done}/{_dl_total}…")
        cb.on_dl_mod_finish(mod.file_id)
        if result and result.success and result.file_path:
            cb.on_extract_queue(mod.file_id, mod.mod_name or mod.file_name or "")
        # The install queue is bounded; if it ever fills (installs falling far
        # behind downloads) this put() blocks the download worker so it can't
        # start the next download. The queue is now sized generously so that
        # shouldn't happen, but MM_COL_TIMING=1 logs any block >0.05s to confirm.
        if _COL_TIMING:
            _t_put = _time_mod.monotonic()
            _install_queue.put((mod, result, effective_domain))
            _blocked = _time_mod.monotonic() - _t_put
            if _blocked > 0.05:
                log(f"[timing] download worker blocked {_blocked:.2f}s on install "
                    f"queue for '{mod.mod_name}' (queue full — install is the "
                    f"bottleneck)")
        else:
            _install_queue.put((mod, result, effective_domain))

    # ---- install consumer --------------------------------------------
    def _install_one(mod, result, effective_domain):
        if _col_stop.is_set():
            with _install_lock:
                _install_counters["skipped"] += 1
                _install_counters["done"] += 1
            cb.on_extract_remove(mod.file_id)
            return
        if result is None or not result.success or not result.file_path:
            if result is None:
                _reason = "no result (exception during download)"
            elif not result.success:
                _reason = (result.error or "unknown error").strip() or "unknown error"
                if not result.file_path:
                    _reason += " (no file_path)"
            else:
                _reason = "success but no file_path"
            log(f"Collection install: download failed for '{mod.mod_name}' "
                f"(mod_id={mod.mod_id}, file_id={mod.file_id}): {_reason}")
            with _install_lock:
                _record_outcome(mod, "download_failed", _reason)
                _install_counters["skipped"] += 1
                _install_counters["done"] += 1
            cb.on_extract_remove(mod.file_id)
            return

        archive_path = str(result.file_path)
        auto_fomod = fomod_by_file_id.get(mod.file_id)
        auto_bain = bain_by_file_id.get(mod.file_id)
        _pmeta = _build_prebuilt_meta(mod, effective_domain)
        _preferred = _preferred_name(mod)

        _extract_est = get_uncompressed_size(archive_path)
        _mem_budget.acquire(_extract_est)
        _fomod_flag = {"value": False}

        def _capture_fomod(is_fomod=False):
            _fomod_flag["value"] = is_fomod

        cb.on_extract_add(mod.file_id, _preferred or (mod.mod_name or mod.file_name or ""))
        try:
            folder_name = install_collection_archive(
                archive_path, game, profile_dir, log_fn=log,
                fomod_auto_selections=auto_fomod, bain_auto_selections=auto_bain,
                prebuilt_meta=_pmeta, preferred_name=_preferred,
                skip_index_update=True, overwrite_existing=overwrite_existing,
                defer_interactive_fomod=(auto_fomod is None),
                defer_interactive_bain=(auto_bain is None),
                resolve_fomod=cb.resolve_fomod, resolve_bain=cb.resolve_bain,
                on_installed=_capture_fomod)
        finally:
            _mem_budget.release(_extract_est)
            cb.on_extract_remove(mod.file_id)
        _installed_was_fomod = _fomod_flag["value"]

        if folder_name == FOMOD_DEFERRED:
            with _install_lock:
                _fomod_deferred.append((mod, result, effective_domain))
                _record_outcome(mod, "deferred", "fomod")
                _install_counters["done"] += 1
            return
        if folder_name == BAIN_DEFERRED:
            with _install_lock:
                _bain_deferred.append((mod, result, effective_domain))
                _record_outcome(mod, "deferred", "bain")
                _install_counters["done"] += 1
            return

        with _install_lock:
            if folder_name:
                _install_results[mod.file_id] = folder_name
                _record_outcome(mod, "installed", folder_name)
                _install_counters["installed"] += 1
            else:
                # install_collection_archive returned falsy — the mod extracted
                # but nothing was staged (structure not recognised / all files
                # filtered out). This is the silent-drop path behind "N mods
                # missing"; log it prominently for the end-of-install summary.
                log(f"Collection install: '{mod.mod_name}' produced NO staged "
                    f"files (mod_id={mod.mod_id}, file_id={mod.file_id}, "
                    f"archive={Path(archive_path).name}) — dropped.")
                _record_outcome(mod, "stage_empty",
                                f"archive={Path(archive_path).name}")
                _install_counters["skipped"] += 1
            _install_counters["done"] += 1
            done_so_far = _install_counters["done"]
            _maybe_delete_archive(archive_path, _installed_was_fomod)

        with _dl_lock:
            dl_done_now = _dl_done
        _set_status(f"Downloaded {dl_done_now}/{_dl_total}, installed {done_so_far}/{_dl_total}…")
        _set_progress(done_so_far / _dl_total if _dl_total else 1.0)
        if mod.file_id and folder_name:
            _install_state["installed_fids"].add(mod.file_id)
            cb.on_row_installed(mod.file_id)

    def _maybe_delete_archive(archive_path: str, was_fomod: bool) -> None:
        """Decrement archive use-count; delete at zero honoring settings. Caller
        must hold _install_lock."""
        if archive_path not in _archive_use_count:
            return
        _archive_use_count[archive_path] -= 1
        _col_force_clear = load_collection_settings().get("clear_archive_after_install", False)
        _keep_for_fomod = (not _col_force_clear and was_fomod
                           and load_keep_fomod_archives())
        _should_clear = _col_force_clear or (
            load_clear_archive_after_install() and not _keep_for_fomod)
        if (_archive_use_count[archive_path] == 0 and _should_clear
                and archive_path not in _external_archive_paths):
            try:
                delete_archive_and_sidecar(Path(archive_path))
            except Exception as _del_exc:
                log(f"Collection install: could not remove archive '{archive_path}': {_del_exc}")

    def _install_consumer():
        while True:
            item = _install_queue.get()
            if item is _DONE_SENTINEL:
                _install_queue.task_done()
                break
            mod, result, effective_domain = item
            try:
                _install_one(mod, result, effective_domain)
            except Exception as exc:
                import traceback as _tbx
                log(f"Collection install: unexpected error installing "
                    f"'{mod.mod_name}' (mod_id={getattr(mod,'mod_id',0)}, "
                    f"file_id={getattr(mod,'file_id',0)}): {exc}\n{_tbx.format_exc()}")
                with _install_lock:
                    _record_outcome(mod, "error", str(exc))
                    _install_counters["skipped"] += 1
                    _install_counters["done"] += 1
            finally:
                _install_queue.task_done()

    # ---- launch pipeline ---------------------------------------------
    if to_download:
        _set_status(f"Downloading & installing {_dl_total} mod(s)…")
        _set_progress(0.0)
        # Sort smallest→largest; the double-ended scheduler dedicates ONE
        # worker to the largest-remaining mods (keeps bandwidth saturated on
        # long transfers) while the rest chew through the smallest-remaining
        # from the other end — hiding the per-file link-fetch latency of tiny
        # archives behind the big worker's ongoing download (fixes the
        # "download 8, stutter, download 8" stall).
        _to_download_sorted = order_by_size(to_download)
        if _total_bytes > 0:
            cb.on_agg_download(_dl_bytes_done, _total_bytes, 0.0)

        # Each download fetches its own signed CDN link lazily inside
        # download_file (exactly one get_download_links call per mod actually
        # downloaded — cached mods cost nothing).

        _consumer_threads: list[threading.Thread] = []
        for _ci in range(_INSTALL_WORKERS):
            t = threading.Thread(target=_install_consumer, daemon=True,
                                 name=f"col-install-{_ci}")
            t.start()
            _consumer_threads.append(t)

        run_double_ended(_to_download_sorted, _download_one, _DL_WORKERS,
                         stop=_col_stop)

        _dl_finished.set()
        cb.on_agg_download(_total_bytes, _total_bytes, 0.0)
        for _ in range(_INSTALL_WORKERS):
            _install_queue.put(_DONE_SENTINEL)
        for t in _consumer_threads:
            t.join()

        def _write_preliminary_plugins_txt(label: str) -> None:
            try:
                import os as _os
                _plugin_exts = (".esm", ".esl", ".esp")
                _pre_plugins: list = []
                _seen_plugins: set = set()
                _pre_staging = game.get_effective_mod_staging_path()
                for _fid, _fname in _install_results.items():
                    _mod_dir = _pre_staging / _fname
                    if not _mod_dir.is_dir():
                        continue
                    for _root, _dirs, _files in _os.walk(str(_mod_dir)):
                        for _fn in _files:
                            if _fn.lower().endswith(_plugin_exts):
                                _pname_low = _fn.lower()
                                if _pname_low not in _seen_plugins:
                                    _seen_plugins.add(_pname_low)
                                    _pre_plugins.append(PluginEntry(name=_fn, enabled=True))
                if _pre_plugins:
                    _star_pre = getattr(game, "plugins_use_star_prefix", True)
                    write_plugins(profile_dir / "plugins.txt", _pre_plugins, star_prefix=_star_pre)
                    write_loadorder(profile_dir / "loadorder.txt", _pre_plugins)
                    log(f"Collection install: wrote preliminary plugins.txt "
                        f"({len(_pre_plugins)} plugin(s)) — {label}.")
            except Exception as _pre_exc:
                log(f"Collection install: preliminary plugins.txt skipped — {_pre_exc}")

        _process_deferred(
            _bain_deferred, _fomod_deferred, game, profile_dir, api,
            schema_mods, schema_file_id_to_phase, schema_file_id_to_pos,
            schema_file_id_to_mod_id, schema_file_id_to_install_type,
            schema_file_id_to_logical, schema_pos_to_name, schema_file_id_to_suffix,
            fomod_by_file_id, bain_by_file_id, _install_results, _install_state,
            _install_counters, _install_lock, _archive_use_count,
            _external_archive_paths, _col_stop, _slug, overwrite_existing,
            _write_preliminary_plugins_txt, _maybe_delete_archive, cb, log, _set_status)

    installed += _install_counters["installed"]
    skipped += _install_counters["skipped"]

    # rebuild mod index once for all newly installed mods
    if _install_counters["installed"] > 0:
        try:
            log("Updating mod index…")
            from Utils.filemap import rebuild_mod_index
            rebuild_mod_index(
                profile_dir / "modindex.bin", game.get_effective_mod_staging_path(),
                strip_prefixes=set(getattr(game, "strip_prefixes", None) or []),
                allowed_extensions=set(getattr(game, "install_extensions", None) or []),
                root_deploy_folders=set(getattr(game, "root_deploy_folders", None) or []),
                normalize_folder_case=getattr(game, "normalize_folder_case", True))
        except Exception as _idx_exc:
            log(f"Mod index rebuild skipped: {_idx_exc}")

    # build install_order from parallel results
    for mod in to_download:
        sort_key = _sort_key(mod)
        folder = (_install_results.get(mod.file_id)
                  or schema_pos_to_name.get(sort_key) or mod.mod_name)
        if mod.file_id in _install_results:
            install_order.append((sort_key, folder))

    # Step 2c: bundled assets from the collection archive
    if with_bundled:
        try:
            _n_bundled = _install_bundled_assets(
                game, api, profile_dir, staging_path, collection_schema,
                schema_mods, download_link_path, revision_number,
                collection_slug, staging_lower_map, install_order, log, _set_status)
            installed += _n_bundled
        except Exception as exc:
            log(f"Collection install: error processing bundled assets: {exc}")

    # Step 3: write modlist.txt.
    #   * update_context set → order-preserving update reconcile (below, after all
    #     mods install) — skip the fresh new-profile write here.
    #   * new-profile path → fresh write.
    #   * append path → append reconcile.
    if update_context is not None and not _col_pause.is_set():
        pass  # handled by _reconcile_update_modlist after Step 4
    elif overwrite_existing is None and not _col_pause.is_set():
        _write_new_profile_modlist(profile_dir, modlist_path, install_order, log)
    elif _is_append_run and not _col_pause.is_set():
        install_order.sort(key=lambda x: x[0])
        _append_reconcile_modlist(modlist_path, install_order, _append_pre_existing, log)

    # Step 3b: bundled folders + binary patches + INI tweaks (before LOOT).
    if with_bundled and not _col_pause.is_set() and overwrite_existing is None:
        try:
            _run_step3b(game, api, profile_dir, staging_path, collection_schema,
                        download_link_path, collection_slug, revision_number,
                        _install_results, log)
        except Exception as exc:
            log(f"Collection install: Step 3b failed: {exc}")

    # Step 4: write plugins.txt / loadorder.txt from collection.json
    if not _col_pause.is_set():
        _write_collection_plugins(
            game, profile_dir, plugins_path, collection_schema,
            overwrite_existing, _is_append_run, log, _set_status)

    # Final reconciliation.
    if (update_context is not None and install_order and modlist_path.is_file()
            and not _col_pause.is_set()):
        # UPDATE path — preserve the user's existing order + separators, insert
        # newly-installed mods relative to their schema neighbours.
        try:
            _reconcile_update_modlist(modlist_path, install_order,
                                      update_context, log)
        except Exception as exc:
            log(f"Collection update: reconcile modlist failed: {exc}")
    elif (install_order and modlist_path.is_file() and not _col_pause.is_set()
            and overwrite_existing is None):
        # New-profile path.
        try:
            _folder_to_key: dict[str, int] = {folder: key for key, folder in install_order}
            _existing = read_modlist(modlist_path)
            _known = [e for e in _existing if e.name in _folder_to_key]
            _unknown = [e for e in _existing if e.name not in _folder_to_key]
            for e in _known:
                e.enabled = True
            for e in _unknown:
                if not e.is_separator:
                    e.enabled = True
            _known.sort(key=lambda e: _folder_to_key[e.name])
            _reconciled = _known + _unknown
            write_modlist(modlist_path, _reconciled)
            log(f"Collection install: reconciled modlist.txt "
                f"({len(_known)} ordered, {len(_unknown)} trailing)")
        except Exception as exc:
            log(f"Collection install: reconcile modlist failed: {exc}")

    # Restore the original profile dir
    try:
        game.set_active_profile_dir(old_profile_dir)
        game.load_paths()
    except Exception:
        pass

    # End-of-install verification: every non-optional manifest mod SHOULD have
    # ended up staged. Loudly report any that didn't (the "N mods missing" bug),
    # with the recorded reason per mod, so a failure is visible + diagnosable
    # instead of silently swallowed. Only meaningful on a clean finish (a paused
    # / cancelled run legitimately leaves mods un-installed).
    if not _col_cancel.is_set() and not _col_pause.is_set():
        try:
            _final_staging = game.get_effective_mod_staging_path()
            _missing: list = []
            for mod in ordered_mods:
                fid = getattr(mod, "file_id", 0) or 0
                if not fid:
                    continue
                folder = _install_results.get(fid)
                staged_ok = bool(folder) and (_final_staging is not None
                                              and (Path(_final_staging) / folder).is_dir())
                if not staged_ok:
                    oc = _mod_outcomes.get(fid, {})
                    _missing.append((getattr(mod, "mod_name", "") or f"file {fid}",
                                     getattr(mod, "mod_id", 0) or 0, fid,
                                     oc.get("status", "unknown"),
                                     oc.get("detail", "")))
            if _missing:
                log(f"⚠ Collection install: {len(_missing)} mod(s) did NOT install "
                    f"and are missing from the profile:")
                for _nm, _mid, _fid, _st, _dt in _missing:
                    log(f"    • {_nm} (mod_id={_mid}, file_id={_fid}) — "
                        f"{_st}{(': ' + _dt) if _dt else ''}")
                _set_status(f"Done, but {len(_missing)} mod(s) failed to install "
                            "— see log.")
        except Exception as _ver_exc:
            log(f"Collection install: verification summary failed: {_ver_exc}")

    # Terminal handling
    if _col_cancel.is_set():
        _install_state["done"] = True
        _ACTIVE_INSTALLS.pop(_slug, None)
        cb.on_cancelled(profile_dir)
        return
    if _col_pause.is_set():
        if _slug:
            _PAUSED_INSTALLS[_slug] = {"profile_dir": profile_dir}
        try:
            from Utils.profile_state import write_collection_install_paused
            write_collection_install_paused(profile_dir, True)
        except Exception:
            pass
        _install_state["status"] = f"Paused — {installed} installed so far."
        _install_state["done"] = True
        _ACTIVE_INSTALLS.pop(_slug, None)
        cb.on_paused(installed, str(profile_dir.name))
        return

    final_msg = (f"Done — {installed}/{total} mods installed into profile "
                 f"'{profile_dir.name}'."
                 + (f" ({skipped} skipped)" if skipped else ""))
    _install_state["status"] = final_msg
    _install_state["done"] = True
    _ACTIVE_INSTALLS.pop(_slug, None)
    cb.on_done(installed, skipped, total, str(profile_dir.name))


# ---------------------------------------------------------------------------
# Deferred BAIN/FOMOD (extracted from _run_install 3508-3735 for readability).
# ---------------------------------------------------------------------------
def _process_deferred(
        _bain_deferred, _fomod_deferred, game, profile_dir, api,
        schema_mods, schema_file_id_to_phase, schema_file_id_to_pos,
        schema_file_id_to_mod_id, schema_file_id_to_install_type,
        schema_file_id_to_logical, schema_pos_to_name, schema_file_id_to_suffix,
        fomod_by_file_id, bain_by_file_id, _install_results, _install_state,
        _install_counters, _install_lock, _archive_use_count,
        _external_archive_paths, _col_stop, _slug, overwrite_existing,
        _write_preliminary_plugins_txt, _maybe_delete_archive, cb, log, _set_status):
    from Nexus.nexus_meta import build_meta_from_download

    def _mk_meta_and_name(mod, domain):
        try:
            _mid = schema_file_id_to_mod_id.get(mod.file_id, 0) or mod.mod_id
            pmeta = build_meta_from_download(
                game_domain=domain, mod_id=_mid, file_id=mod.file_id,
                archive_name=mod.file_name or "", from_collection=_slug)
            pmeta.nexus_name = mod.mod_name or ""
            pmeta.author = mod.mod_author or ""
            pmeta.version = mod.version or ""
            if getattr(mod, "category_id", 0):
                pmeta.category_id = mod.category_id
            if getattr(mod, "category_name", ""):
                pmeta.category_name = mod.category_name
            if schema_file_id_to_install_type.get(mod.file_id, "").lower() == "dinput":
                pmeta.root_folder = True
        except Exception:
            pmeta = None
        logical = schema_file_id_to_logical.get(mod.file_id, "") or ""
        schema_name = schema_pos_to_name.get(
            schema_file_id_to_pos.get(mod.file_id, -1), "") or ""
        pref = (logical or schema_name or mod.mod_name or "") \
            + schema_file_id_to_suffix.get(mod.file_id, "")
        return pmeta, pref

    def _record(mod, folder):
        with _install_lock:
            if folder:
                _install_results[mod.file_id] = folder
                _install_counters["installed"] += 1
            else:
                log(f"Collection install: deferred mod '{mod.mod_name}' produced "
                    f"NO staged files (mod_id={getattr(mod,'mod_id',0)}, "
                    f"file_id={mod.file_id}) — dropped.")
                _install_counters["skipped"] += 1
        if folder and mod.file_id:
            _install_state["installed_fids"].add(mod.file_id)
            cb.on_row_installed(mod.file_id)

    # Deferred BAIN first (before FOMODs).
    if _bain_deferred and not _col_stop.is_set():
        _bain_deferred.sort(key=lambda t: (
            schema_file_id_to_phase.get(t[0].file_id, 0),
            schema_file_id_to_pos.get(t[0].file_id, len(schema_mods))))
        log(f"Installing {len(_bain_deferred)} deferred BAIN mod(s)…")
        _set_status(f"Installing {len(_bain_deferred)} deferred BAIN mod(s)…")
        for _mod, _result, _domain in _bain_deferred:
            if _col_stop.is_set():
                break
            _archive = str(_result.file_path)
            _pmeta, _pref = _mk_meta_and_name(_mod, _domain)
            cb.on_extract_add(_mod.file_id, _pref or (_mod.mod_name or ""))
            try:
                _folder = install_collection_archive(
                    _archive, game, profile_dir, log_fn=log,
                    bain_auto_selections=bain_by_file_id.get(_mod.file_id),
                    prebuilt_meta=_pmeta, preferred_name=_pref,
                    skip_index_update=True, overwrite_existing=overwrite_existing,
                    resolve_bain=cb.resolve_bain)
            except Exception as _exc:
                log(f"Collection install: failed to install deferred BAIN "
                    f"'{_mod.mod_name}': {_exc}")
                _folder = None
            finally:
                cb.on_extract_remove(_mod.file_id)
            _record(_mod, _folder)
            with _install_lock:
                _maybe_delete_archive(_archive, True)

    # Deferred FOMODs — write prelim plugins.txt first, then per-phase.
    if _fomod_deferred and not _col_stop.is_set():
        _write_preliminary_plugins_txt("pre-FOMOD")
        _fomod_deferred.sort(key=lambda t: (
            schema_file_id_to_phase.get(t[0].file_id, 0),
            schema_file_id_to_pos.get(t[0].file_id, len(schema_mods))))
        _phase_counts: dict[int, int] = {}
        for _t in _fomod_deferred:
            _ph = schema_file_id_to_phase.get(_t[0].file_id, 0)
            _phase_counts[_ph] = _phase_counts.get(_ph, 0) + 1
        _phase_summary = ", ".join(
            f"phase {p}: {_phase_counts[p]}" for p in sorted(_phase_counts))
        log(f"Installing {len(_fomod_deferred)} deferred FOMOD mod(s) ({_phase_summary})…")
        _set_status(f"Installing {len(_fomod_deferred)} deferred FOMOD mod(s)…")
        _current_phase = None
        for _mod, _result, _domain in _fomod_deferred:
            if _col_stop.is_set():
                break
            _this_phase = schema_file_id_to_phase.get(_mod.file_id, 0)
            if _current_phase is not None and _this_phase != _current_phase:
                _write_preliminary_plugins_txt(f"phase {_current_phase} → {_this_phase}")
            _current_phase = _this_phase
            _archive = str(_result.file_path)
            _pmeta, _pref = _mk_meta_and_name(_mod, _domain)
            cb.on_extract_add(_mod.file_id, _pref or (_mod.mod_name or ""))
            try:
                _folder = install_collection_archive(
                    _archive, game, profile_dir, log_fn=log,
                    fomod_auto_selections=fomod_by_file_id.get(_mod.file_id),
                    bain_auto_selections=bain_by_file_id.get(_mod.file_id),
                    prebuilt_meta=_pmeta, preferred_name=_pref,
                    skip_index_update=True, overwrite_existing=overwrite_existing,
                    resolve_fomod=cb.resolve_fomod, resolve_bain=cb.resolve_bain)
            except Exception as _exc:
                log(f"Collection install: failed to install deferred FOMOD "
                    f"'{_mod.mod_name}': {_exc}")
                _folder = None
            finally:
                cb.on_extract_remove(_mod.file_id)
            _record(_mod, _folder)
            with _install_lock:
                _maybe_delete_archive(_archive, True)


# ---------------------------------------------------------------------------
# modlist / plugins writers (new-profile path, extracted for readability).
# ---------------------------------------------------------------------------
def _write_new_profile_modlist(profile_dir, modlist_path, install_order, log):
    install_order.sort(key=lambda x: x[0])
    try:
        _pre_existing = read_modlist(modlist_path) if modlist_path.is_file() else []
    except Exception:
        _pre_existing = []
    _ord_names_lower = {folder.lower() for _, folder in install_order}
    _preserved = [e for e in _pre_existing
                  if not e.is_separator and e.name.lower() not in _ord_names_lower]
    modlist_entries = [ModEntry(name=folder, enabled=True, locked=False)
                       for _, folder in install_order]
    if not modlist_entries:
        return
    try:
        _candidates: dict[str, list] = {}
        _order: list = []
        for me in modlist_entries:
            _order.append(me)
            if "__" in me.name:
                bname = me.name.split("__", 1)[0]
                _candidates.setdefault(bname, []).append(me)
        _bundle_map = {k: v for k, v in _candidates.items() if len(v) >= 2}
        _bundle_members = {id(e) for vs in _bundle_map.values() for e in vs}
        _non_bundle = [e for e in _order if id(e) not in _bundle_members]
        final_entries: list = list(_non_bundle)
        for bname, variants in _bundle_map.items():
            final_entries.append(
                ModEntry(name=f"{bname}_separator", enabled=True, locked=True, is_separator=True))
            for v in variants:
                v.locked = False
                v.enabled = True
                final_entries.append(v)
        user_sep_name = "User_Installed_separator"
        if _preserved:
            final_entries.append(
                ModEntry(name=user_sep_name, enabled=True, locked=True, is_separator=True))
            final_entries.extend(_preserved)
        write_modlist(modlist_path, final_entries)
        if _bundle_map or _preserved:
            from Utils.profile_state import read_separator_locks, write_separator_locks
            _locks = read_separator_locks(profile_dir)
            for bname in _bundle_map:
                _locks[f"{bname}_separator"] = True
            if _preserved:
                _locks[user_sep_name] = True
            write_separator_locks(profile_dir, _locks)
        log(f"Collection install: wrote modlist.txt with {len(final_entries)} entries")
    except Exception as exc:
        log(f"Collection install: failed to write modlist.txt: {exc}")


def _write_collection_plugins(game, profile_dir, plugins_path, collection_schema,
                              overwrite_existing, _is_append_run, log, _set_status):
    from Utils.game_helpers import _vanilla_plugins_for_game
    schema_plugins: list[dict] = collection_schema.get("plugins", [])
    if schema_plugins and overwrite_existing is None:
        try:
            author_entries = [
                PluginEntry(name=p.get("name", ""), enabled=p.get("enabled", True))
                for p in schema_plugins if p.get("name", "")]
            author_lower = {e.name.lower() for e in author_entries}
            vanilla_map = _vanilla_plugins_for_game(game)
            plugins_include_vanilla = getattr(game, "plugins_include_vanilla", False)
            vanilla_lower = set() if plugins_include_vanilla else set(vanilla_map.keys())
            _apply_collection_groups(profile_dir, collection_schema, log)
            final_entries: list[PluginEntry] = []
            loot_enabled = getattr(game, "loot_sort_enabled", False)
            if loot_enabled and _loot_available():
                try:
                    _set_status("Running LOOT sort to apply collection load order…")
                    from LOOT.loot_sorter import sort_plugins as _loot_sort
                    _ext_order = {".esm": 0, ".esp": 1, ".esl": 2}
                    vanilla_prepend = [
                        PluginEntry(name=orig, enabled=True)
                        for low, orig in sorted(
                            vanilla_map.items(),
                            key=lambda kv: (_ext_order.get(Path(kv[0]).suffix, 9), kv[0]))
                        if low not in author_lower]
                    all_entries = vanilla_prepend + author_entries
                    name_to_enabled = {e.name: e.enabled for e in all_entries}
                    loot_result = _loot_sort(
                        plugin_names=[e.name for e in all_entries],
                        enabled_set={e.name for e in all_entries if e.enabled},
                        game_name=game.name, game_path=game.get_game_path(),
                        staging_root=game.get_effective_mod_staging_path(), log_fn=log,
                        game_type_attr=getattr(game, "loot_game_type", ""),
                        game_id=getattr(game, "game_id", ""),
                        masterlist_url=getattr(game, "loot_masterlist_url", ""),
                        masterlist_repo=getattr(game, "loot_masterlist_repo", ""),
                        game_data_dir=(game.get_vanilla_plugins_path()
                                       if hasattr(game, "get_vanilla_plugins_path") else None),
                        userlist_path=profile_dir / "userlist.yaml")
                    final_entries = [
                        PluginEntry(name=n, enabled=name_to_enabled.get(n, True))
                        for n in loot_result.sorted_names]
                    log(f"Collection install: LOOT sort produced {len(final_entries)} plugin(s).")
                except Exception as loot_exc:
                    log(f"Collection install: LOOT sort failed — {loot_exc}; "
                        "falling back to flat list.")
            if not final_entries:
                _ext_order = {".esm": 0, ".esp": 1, ".esl": 2}
                vanilla_prefix = [
                    PluginEntry(name=orig, enabled=True)
                    for low, orig in sorted(
                        vanilla_map.items(),
                        key=lambda kv: (_ext_order.get(Path(kv[0]).suffix, 9), kv[0]))
                    if low not in author_lower]
                final_entries = vanilla_prefix + author_entries
            star_prefix = getattr(game, "plugins_use_star_prefix", True)
            write_plugins(plugins_path,
                          [e for e in final_entries if e.name.lower() not in vanilla_lower],
                          star_prefix=star_prefix)
            write_loadorder(plugins_path.parent / "loadorder.txt", final_entries)
            log(f"Collection install: wrote plugins.txt ({len(final_entries)} plugin(s)).")
        except Exception as exc:
            log(f"Collection install: failed to write plugins.txt: {exc}")
    elif schema_plugins and _is_append_run:
        try:
            _apply_collection_groups(profile_dir, collection_schema, log)
        except Exception as exc:
            log(f"Collection append: failed to write userlist.yaml rules: {exc}")


def _loot_available() -> bool:
    try:
        from LOOT.loot_sorter import is_available
        return bool(is_available())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Bundled assets (Step 2c). Ported from _run_install 3771-3888.
# ---------------------------------------------------------------------------
def _install_bundled_assets(game, api, profile_dir, staging_path, collection_schema,
                            schema_mods, download_link_path, revision_number,
                            collection_slug, staging_lower_map, install_order, log,
                            _set_status) -> int:
    import tempfile as _tf
    import shutil as _shutil
    bundle_schema_mods = [
        m for m in schema_mods
        if (m.get("source") or {}).get("type", "").lower() == "bundle"]
    if not (bundle_schema_mods and download_link_path):
        return 0
    installed = 0
    _scratch_root = get_download_cache_dir_for_game(getattr(game, "name", "") or "")
    bundle_extract_dir = _tf.mkdtemp(prefix="amethyst_bundle_", dir=str(_scratch_root))
    try:
        _slug = (collection_slug or "").strip()
        _rev = int(revision_number) if revision_number is not None else "x"
        _cached_archive = _scratch_root / f"{_slug}_rev{_rev}.7z"
        cj_full: dict = {}
        if _slug and _cached_archive.is_file():
            _set_status(f"Extracting cached collection archive for "
                        f"{len(bundle_schema_mods)} bundled mod(s)…")
            log(f"Collection install: reusing cached archive {_cached_archive}")
            try:
                import py7zr as _py7zr_local
                with _py7zr_local.SevenZipFile(str(_cached_archive), mode="r") as arc:
                    arc.extractall(path=bundle_extract_dir)
                _cj_path = Path(bundle_extract_dir) / "collection.json"
                if _cj_path.is_file():
                    cj_full = json.loads(_cj_path.read_text(encoding="utf-8"))
            except Exception as exc:
                log(f"Collection install: cached archive extract failed ({exc}) — re-downloading")
                cj_full = {}
        if not cj_full:
            _set_status(f"Downloading collection archive for "
                        f"{len(bundle_schema_mods)} bundled mod(s)…")
            cj_full = api.get_collection_archive_full(
                download_link_path, bundle_extract_dir,
                keep_archive_at=str(_cached_archive) if _slug else None)
        if cj_full:
            _bundled_meta_map = _installed_bundled_meta_map(staging_path, _slug)
            for bm in bundle_schema_mods:
                bm_name = bm.get("name") or ""
                src = bm.get("source") or {}
                file_expr = src.get("fileExpression") or bm_name
                bundle_subdir = Path(bundle_extract_dir) / "bundled" / file_expr
                if not bundle_subdir.is_dir():
                    bundle_subdir = Path(bundle_extract_dir) / "bundled" / bm_name
                if not bundle_subdir.is_dir():
                    log(f"Collection install: bundled asset '{bm_name}' not found in archive")
                    continue
                mod_name_clean = re.sub(r"[^\w\s\-]", "", bm_name).strip().replace(" ", "_") or file_expr
                if mod_name_clean.lower() in {k.lower() for k in staging_lower_map}:
                    log(f"Collection install: bundled '{bm_name}' already installed — skipping")
                    existing = staging_lower_map.get(mod_name_clean.lower(), mod_name_clean)
                    install_order.append((-1, existing))
                    installed += 1
                    continue
                _meta_hit = (_bundled_meta_map.get(file_expr.lower())
                             or _bundled_meta_map.get(bm_name.lower()))
                if _meta_hit:
                    log(f"Collection install: bundled '{bm_name}' already installed "
                        f"as '{_meta_hit}' — skipping")
                    install_order.append((-1, _meta_hit))
                    installed += 1
                    continue
                _set_status(f"Installing bundled asset: {bm_name}…")
                try:
                    import configparser as _cpi
                    dest = staging_path / mod_name_clean
                    if dest.exists():
                        _shutil.rmtree(dest)
                    _shutil.copytree(str(bundle_subdir), str(dest))
                    cp = _cpi.ConfigParser()
                    general = {
                        "modname": bm_name, "installationfile": file_expr,
                        "fromCollection": _slug, "fromCollectionBundled": "true"}
                    if revision_number is not None:
                        general["fromCollectionRevision"] = str(int(revision_number))
                    cp["General"] = general
                    with open(dest / "meta.ini", "w", encoding="utf-8") as mf:
                        cp.write(mf)
                    install_order.append((-1, mod_name_clean))
                    installed += 1
                    log(f"Collection install: installed bundled asset "
                        f"'{bm_name}' → '{mod_name_clean}'")
                except Exception as exc:
                    log(f"Collection install: failed to install bundled asset '{bm_name}': {exc}")
    finally:
        try:
            _shutil.rmtree(bundle_extract_dir, ignore_errors=True)
        except Exception:
            pass
    return installed


def _installed_bundled_meta_map(staging_path: Path, slug: str) -> "dict[str, str]":
    """Map installationfile/modname of installed bundled mods → folder name
    (ported from _installed_bundled_meta_map)."""
    import configparser as _cpi
    meta_map: dict[str, str] = {}
    if not staging_path.is_dir():
        return meta_map
    for mod_dir in staging_path.iterdir():
        meta_path = mod_dir / "meta.ini"
        if not mod_dir.is_dir() or not meta_path.is_file():
            continue
        cp = _cpi.ConfigParser()
        try:
            cp.read(meta_path, encoding="utf-8")
            if not cp.has_section("General"):
                continue
            if not cp["General"].getboolean("fromCollectionBundled", fallback=False):
                continue
            if (cp["General"].get("fromCollection", "") or "").strip() != slug:
                continue
            for key in ("installationfile", "modname"):
                val = (cp["General"].get(key, "") or "").strip()
                if val:
                    meta_map[val.lower()] = mod_dir.name
        except Exception:
            continue
    return meta_map


# ---------------------------------------------------------------------------
# Step 3b: bundled folders + binary patches + INI tweaks from the cached archive.
# Ported from _install_bundled_from_extracted / _apply_collection_binary_patches
# / _apply_collection_ini_tweaks (+ _ensure_collection_archive_extracted).
# ---------------------------------------------------------------------------
def _ensure_collection_archive_extracted(game, api, collection_slug,
                                         revision_number, download_link_path, log):
    """Return a dir with the extracted collection archive (cached .7z preferred),
    or None. Caller rmtree's the returned dir."""
    import shutil as _shutil
    import tempfile as _tf
    slug = (collection_slug or "").strip()
    rev = int(revision_number) if revision_number is not None else "x"
    if not slug:
        return None
    cache_dir = get_download_cache_dir_for_game(getattr(game, "name", "") or "")
    archive_path = cache_dir / f"{slug}_rev{rev}.7z"
    if not archive_path.is_file():
        if not download_link_path:
            log(f"Collection archive: not at {archive_path} and no link — skipping")
            return None
        log(f"Collection archive: not cached, downloading to {archive_path}")
        _fetch_dir = Path(_tf.mkdtemp(prefix="amethyst_bundle_fetch_", dir=str(cache_dir)))
        try:
            cj = api.get_collection_archive_full(
                download_link_path, str(_fetch_dir), keep_archive_at=str(archive_path))
            if not cj or not archive_path.is_file():
                log("Collection archive: fallback download failed")
                return None
        finally:
            _shutil.rmtree(_fetch_dir, ignore_errors=True)
    extract_dir = Path(_tf.mkdtemp(prefix="amethyst_archive_extract_", dir=str(cache_dir)))
    try:
        import py7zr
        with py7zr.SevenZipFile(str(archive_path), mode="r") as arc:
            arc.extractall(path=str(extract_dir))
    except Exception as exc:
        log(f"Collection archive: failed to extract {archive_path}: {exc}")
        _shutil.rmtree(extract_dir, ignore_errors=True)
        return None
    return extract_dir


def _install_bundled_from_extracted(archive_root, modlist_path, staging_path,
                                    collection_slug, revision_number, log):
    import re as _re
    import shutil as _shutil
    import configparser as _cpi
    slug = (collection_slug or "").strip()
    rev_str = str(int(revision_number)) if revision_number is not None else ""
    bundled_root = archive_root / "bundled"
    if not bundled_root.is_dir():
        return
    bundle_folders = [p for p in sorted(bundled_root.iterdir()) if p.is_dir()]
    if not bundle_folders:
        return
    log(f"Collection bundled-cache: installing {len(bundle_folders)} bundled folder(s)")
    staging_lower_map = ({p.name.lower(): p.name for p in staging_path.iterdir() if p.is_dir()}
                         if staging_path.exists() else {})
    bundled_meta_map = _installed_bundled_meta_map(staging_path, slug)
    new_mod_names: list[str] = []
    for src_folder in bundle_folders:
        raw_name = src_folder.name
        clean = _re.sub(r"[^\w\s\-]", "", raw_name).strip().replace(" ", "_") or raw_name
        if clean.lower() in staging_lower_map:
            new_mod_names.append(staging_lower_map[clean.lower()])
            continue
        if raw_name.lower() in bundled_meta_map:
            new_mod_names.append(bundled_meta_map[raw_name.lower()])
            continue
        dest = staging_path / clean
        if dest.exists():
            _shutil.rmtree(dest, ignore_errors=True)
        _shutil.copytree(str(src_folder), str(dest))
        cp = _cpi.ConfigParser()
        general = {"modname": raw_name, "installationfile": raw_name,
                   "fromCollection": slug, "fromCollectionBundled": "true"}
        if rev_str:
            general["fromCollectionRevision"] = rev_str
        cp["General"] = general
        try:
            with open(dest / "meta.ini", "w", encoding="utf-8") as mf:
                cp.write(mf)
        except Exception:
            pass
        new_mod_names.append(clean)
        log(f"Collection bundled-cache: installed '{raw_name}' → '{clean}'")
    if new_mod_names and modlist_path.is_file():
        try:
            existing = read_modlist(modlist_path)
            existing_lower = {e.name.lower() for e in existing}
            prepend = [ModEntry(name=n, enabled=True, locked=False)
                       for n in new_mod_names if n.lower() not in existing_lower]
            if prepend:
                write_modlist(modlist_path, prepend + existing)
                log(f"Collection bundled-cache: prepended {len(prepend)} bundled mod(s)")
        except Exception as exc:
            log(f"Collection bundled-cache: modlist update failed: {exc}")


def _apply_collection_binary_patches(archive_root, collection_schema, staging_path,
                                     install_results, collection_slug,
                                     revision_number, log):
    from Utils.collection_patches import apply_collection_patches
    staging_lower = ({p.name.lower(): p.name for p in staging_path.iterdir() if p.is_dir()}
                     if staging_path.exists() else {})

    def _folder_for(schema_entry):
        src = schema_entry.get("source") or {}
        fid = src.get("fileId")
        if fid is not None:
            folder = install_results.get(int(fid))
            if folder:
                return folder
        schema_name = schema_entry.get("name") or ""
        if schema_name:
            return staging_lower.get(schema_name.lower())
        return None

    slug = (collection_slug or "").strip()
    rev_str = str(int(revision_number)) if revision_number is not None else None
    result = apply_collection_patches(
        archive_root=archive_root, collection_schema=collection_schema,
        staging_path=staging_path, mod_folder_for=_folder_for, log_fn=log,
        collection_slug=slug, collection_revision=rev_str)
    if (result.applied or result.crc_mismatch or result.missing_diff
            or result.missing_target or result.failed):
        log(f"Collection patches: applied={result.applied}, "
            f"crc_mismatch={result.crc_mismatch}, missing_diff={result.missing_diff}, "
            f"missing_target={result.missing_target}, failed={result.failed}")


def _apply_collection_ini_tweaks(archive_root, profile_dir, game, log):
    from Utils.collection_ini_tweaks import GAME_INI_TARGETS, apply_collection_ini_tweaks
    if not (archive_root / "INI Tweaks").is_dir():
        return
    try:
        from Games.Bethesda.Bethesda import _read_ini_key, _set_ini_key
    except Exception as exc:
        log(f"Collection INI tweaks: INI helpers unavailable ({exc}) — skipped")
        return
    prefix_ini_dir = None
    get_mygames = getattr(game, "_mygames_path", None)
    if callable(get_mygames):
        try:
            prefix_ini_dir = get_mygames()
        except Exception:
            prefix_ini_dir = None
    game_name = getattr(game, "name", "") or ""
    allowed_targets = GAME_INI_TARGETS.get(game_name)
    ini_target_dir = profile_dir
    profile_name = profile_dir.name
    get_ini_dir = getattr(game, "_profile_ini_dir", None)
    if callable(get_ini_dir):
        try:
            ini_target_dir = get_ini_dir(profile_name)
            ini_target_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            log(f"Collection INI tweaks: could not resolve profile 'ini files' "
                f"folder ({exc}) — using profile root")
            ini_target_dir = profile_dir
    if not getattr(game, "profile_ini_files", False):
        try:
            game.set_profile_ini_files(True)
            log("Collection INI tweaks: enabled profile-specific INI files")
        except Exception as exc:
            log(f"Collection INI tweaks: could not enable profile INI files ({exc})")
    result = apply_collection_ini_tweaks(
        archive_root=archive_root, profile_dir=ini_target_dir,
        prefix_ini_dir=prefix_ini_dir, set_ini_key=_set_ini_key,
        read_ini_key=_read_ini_key, log_fn=log, allowed_targets=allowed_targets)
    if result.files_processed or result.skipped:
        log(f"Collection INI tweaks: files={result.files_processed}, "
            f"added={result.keys_added}, changed={result.keys_changed}, "
            f"unchanged={result.keys_unchanged}, skipped={result.skipped}")


def _run_step3b(game, api, profile_dir, staging_path, collection_schema,
                download_link_path, collection_slug, revision_number,
                install_results, log):
    """Install bundled folders + apply binary patches + INI tweaks from the cached
    collection archive. Runs after modlist is written, before LOOT."""
    import shutil as _shutil
    archive_root = _ensure_collection_archive_extracted(
        game, api, collection_slug, revision_number, download_link_path or "", log)
    if archive_root is None:
        return
    modlist_path = profile_dir / "modlist.txt"
    try:
        try:
            _install_bundled_from_extracted(
                archive_root, modlist_path, staging_path, collection_slug,
                revision_number, log)
        except Exception as exc:
            log(f"Collection install: bundled step failed: {exc}")
        try:
            _apply_collection_binary_patches(
                archive_root, collection_schema, staging_path, install_results,
                collection_slug, revision_number, log)
        except Exception as exc:
            log(f"Collection install: patches step failed: {exc}")
        try:
            _apply_collection_ini_tweaks(archive_root, profile_dir, game, log)
        except Exception as exc:
            log(f"Collection install: INI tweaks step failed: {exc}")
    finally:
        _shutil.rmtree(archive_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Append reconcile (ported; used only on the append path — not yet wired by Qt).
# ---------------------------------------------------------------------------
def _append_reconcile_modlist(modlist_path, install_order, pre_existing, log):
    """Re-apply the collection's load order but only reposition mods newly
    installed by this run; every pre-existing mod keeps its position + state.
    Ported from CollectionsDialog._append_reconcile_modlist."""
    try:
        existing = read_modlist(modlist_path) if modlist_path.is_file() else []
    except Exception:
        existing = []
    _ord = [(k, f) for k, f in install_order]
    _new_names = {f for _, f in _ord if f.lower() not in pre_existing}
    # Keep pre-existing entries where they are; drop the freshly-installed ones
    # so we can reinsert them in collection order.
    kept = [e for e in existing if e.name not in _new_names]
    new_entries = [ModEntry(name=f, enabled=True, locked=False)
                   for _, f in sorted(_ord, key=lambda x: x[0])
                   if f in _new_names]
    # Insert the new mods at the top (highest priority) preserving kept order.
    write_modlist(modlist_path, new_entries + kept)
    log(f"Collection append: placed {len(new_entries)} new mod(s), "
        f"preserved {len(kept)} existing entrie(s)")


def _reconcile_update_modlist(modlist_path, install_order, update_context, log):
    """Rebuild modlist.txt after a collection UPDATE install.

    Preserves separators and the user's existing load order for mods that are
    still in the new revision. New mods (installed during this run that weren't
    in the pre-update snapshot) are inserted relative to their schema-defined
    neighbours; mods with no schema position go at the top of the list.

    ``install_order`` is the sorted list of ``(schema_pos, folder_name)`` pairs
    the installer produced. ``update_context["snapshot"]`` is the pre-removal
    modlist (order minus the mods removed during the update). Verbatim port of
    ``CollectionsDialog._reconcile_update_modlist``."""
    snapshot: "list[ModEntry]" = list(update_context.get("snapshot") or [])

    # Existing snapshot folder names (non-separator) — the mods staying put.
    snapshot_folder_lower: set[str] = {
        e.name.lower() for e in snapshot if not e.is_separator
    }

    # Partition install_order into "already in snapshot" (no-op, order preserved)
    # vs "new" (need insertion).
    new_folders: "list[tuple[int, str]]" = [
        (pos, folder) for pos, folder in install_order
        if folder.lower() not in snapshot_folder_lower
    ]

    # Split new folders by whether they have a defined schema position.
    unplaced: "list[str]" = []
    placeable: "list[tuple[int, str]]" = []
    for pos, folder in new_folders:
        if pos < 0:
            unplaced.append(folder)
        else:
            placeable.append((pos, folder))
    placeable.sort(key=lambda x: x[0])

    result: list = list(snapshot)  # copy
    sorted_io = sorted(install_order, key=lambda x: x[0])

    def _find_result_index(folder_lower: str) -> int:
        for i, e in enumerate(result):
            if not e.is_separator and e.name.lower() == folder_lower:
                return i
        return -1

    for pos, folder in placeable:
        # Right neighbour: first folder in sorted_io with pos > this pos that is
        # currently present in result (and not this same folder).
        insert_idx = None
        for npos, nfolder in sorted_io:
            if npos <= pos or nfolder == folder:
                continue
            idx = _find_result_index(nfolder.lower())
            if idx >= 0:
                insert_idx = idx
                break
        if insert_idx is None:
            # Left neighbour: last folder with pos < this pos in result.
            left_candidates = [
                (npos, nfolder) for npos, nfolder in sorted_io
                if npos < pos and nfolder != folder
            ]
            for npos, nfolder in sorted(left_candidates, key=lambda x: -x[0]):
                idx = _find_result_index(nfolder.lower())
                if idx >= 0:
                    insert_idx = idx + 1
                    break
        if insert_idx is None:
            insert_idx = 0
        result.insert(insert_idx, ModEntry(name=folder, enabled=True, locked=False))

    # Unplaced (no schema position) go at the very top.
    for folder in reversed(unplaced):
        result.insert(0, ModEntry(name=folder, enabled=True, locked=False))

    # Force-enable every mod entry we're writing — update never leaves a mod
    # disabled. Separators keep their locked/enabled state.
    for e in result:
        if not e.is_separator:
            e.enabled = True

    write_modlist(modlist_path, result)
    log(f"Collection update: reconciled modlist.txt "
        f"({len(snapshot)} preserved, {len(placeable)} inserted, "
        f"{len(unplaced)} unplaced at top)")
