"""
Install mod from archive: extract, FOMOD wizard, strip/prefix, copy to staging, update modlist/plugins.
Used by ModListPanel, PluginPanel, TopBar, and App. Imports dialogs and mod_name_utils.
"""

import json
import inspect
import os
import re
import shutil
import tarfile
import tempfile
import threading
import zipfile

# Ensures only one interactive dialog (FOMOD, Unexpected Mod Structure, etc.) is
# shown at a time when collection installs run parallel extraction workers.
# Any worker that needs user input acquires this lock, marshals the dialog to
# the main thread, waits for the result, then releases.
_interactive_dialog_lock = threading.Lock()

# Set while a FOMOD dialog is open (either on the main thread or via a worker).
# Used by the NXM install queue to defer new installs until the dialog closes,
# preventing a second FomodDialog overlay from being placed on top of the first.
fomod_dialog_active = threading.Event()

# Guards /tmp space accounting so parallel workers don't all race to claim the
# same free space before any of them has started writing.
_tmp_space_lock = threading.Lock()
_tmp_space_reserved: int = 0  # bytes currently claimed by in-flight extractions


def get_uncompressed_size(path: str, compressed_size: int = 0) -> int:
    """Return best-effort total uncompressed size of the archive in bytes.

    Tries archive metadata first (zipfile headers, ``7z l -slt``), then falls
    back to a 15× multiplier of *compressed_size* (handles extreme texture
    packs).  If *compressed_size* is 0, the on-disk file size is used instead.
    """
    if compressed_size <= 0:
        try:
            compressed_size = os.path.getsize(path)
        except OSError:
            compressed_size = 0
    _ext = path.lower()
    # ZIP: fast metadata read via zipfile
    if _ext.endswith(".zip"):
        try:
            with zipfile.ZipFile(path, "r") as _zf:
                _total = sum(m.file_size for m in _zf.infolist())
            if _total > 0:
                return _total
        except Exception:
            pass
    # 7z/rar/zip fallback: use `7z l -slt` which prints Size: per entry
    _7z_bin = shutil.which("7zzs") or shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")
    if _7z_bin:
        try:
            import subprocess
            _res = subprocess.run(
                [_7z_bin, "l", "-slt", path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=30,
            )
            _total = 0
            for _line in _res.stdout.splitlines():
                if _line.startswith("Size = "):
                    try:
                        _total += int(_line.split("=", 1)[1].strip())
                    except ValueError:
                        pass
            if _total > 0:
                return _total
        except Exception:
            pass
    # Fallback: assume a generous 15× expansion (handles extreme texture packs)
    return compressed_size * 15


def _path_is_tmpfs(path: str) -> bool:
    """Best-effort check whether *path* lives on a tmpfs (ramdisk) mount.

    Used to avoid silently extracting a multi-GB archive into a small
    tmpfs ``/tmp``, where overflowing the mount's ``size=`` cap surfaces as
    errno EDQUOT ("Disk Quota Exceeded") rather than ENOSPC.
    """
    try:
        # f_blocks * f_frsize is the *total* size of the mount.  A tmpfs is
        # typically a small fraction of RAM; a real disk is far larger.  We
        # don't have statfs f_type from os.statvfs, so size is our proxy.
        st = os.statvfs(path)
        return st.f_blocks * st.f_frsize < 8 * 1024 ** 3  # < 8 GiB ⇒ treat as ramdisk
    except OSError:
        return False


def _is_disk_full_error(text: str | None) -> bool:
    """True if *text* (tool stderr) reports an out-of-space condition.

    Covers both ENOSPC ("No space left") and the tmpfs size-cap case, which
    the kernel reports as EDQUOT — surfaced by 7z/tar as "Disk Quota Exceeded".
    """
    if not text:
        return False
    low = text.lower()
    return any(s in low for s in (
        "disk quota exceeded", "quota exceeded",
        "no space left", "enospc", "edquot",
    ))


def _reroute_extract_dir(old_dir: str, disk_parent: "Path") -> str:
    """Discard *old_dir* and create a fresh extraction dir under *disk_parent*.

    Falls back to keeping *old_dir* if the new location can't be created.
    """
    try:
        Path(disk_parent).mkdir(parents=True, exist_ok=True)
        new_dir = tempfile.mkdtemp(prefix="modmgr_", dir=str(disk_parent))
    except OSError:
        return old_dir
    shutil.rmtree(old_dir, ignore_errors=True)
    return new_dir


def _debackslash_extracted_tree(extract_dir: str, log_fn=None) -> int:
    """Repair an extraction that kept Windows backslash path separators.

    Some ZIPs (commonly packed by PowerShell's ``Compress-Archive``) store
    member names with ``\\`` separators, which violates the ZIP spec. Native
    extractors (7z/bsdtar) and Python's ``zipfile`` then create *flat* files
    whose names literally contain backslashes — e.g. a single file called
    ``Data\\Interface\\foo.ttf`` — instead of a nested folder tree.

    Downstream install code resolves paths with forward slashes, so those flat
    entries are never matched (``rglob`` finds them, but ``src.is_file()`` then
    fails against the rewritten ``Data/Interface/foo.ttf`` path) → "0 items
    copied". This sweep relocates every backslash-named entry to its proper
    nested location. Returns the number of entries moved.

    Idempotent and cheap when no backslash names exist (the common case).
    """
    root = Path(extract_dir)
    # Collect first so we don't mutate the tree mid-walk. Deepest paths first
    # so files move before we try to clean up their (now-empty) flat parents.
    offenders = [p for p in root.rglob("*") if "\\" in p.name]
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


def _get_available_memory_bytes() -> int:
    """Return available system memory in bytes via /proc/meminfo."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024  # kB → bytes
    except (OSError, ValueError):
        pass
    return 4 * 1024 ** 3  # conservative 4 GB fallback


class ExtractionMemoryBudget:
    """Gate concurrent extractions by estimated memory usage.

    Each extraction must ``acquire(size)`` before starting and ``release(size)``
    when finished.  *acquire* blocks until enough budget is available **and**
    live system memory confirms headroom.

    The budget is the lesser of *max_budget_bytes* and (available RAM at init
    minus *safety_margin_bytes*).  A 1.5× spike factor is applied to each
    request to account for transient memory spikes during decompression.

    *max_workers* caps the number of concurrent extractions regardless of
    memory (the caller still needs a thread-pool of this size).

    A floor of 1 ensures at least one extraction can always proceed, even if
    the estimated size exceeds the budget (otherwise a single large archive
    would deadlock the pipeline).
    """

    SPIKE_FACTOR = 1.5  # headroom multiplier for decompression spikes

    def __init__(self, max_workers: int = 4,
                 safety_margin_bytes: int = 1024 * 1024 * 1024,
                 max_budget_bytes: int | None = None):
        avail = _get_available_memory_bytes()
        auto_budget = max(0, avail - safety_margin_bytes)
        self._budget = min(auto_budget, max_budget_bytes) if max_budget_bytes else auto_budget
        self._reserved: int = 0
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._semaphore = threading.Semaphore(max(1, max_workers))

    @property
    def budget(self) -> int:
        return self._budget

    def acquire(self, estimated_bytes: int) -> None:
        """Reserve *estimated_bytes* (with spike factor) of extraction budget.

        Blocks until budget and a worker slot are available.  If the request
        is larger than the total budget, it is allowed through once all other
        reservations have drained (prevents deadlock on single huge archives).
        A live memory check adds a second safety net: even if the bookkeeping
        says there is room, we wait if the OS reports less than 1 GB free.
        """
        cost = int(estimated_bytes * self.SPIKE_FACTOR)
        self._semaphore.acquire()
        with self._cv:
            while True:
                fits_budget = (
                    self._reserved + cost <= self._budget
                    or self._reserved == 0  # allow oversized archive when alone
                )
                # Live memory check — even if budget bookkeeping says OK, wait
                # if the system is actually low on RAM (< 1 GB free).
                live_ok = _get_available_memory_bytes() >= 1024 * 1024 * 1024
                if fits_budget and live_ok:
                    break
                self._cv.wait(timeout=2.0)  # re-check periodically
            self._reserved += cost

    def release(self, estimated_bytes: int) -> None:
        """Return *estimated_bytes* (with spike factor) to the budget pool."""
        cost = int(estimated_bytes * self.SPIKE_FACTOR)
        with self._cv:
            self._reserved = max(0, self._reserved - cost)
            self._cv.notify_all()
        self._semaphore.release()

# py7zr / liblzma is not safe to run in multiple threads simultaneously —
# concurrent extractions can segfault.  Serialize all py7zr calls globally.
_py7zr_lock = threading.Lock()
from pathlib import Path
from datetime import datetime

import py7zr

from gui.dialogs import (
    _ReplaceModDialog,
    _SelectFilesDialog,
    _SetPrefixDialog,
    queue_rename_after_install,
)
import customtkinter as ctk
from gui.theme import BG_DEEP
from gui.fomod_dialog import FomodDialog
from gui.bain_dialog import BainDialog
from gui.mod_name_utils import _strip_title_metadata, _suggest_mod_names, sanitize_mod_folder_name
from Utils.fomod_parser import detect_fomod, detect_scripted_fomod, parse_module_config, parse_mod_info
from Utils.fomod_installer import resolve_files, check_module_dependencies
from Utils.bain_installer import detect_bain, resolve_bain_files, bain_unwrap_single_folder
from Utils.ui_config import load_dev_mode, load_rename_mod_after_install
from Utils.config_paths import get_fomod_selections_path, get_bain_selections_path
from Utils.plugins import read_plugins, append_plugin, read_loadorder, write_loadorder, PluginEntry
from Utils.modlist import prepend_mod, ensure_mod_preserving_position, read_modlist, write_modlist, ModEntry
from Utils.profile_state import read_separator_locks, write_separator_locks
from Utils.re_bundle import (
    detect_re_bundle, detect_multi_mod, BundleLayout,
    layout_to_spec, merge_bundle_spec, read_bundle_spec, write_bundle_spec,
    materialize_selection, BUNDLE_LIB_DIR,
)
from Utils.filemap import _scan_dir, update_mod_index
from Utils.bsa_filemap import update_bsa_index
from Nexus.nexus_meta import write_meta, read_meta, resolve_nexus_meta_for_archive
from gui.ctk_components import CTkNotification


def _try_modio_resolve(game, archive_path, meta_path, log_fn) -> bool:
    """BG3-only: identify a mod on mod.io and stamp its meta.ini keys.

    Runs independently of the Nexus lookup (a mod can be on both sites, often
    with identical md5s, and the user may prefer to update via mod.io).  Writes
    are non-destructive, so any Nexus keys already present are preserved.  The
    mod.io modules live in the (space-named) BG3 folder and aren't importable,
    so they're loaded by file path.  Returns True if mod.io metadata was written.
    """
    try:
        if getattr(game, "game_id", "") != "baldurs_gate_3":
            return False

        import importlib.util
        import sys as _sys
        from pathlib import Path as _Path

        bg3_dir = _Path(__file__).resolve().parent.parent / "Games" / "Baldur's Gate 3"

        def _load(stem):
            mn = f"{stem}_bg3"
            cached = _sys.modules.get(mn)
            if cached is not None:
                return cached
            spec = importlib.util.spec_from_file_location(mn, str(bg3_dir / f"{stem}.py"))
            mod = importlib.util.module_from_spec(spec)
            _sys.modules[mn] = mod
            spec.loader.exec_module(mod)
            return mod

        api_key = _load("modio_key").load_modio_key()
        if not api_key:
            log_fn("mod.io: no API key configured (set one in the mod.io wizard) — skipping.")
            return False

        modio_meta = _load("modio_meta")
        meta = modio_meta.resolve_modio_meta(
            archive_path=_Path(archive_path),
            staging_dir=_Path(meta_path).parent,
            api_key=api_key,
            log_fn=log_fn,
        )
        if meta and meta.mod_id > 0:
            modio_meta.write_modio_meta(_Path(meta_path), meta)
            return True
    except Exception as e:
        log_fn(f"mod.io: fallback failed — {e}")
    return False


def _run_dialog_on_main(parent_window, factory, result_holder: list,
                        done_event: threading.Event, result_attr: str | None = None) -> None:
    """Run on main thread via after(0, ...). Creates dialog via factory(parent), waits, stores result."""
    try:
        dlg = factory(parent_window)
        try:
            if dlg.winfo_exists():
                parent_window.wait_window(dlg)
        except Exception:
            pass
        result_holder[0] = getattr(dlg, result_attr) if result_attr else dlg
    except Exception:
        result_holder[0] = None
    finally:
        done_event.set()


def _show_replace_dialog_on_main(parent_window, mod_name: str,
                                 result_holder: list, done_event: threading.Event,
                                 suggestions: list[str] | None = None,
                                 rename_conflict: str | None = None) -> None:
    _run_dialog_on_main(
        parent_window,
        lambda p: _ReplaceModDialog(p, mod_name, suggestions=suggestions,
                                    rename_conflict=rename_conflict),
        result_holder, done_event)


def _prompt_replace_dialog(parent_window, mod_name: str, staging_root: Path,
                           suggestions: list[str] | None):
    """Show the replace-mod dialog, re-prompting if the user picks a rename
    target that also already exists. Returns the final dialog (with .result
    and .new_name populated) or None on cancel/timeout."""
    rename_conflict: str | None = None
    while True:
        if threading.current_thread() is threading.main_thread():
            dlg = _ReplaceModDialog(parent_window, mod_name,
                                    suggestions=suggestions,
                                    rename_conflict=rename_conflict)
            parent_window.wait_window(dlg)
        else:
            with _interactive_dialog_lock:
                _rh: list = [None]
                _ev = threading.Event()
                _conflict = rename_conflict
                parent_window.after(0, lambda: _show_replace_dialog_on_main(
                    parent_window, mod_name, _rh, _ev,
                    suggestions=suggestions, rename_conflict=_conflict))
                _ev.wait()
                dlg = _rh[0]
        if dlg is None or dlg.result != "rename":
            return dlg
        candidate = staging_root / dlg.new_name
        if not candidate.exists():
            return dlg
        rename_conflict = dlg.new_name


def _show_select_files_dialog_on_main(parent_window, file_list: list,
                                      result_holder: list, done_event: threading.Event) -> None:
    _run_dialog_on_main(parent_window,
                        lambda p: _SelectFilesDialog(p, file_list),
                        result_holder, done_event)


def _show_set_prefix_dialog_on_main(parent_window, required, file_list, mod_name: str,
                                    result_holder: list, done_event: threading.Event) -> None:
    _run_dialog_on_main(parent_window,
                        lambda p: _SetPrefixDialog(p, required, file_list, mod_name=mod_name),
                        result_holder, done_event, result_attr="result")


def _launch_fomod_dialog(parent_window, config, mod_root,
                         installed_files: set, active_files: set,
                         saved_selections, selections_path, on_done,
                         loose_files: set | None = None) -> None:
    """Create a FomodDialog overlay on the mod-panel container, with a popout
    toggle that re-hosts it in a separate window. Must run on the main thread.

    Tk can't reparent a live widget across toplevels, so the popout/dock toggle
    tears down the current FomodDialog and builds a fresh one in the other host,
    carrying its in-progress state. *on_done* fires exactly once (finish/cancel),
    regardless of how many times the user pops out and docks."""
    import traceback as _tb
    try:
        container = getattr(parent_window, '_mod_panel_container', None) or parent_window

        # Mutable cell holding the live popout Toplevel (None when docked).
        popout_win: list = [None]
        _caller_on_done = on_done

        def on_done(result):
            # Tear down a lingering popout window if the install resolved while
            # popped out.
            win = popout_win[0]
            if win is not None:
                popout_win[0] = None
                try:
                    win.destroy()
                except Exception:
                    pass
            _caller_on_done(result)

        def build(state: dict | None, popped_out: bool):
            """(Re)create the FomodDialog in the requested host."""
            # Always start by clearing any existing popout window — when docking,
            # the dialog inside it has already been destroyed by the toggle.
            win = popout_win[0]
            if win is not None:
                popout_win[0] = None
                try:
                    win.destroy()
                except Exception:
                    pass

            def on_rehost(new_state, going_to_popout):
                build(new_state, going_to_popout)

            common = dict(
                installed_files=installed_files,
                active_files=active_files,
                loose_files=loose_files,
                saved_selections=saved_selections,
                selections_path=selections_path,
                on_done=on_done,
                on_rehost=on_rehost,
                is_popped_out=popped_out,
                initial_state=state,
            )

            if popped_out:
                host = ctk.CTkToplevel(parent_window, fg_color=BG_DEEP)
                host.title(config.name or "FOMOD Installer")
                # Size to roughly the main window so the layout matches.
                try:
                    root = parent_window.winfo_toplevel()
                    root.update_idletasks()
                    w = max(int(root.winfo_width() * 0.85), 720)
                    h = max(int(root.winfo_height() * 0.85), 520)
                    x = root.winfo_rootx() + (root.winfo_width() - w) // 2
                    y = root.winfo_rooty() + (root.winfo_height() - h) // 2
                    host.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
                except Exception:
                    host.geometry("960x640")
                host.minsize(640, 480)
                host.grid_rowconfigure(0, weight=1)
                host.grid_columnconfigure(0, weight=1)
                # Closing the popout window cancels the install (mirrors the
                # overlay's Cancel button, so the worker thread doesn't hang).
                host.protocol("WM_DELETE_WINDOW",
                              lambda: panel_ref[0]._on_cancel()
                              if panel_ref[0] is not None else on_done(None))
                popout_win[0] = host

                panel = FomodDialog(host, config, mod_root, **common)
                panel.grid(row=0, column=0, sticky="nsew")
                panel_ref[0] = panel
                try:
                    # Deliberately NOT transient/topmost: a transient window
                    # stays stacked above its parent on most Linux WMs, which
                    # makes the manager underneath unusable. We want an ordinary
                    # independent window the user can freely raise/lower. Just
                    # bring it to the front once on open.
                    host.attributes("-topmost", False)
                    host.lift()
                    host.focus_force()
                    panel.focus_set()
                except Exception:
                    _tb.print_exc()
            else:
                panel = FomodDialog(container, config, mod_root, **common)
                panel_ref[0] = panel
                try:
                    if panel.winfo_exists():
                        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
                        panel.lift()
                        panel.focus_set()
                except Exception:
                    _tb.print_exc()

        # Holds the currently-live FomodDialog so the popout window's close
        # button can route through its cancel path.
        panel_ref: list = [None]

        build(None, popped_out=False)
    except Exception:
        _tb.print_exc()
        on_done(None)


def _show_fomod_dialog_on_main(parent_window, config, mod_root,
                               installed_files: set, active_files: set,
                               saved_selections, selections_path,
                               result_holder: list, done_event: threading.Event,
                               loose_files: set | None = None) -> None:
    """Worker-thread entry point: marshal the FOMOD dialog onto the main thread
    and resolve the worker's done_event when the user finishes/cancels."""
    def _on_done(result):
        result_holder[0] = result
        done_event.set()

    _launch_fomod_dialog(parent_window, config, mod_root,
                         installed_files, active_files,
                         saved_selections, selections_path, _on_done,
                         loose_files=loose_files)


def _show_bain_dialog_on_main(parent_window, subpackages, mod_root,
                              readme_text, saved_selections, selections_path,
                              result_holder: list, done_event: threading.Event,
                              mod_name: str | None = None) -> None:
    """Run on main thread. Creates a BainDialog overlay on the mod-panel container."""
    import traceback as _tb
    try:
        container = getattr(parent_window, '_mod_panel_container', None) or parent_window

        def on_done(result):
            result_holder[0] = result
            done_event.set()

        panel = BainDialog(container, subpackages, mod_root,
                           readme_text=readme_text,
                           saved_selections=saved_selections,
                           selections_path=selections_path,
                           on_done=on_done,
                           mod_name=mod_name)
        try:
            if panel.winfo_exists():
                panel.place(relx=0, rely=0, relwidth=1, relheight=1)
                panel.lift()
                panel.focus_set()
        except Exception:
            _tb.print_exc()
    except Exception:
        _tb.print_exc()
        result_holder[0] = None
        done_event.set()


def _show_mod_notification(parent_window, message: str, state: str = "success") -> None:
    """Show a notification at bottom-right, auto-dismiss after 4 s.

    Always schedules on the main Tk thread: install_mod_from_archive is often
    invoked from a worker thread; creating CTkToplevel off-thread breaks
    geometry (e.g. square instead of the intended bar shape).
    """
    def _show():
        try:
            root = parent_window.winfo_toplevel()
            notif = CTkNotification(root, state=state, message=message)
            root.after(4000, notif.destroy)
        except Exception:
            pass

    try:
        parent_window.after(0, _show)
    except Exception:
        pass


def _apply_strip_prefixes_to_file_list(
    file_list: list[tuple[str, str, bool]],
    strip_prefixes: set[str],
) -> list[tuple[str, str, bool]]:
    """
    Strip leading path segments from each dst_rel that match strip_prefixes
    (case-insensitive), repeatedly until the first segment is not in the set.
    """
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


def _check_mod_top_level(file_list: list[tuple[str, str, bool]],
                         required: set[str]) -> bool:
    """Return True if at least one file's top-level folder matches a required name."""
    for _, dst_rel, _ in file_list:
        top = dst_rel.replace("\\", "/").split("/")[0].lower()
        if top in required:
            return True
    return False


def _try_auto_strip_top_level(
    file_list: list[tuple[str, str, bool]],
    required: set[str],
    max_strip_depth: int = 20,
) -> tuple[list[tuple[str, str, bool]], bool]:
    """
    Try stripping leading path segments until at least one file has a top-level
    folder in required. Returns (new_file_list, True) if a strip depth worked,
    otherwise (original file_list, False).
    """
    required_lower = {r.lower() for r in required}
    if _check_mod_top_level(file_list, required_lower):
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


def _check_mod_top_level_file_types(
    file_list: list[tuple[str, str, bool]],
    required_exts: set[str],
) -> bool:
    """Return True if at least one top-level file (no sub-folder) has a required extension."""
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


def _try_auto_strip_for_file_types(
    file_list: list[tuple[str, str, bool]],
    required_exts: set[str],
    max_strip_depth: int = 20,
) -> tuple[list[tuple[str, str, bool]], bool]:
    """
    Try stripping leading path segments until at least one top-level file has a
    required extension.  Returns (new_file_list, True) if successful, otherwise
    (original file_list, False).
    """
    if _check_mod_top_level_file_types(file_list, required_exts):
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


def _stamp_meta_install_date(meta_ini_path: Path, installation_file: str = "",
                              is_fomod: bool = False, is_bain: bool = False) -> None:
    """Write the current datetime as the ``installed`` key in meta.ini if not
    already present.  Also write ``installationFile`` if *installation_file* is
    given and the key is not yet set (MO2-compatible).  If *is_fomod* is True,
    also set ``FOMOD=True``; if *is_bain* is True, set ``BAIN=True`` so the
    modlist panel can identify how the mod was installed."""
    import configparser as _cp
    parser = _cp.ConfigParser()
    if meta_ini_path.is_file():
        parser.read(str(meta_ini_path), encoding="utf-8")
    if not parser.has_section("General"):
        parser.add_section("General")
    changed = False
    if not parser.get("General", "installed", fallback=""):
        parser.set("General", "installed",
                   datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
        changed = True
    if installation_file and not parser.get("General", "installationFile", fallback=""):
        parser.set("General", "installationFile", installation_file)
        changed = True
    if is_fomod and parser.get("General", "FOMOD", fallback="").lower() != "true":
        parser.set("General", "FOMOD", "True")
        changed = True
    if is_bain and parser.get("General", "BAIN", fallback="").lower() != "true":
        parser.set("General", "BAIN", "True")
        changed = True
    if changed:
        meta_ini_path.parent.mkdir(parents=True, exist_ok=True)
        with open(meta_ini_path, "w", encoding="utf-8") as fh:
            parser.write(fh)


def _expand_folders_for_dialog(
    file_list: list[tuple[str, str, bool]], src_root: str
) -> list[tuple[str, str, bool]]:
    """
    Expand any is_folder=True entries into individual file entries so the
    _SelectFilesDialog can show real files instead of opaque folder names.
    """
    result = []
    root = Path(src_root)
    for src_rel, dst_rel, is_folder in file_list:
        if not is_folder:
            result.append((src_rel, dst_rel, False))
            continue
        src_dir = root / src_rel if src_rel else root
        if not src_dir.is_dir():
            result.append((src_rel, dst_rel, True))  # fallback: keep as-is
            continue
        for entry in sorted(src_dir.rglob("*")):
            if entry.is_file():
                file_src_rel = str(entry.relative_to(root))
                rel_to_src = entry.relative_to(src_dir)
                file_dst_rel = str(Path(dst_rel) / rel_to_src) if dst_rel else str(rel_to_src)
                result.append((file_src_rel, file_dst_rel, False))
    return result


def _unwrap_single_folder(extract_dir: str) -> str:
    """If extract_dir contains exactly one subdirectory and no files, return
    that subdirectory's path.  Archives like 'ModName.zip' that contain a
    single top-level folder 'ModName/' would otherwise hide their contents
    from bundle/multi-mod detection."""
    root = Path(extract_dir)
    entries = list(root.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return str(entries[0])
    return extract_dir


def _find_fomod_archive(extract_dir: str) -> str | None:
    """Return the path to a `.fomod` file inside *extract_dir* if the archive
    is just a wrapper around one (optionally peeling a single top-level
    folder).  A `.fomod` is itself a renamed 7z/zip — Nexus packages older
    Fallout/Oblivion mods this way and they need a second extraction pass
    before FOMOD detection can find `fomod/ModuleConfig.xml`."""
    root = Path(_unwrap_single_folder(extract_dir))
    fomods = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() == ".fomod"]
    if len(fomods) == 1:
        siblings = [p for p in root.iterdir() if p != fomods[0]]
        if all(s.is_file() and s.suffix.lower() in (".txt", ".md", ".rtf", ".jpg", ".png", ".pdf") for s in siblings):
            return str(fomods[0])
    return None


def _extract_fomod_archive(fomod_path: str, dest_dir: str, log_fn) -> bool:
    """Extract a `.fomod` (7z or zip in disguise) into *dest_dir*.  Tries
    native 7z, then bsdtar, then py7zr, then zipfile — same fallback chain
    used for regular .7z archives."""
    import subprocess
    _7z_bin = shutil.which("7zzs") or shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")
    if _7z_bin:
        r = subprocess.run(
            [_7z_bin, "x", fomod_path, f"-o{dest_dir}", "-y", "-mmt=on"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        if r.returncode == 0:
            log_fn("Extracted .fomod with 7z.")
            return True
        log_fn(f"7z failed on .fomod ({r.stderr.strip()}), trying bsdtar…")
    if shutil.which("bsdtar"):
        r = subprocess.run(
            ["bsdtar", "-xf", fomod_path, "-C", dest_dir],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        if r.returncode == 0 and any(os.scandir(dest_dir)):
            log_fn("Extracted .fomod with bsdtar.")
            return True
        log_fn(f"bsdtar failed on .fomod ({r.stderr.strip()}), trying py7zr…")
    try:
        with _py7zr_lock:
            with py7zr.SevenZipFile(fomod_path, "r") as z:
                z.extractall(dest_dir)
        log_fn("Extracted .fomod with py7zr.")
        return True
    except Exception as e:
        log_fn(f"py7zr failed on .fomod ({e}), trying zipfile…")
    try:
        with zipfile.ZipFile(fomod_path, "r") as z:
            z.extractall(dest_dir)
        log_fn("Extracted .fomod with zipfile.")
        return True
    except Exception as e:
        log_fn(f"zipfile failed on .fomod ({e}).")
    return False


def _resolve_direct_files(extract_dir: str) -> list[tuple[str, str, bool]]:
    """
    For a non-FOMOD archive, return every file as a (src, dst, is_folder)
    tuple where src and dst are both relative to the archive root.

    A top-level ``fomod/`` folder is skipped: it holds installer metadata
    (ModuleConfig.xml, script.cs, info.xml, screenshots) — not game content.
    This matters for scripted (C#) FOMODs like older Nexus ``.fomod`` packages
    which we can't run as a wizard and fall through to a plain install; without
    this the whole fomod/ folder would deploy into the game's Data dir as junk.
    """
    result = []
    root = Path(extract_dir)
    for entry in root.rglob("*"):
        if entry.is_file():
            rel = str(entry.relative_to(root))
            first = rel.replace("\\", "/").split("/", 1)[0]
            if first.lower() == "fomod":
                continue
            result.append((rel, rel, False))
    return result


def _resolve_src_case(src_root: Path, src_rel: str,
                      _cache: "dict[Path, dict[str, str]] | None" = None) -> Path:
    """
    Build src_root / src_rel while resolving each path component case-insensitively
    against what actually exists on disk.  FOMOD XML is written on Windows (case-
    insensitive) so source paths like "00 SOS\\FULL\\" may not match the real
    capitalisation on a Linux filesystem (e.g. "00 SOS/Full/").

    Pass a shared dict as ``_cache`` to avoid re-scanning the same directories
    when resolving many paths under the same root.
    """
    if _cache is None:
        _cache = {}
    parts = src_rel.replace("\\", "/").strip("/").split("/")
    current = src_root
    for part in parts:
        if not part:
            continue
        if current not in _cache:
            try:
                _cache[current] = {p.name.lower(): p.name for p in current.iterdir() if current.is_dir()}
            except OSError:
                _cache[current] = {}
        resolved = _cache[current].get(part.lower(), part)
        current = current / resolved
    return current


def _resolve_dst_case(dest_root: Path, dst_rel: str,
                      _cache: "dict[Path, dict[str, str]] | None" = None) -> Path:
    """
    Build dest_root / dst_rel while resolving each path component case-insensitively
    against what already exists on disk.  This prevents FOMOD installs from creating
    duplicate folders that differ only in case (e.g. 'Interface' vs 'interface') when
    running on a case-sensitive Linux filesystem.

    Pass a shared dict as ``_cache`` to avoid re-scanning the same directories
    when resolving many paths under the same root.
    """
    if _cache is None:
        _cache = {}
    parts = dst_rel.replace("\\", "/").split("/")
    current = dest_root
    for part in parts:
        if not part:
            continue
        if current not in _cache:
            try:
                _cache[current] = {p.name.lower(): p.name for p in current.iterdir() if current.is_dir()}
            except OSError:
                _cache[current] = {}
        resolved = _cache[current].get(part.lower(), part)
        current = current / resolved
    return current


def _link_or_copy(src: str | os.PathLike, dst: str | os.PathLike) -> None:
    """
    Place src at dst using a hardlink when the two are on the same filesystem,
    otherwise fall back to a byte-for-byte copy.  Hardlinks are near-instant
    and cost zero additional disk space, which roughly halves install time
    (and peak disk usage) for large archives that get extracted next to the
    staging folder.  Cross-device (EXDEV) and filesystems that don't support
    hardlinks (ENOSYS/EPERM) transparently fall through to shutil.copy2, so
    the small-archive tmpfs path behaves exactly as before.
    """
    try:
        os.link(src, dst)
        return
    except OSError:
        # EXDEV (cross-device), ENOSYS / EPERM (fs doesn't support hardlinks),
        # or any other link-time failure — fall back to a real copy.
        try:
            shutil.copy2(src, dst)
        except FileNotFoundError:
            # dst vanished between copyfile and copystat (concurrent worker
            # unlinked the same path). Retry once — the retry reclaims dst.
            shutil.copy2(src, dst)


def _copytree_case_insensitive(src: Path, dst: Path) -> int:
    """
    Recursively copy src into dst, resolving every destination directory
    component case-insensitively against what already exists on disk.
    This is a case-aware replacement for shutil.copytree(dirs_exist_ok=True).
    Returns the number of files copied.
    """
    copied = 0
    dst.mkdir(parents=True, exist_ok=True)
    for entry in os.scandir(src):
        # Resolve this entry's name against what already exists in dst
        try:
            existing = {p.name.lower(): p.name for p in dst.iterdir()}
        except OSError:
            existing = {}
        resolved_name = existing.get(entry.name.lower(), entry.name)
        child_dst = dst / resolved_name
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


def _copy_file_list(file_list: list[tuple[str, str, bool]],
                    src_root: str, dest_root: Path, log_fn) -> None:
    """
    Copy each (source, destination, is_folder) entry from src_root into dest_root.
    Folder entries use the recursive case-insensitive copytree (serial).
    File-only entries are copied in parallel for speed on large mods.
    """
    from concurrent.futures import ThreadPoolExecutor
    import threading as _threading

    folder_copied = 0
    file_entries: list[tuple[Path, Path]] = []  # (src, dst) pairs ready to copy

    _src_cache: dict[Path, dict[str, str]] = {}
    _dst_cache: dict[Path, dict[str, str]] = {}
    _src_root_path = Path(src_root)

    for src_rel, dst_rel, is_folder in file_list:
        # For direct installs src_rel == dst_rel and the path was produced by rglob
        # on the local filesystem — no case resolution needed on the source side.
        if src_rel and src_rel == dst_rel:
            src = _src_root_path / src_rel.replace("\\", "/")
        elif src_rel:
            src = _resolve_src_case(_src_root_path, src_rel, _src_cache)
        else:
            src = _src_root_path

        dst = _resolve_dst_case(dest_root, dst_rel, _dst_cache) if dst_rel else dest_root / dst_rel

        if is_folder:
            if not dst_rel:
                dst = dest_root
            if src.is_dir():
                folder_copied += _copytree_case_insensitive(src, dst)
                # Invalidate the cache for dst's parent so that subsequent
                # path resolutions pick up directories just created above.
                _dst_cache.pop(dst.parent, None)
        else:
            if not dst_rel:
                dst = dest_root / src.name
            elif dst_rel.endswith("/") or dst_rel.endswith("\\"):
                dst = _resolve_dst_case(dest_root, dst_rel.rstrip("/\\"), _dst_cache) / src.name
            if src.is_file():
                file_entries.append((src, dst))

    # Dedupe by destination (case-insensitive) so two workers never race on the
    # same dst — that race manifested as FileNotFoundError in shutil.copy2's
    # copystat step when one thread unlinked a dst mid-copy. Keep the last
    # entry per dst, which matches FOMOD priority (later wins).
    if file_entries:
        _by_dst: dict[str, tuple[Path, Path]] = {}
        for src, dst in file_entries:
            _by_dst[str(dst).lower()] = (src, dst)
        file_entries = list(_by_dst.values())

    # Pre-create all destination directories (serial — avoids mkdir races).
    dirs_seen: set[Path] = set()
    for _, dst in file_entries:
        d = dst.parent
        if d not in dirs_seen:
            d.mkdir(parents=True, exist_ok=True)
            dirs_seen.add(d)

    _copy_count = _threading.local()

    def _copy_one(src_dst: tuple[Path, Path]) -> None:
        src, dst = src_dst
        if dst.is_dir():
            shutil.rmtree(dst)
        elif dst.exists() or dst.is_symlink():
            # Unlink first so we don't punch through a hardlink shared with
            # another mod's staging file (deploy uses hardlinks on this setup).
            try:
                dst.unlink()
            except PermissionError:
                dst.chmod(0o644)
                dst.unlink()
        _link_or_copy(src, dst)

    _COPY_WORKERS = 8
    with ThreadPoolExecutor(max_workers=_COPY_WORKERS) as pool:
        # map() streams results lazily — far less memory overhead than submit()+as_completed()
        # when file_entries is large (tens of thousands of files).
        for _ in pool.map(_copy_one, file_entries, chunksize=256):
            pass

    copied = folder_copied + len(file_entries)
    log_fn(f"Copied {copied} item(s) to staging area.")


FOMOD_DEFERRED = "__FOMOD_DEFERRED__"
BAIN_DEFERRED = "__BAIN_DEFERRED__"


def _fire_on_installed(cb, is_fomod: bool = False,
                       installed_mod_name: str | None = None) -> None:
    """Invoke an `on_installed` callback with backwards-compatible signature.

    Callers may supply a no-arg callable, one that accepts ``is_fomod``,
    or one that also accepts ``installed_mod_name``. Try the richest form
    first, then fall back."""
    if cb is None:
        return
    for kwargs in (
        {"is_fomod": is_fomod, "installed_mod_name": installed_mod_name},
        {"is_fomod": is_fomod},
        {},
    ):
        try:
            cb(**kwargs)
            return
        except TypeError:
            continue
        except Exception:
            return


def _maybe_queue_rename_after_install(parent_window, mod_panel, headless: bool,
                                      mod_name: str, suggestions: list[str]) -> None:
    """Schedule a post-install rename prompt if the option is enabled.

    Collection installs pass ``headless=True`` and are deliberately skipped.
    The queue helper ensures only one dialog is visible at a time even when
    parallel workers finish close together.
    """
    if headless or mod_panel is None or parent_window is None or not mod_name:
        return
    try:
        if not load_rename_mod_after_install():
            return
    except Exception:
        return
    try:
        queue_rename_after_install(parent_window, mod_panel, mod_name, suggestions)
    except Exception:
        pass


def install_mod_from_archive(archive_path: str, parent_window, log_fn,
                             game, mod_panel=None,
                             on_installed=None,
                             fomod_auto_selections: "dict | None" = None,
                             bain_auto_selections: "dict | None" = None,
                             prebuilt_meta=None,
                             disable_extract: bool = False,
                             profile_dir=None,
                             headless: bool = False,
                             preferred_name: str = "",
                             skip_index_update: bool = False,
                             overwrite_existing: "bool | None" = None,
                             progress_fn=None,
                             clear_progress_fn=None,
                             defer_interactive_fomod: bool = False,
                             defer_interactive_bain: bool = False,
                             suppress_notification: bool = False,
                             skip_reload: bool = False) -> None:
    """
    Extract archive to a temp directory, detect FOMOD, run the wizard if
    present, then copy the resolved files into the game's mod staging area.
    Supports .zip, .7z, and .tar.* formats.

    on_installed : optional callable
        Called after a successful install, before the function returns.
        Use this to e.g. delete the source archive or refresh the UI.
        May be defined as either ``f()`` or ``f(is_fomod: bool)`` —
        when a keyword ``is_fomod`` is accepted, it indicates the mod
        was installed via a FOMOD installer.

    fomod_auto_selections : dict | None
        When provided, the FOMOD wizard is skipped entirely and these
        pre-resolved selections are passed straight to ``resolve_files()``.
        Format: ``{step_name: {group_name: [plugin_name, ...]}}``
        (same structure as ``saved_selections`` / ``FomodDialog.result``).
        Intended for collection installs where the author has already
        chosen the FOMOD options.

    skip_reload : bool
        When True, the per-install ``mod_panel.reload_after_install`` call is
        suppressed.  Used by batch operations (e.g. Quick Update) that install
        many mods back-to-back and trigger a single reload at the end instead of
        one full reload per mod, which lags the UI on large modlists.

    disable_extract : bool
        When True, skip extraction entirely.  The archive file is moved as-is
        into the mod staging folder (inside a folder named after the archive
        stem) instead of being extracted.  Useful for games that expect mods
        to remain in zip/archive format.
    """
    ext = archive_path.lower()
    raw_stem = os.path.splitext(os.path.basename(archive_path))[0]
    if raw_stem.endswith(".tar"):
        raw_stem = os.path.splitext(raw_stem)[0]

    suggestions = _suggest_mod_names(raw_stem)
    mod_name = preferred_name.strip() if preferred_name.strip() else (suggestions[0] if suggestions else raw_stem)
    # Ensure the on-disk folder name is Wine/Windows-addressable: trailing dots
    # or spaces and reserved characters make the folder invisible to Wine tools
    # (xEdit, PGPatcher, …) and break deploy into Wine prefixes.
    mod_name = sanitize_mod_folder_name(mod_name)

    # Collection installers tag SKSE-style mods (install_type="dinput") as
    # root_folder=True. Those mods are a mix of game-root files (skse_loader.exe)
    # and Data/-tree files (Scripts/, src/), so the standard "strip Data/" pass
    # would dump the Data subtree at game root. Preserve the archive layout
    # verbatim — deploy_root_folder will then place everything at the right
    # depth (Data/Scripts/* lands under <game>/Data/, loose .exe at <game>/).
    _is_root_install = bool(getattr(prebuilt_meta, "root_folder", False)) if prebuilt_meta is not None else False

    # ------------------------------------------------------------------
    # Disable-extract mode: move the archive as-is into the mod folder.
    # ------------------------------------------------------------------
    _preserved_endorsed = False
    if disable_extract:
        try:
            dest_root = game.get_effective_mod_staging_path() / mod_name
            was_existing_mod = dest_root.exists()
            if dest_root.exists():
                _existing_meta_path = dest_root / "meta.ini"
                if _existing_meta_path.exists():
                    try:
                        _preserved_endorsed = bool(read_meta(_existing_meta_path).endorsed)
                    except Exception:
                        pass
                def _force_remove(func, path, _exc):
                    os.chmod(path, 0o700)
                    func(path)
                if overwrite_existing:
                    # Auto Replace-All (Quick Update / collection overwrite).
                    shutil.rmtree(dest_root, onexc=_force_remove)
                else:
                    replace_dialog = _prompt_replace_dialog(
                        parent_window, mod_name,
                        game.get_effective_mod_staging_path(), suggestions)
                    if replace_dialog is None or replace_dialog.result == "cancel":
                        log_fn(f"Install cancelled — '{mod_name}' already exists.")
                        return
                    if replace_dialog.result == "rename":
                        mod_name = replace_dialog.new_name
                        dest_root = game.get_effective_mod_staging_path() / mod_name
                        was_existing_mod = False
                    elif replace_dialog.result == "all":
                        shutil.rmtree(dest_root, onexc=_force_remove)
            dest_root.mkdir(parents=True, exist_ok=True)
            archive_filename = os.path.basename(archive_path)
            dest_file = dest_root / archive_filename
            if dest_file.exists():
                dest_file.unlink()
            shutil.copy2(archive_path, dest_file)
            log_fn(f"Installed '{mod_name}' (no extract) → {dest_root}")

            _stamp_meta_install_date(dest_root / "meta.ini",
                                     installation_file=archive_filename)

            _ne_meta_path = dest_root / "meta.ini"
            _ne_archive = Path(archive_path)
            _ne_game_domain = getattr(game, "nexus_game_domain", "")
            if prebuilt_meta is not None:
                try:
                    prebuilt_meta.installation_file = archive_filename
                    if _preserved_endorsed:
                        prebuilt_meta.endorsed = True
                    write_meta(_ne_meta_path, prebuilt_meta)
                except OSError:
                    pass
            elif _ne_game_domain and _ne_archive.is_file():
                def _detect_meta_no_extract():
                    try:
                        app = None
                        try:
                            app = parent_window.winfo_toplevel()
                        except Exception:
                            pass
                        api = getattr(app, "_nexus_api", None) if app else None
                        meta = resolve_nexus_meta_for_archive(
                            _ne_archive, _ne_game_domain,
                            api=api,
                            log_fn=lambda m: (
                                app.after(0, lambda msg=m: log_fn(msg))
                                if app else None
                            ),
                        )
                        if meta:
                            write_meta(_ne_meta_path, meta)
                            msg = f"Nexus: Saved metadata for '{mod_name}' (mod {meta.mod_id})"
                            if app:
                                app.after(0, lambda: log_fn(msg))
                        # Always also try mod.io (a mod can be on both sites).
                        _try_modio_resolve(
                            game, _ne_archive, _ne_meta_path,
                            lambda m: (app.after(0, lambda msg=m: log_fn(msg))
                                       if app else None),
                        )
                    except Exception:
                        pass
                threading.Thread(target=_detect_meta_no_extract, daemon=True).start()

            if mod_panel is not None and mod_panel._modlist_path is not None:
                modlist_path = mod_panel._modlist_path
            else:
                profile_dir = game.get_profile_root() / "profiles" / "default"
                modlist_path = profile_dir / "modlist.txt"

            if was_existing_mod:
                ensure_mod_preserving_position(modlist_path, mod_name, enabled=True)
            else:
                prepend_mod(modlist_path, mod_name, enabled=True)

            log_fn(f"Added '{mod_name}' to modlist.")
            if not suppress_notification:
                _show_mod_notification(parent_window, f"Installed: {mod_name}")

            # Disable-extract path never runs a FOMOD installer.
            _fire_on_installed(on_installed, is_fomod=False,
                               installed_mod_name=mod_name)

            if mod_panel is not None and not skip_reload:
                mod_panel.after(0, mod_panel.reload_after_install)
            _maybe_queue_rename_after_install(
                parent_window, mod_panel, headless=False,
                mod_name=mod_name, suggestions=suggestions,
            )
            return mod_name
        except Exception as e:
            import traceback
            log_fn(f"Install error: {e}")
            log_fn(traceback.format_exc())
        return None

    # Use the system temp dir (usually a tmpfs) only when it has enough
    # headroom for this archive plus a 512 MB safety margin.  A module-level
    # lock + reservation counter prevents parallel workers from all racing to
    # claim the same free space before any of them has started writing.
    # We query the real uncompressed size from archive metadata so that archives
    # with extreme compression ratios (e.g. 700 MB → 8 GB texture mods) don't
    # overflow /tmp.  A fallback multiplier is used only when metadata is
    # unavailable (e.g. solid .7z archives where 7z listing is slow/unavailable).
    global _tmp_space_reserved
    try:
        _archive_size = os.path.getsize(archive_path)
    except OSError:
        _archive_size = 0

    _extract_size_estimate = get_uncompressed_size(archive_path, _archive_size)
    _staging = game.get_effective_mod_staging_path()
    _tmp_claimed = False
    with _tmp_space_lock:
        try:
            _tmp_stat = os.statvfs(tempfile.gettempdir())
            _tmp_free = _tmp_stat.f_frsize * _tmp_stat.f_bavail
            _tmp_headroom = 512 * 1024 * 1024  # keep 512 MB free in /tmp
            _use_tmp = _extract_size_estimate + _tmp_headroom + _tmp_space_reserved < _tmp_free
        except OSError:
            _use_tmp = False
        if _use_tmp:
            _tmp_space_reserved += _extract_size_estimate
            _tmp_claimed = True
    if _use_tmp:
        _tmp_parent = None  # let mkdtemp use the default /tmp
    else:
        _tmp_parent = _staging.parent if _staging else None
    # Resolve a concrete on-disk fallback parent (the staging filesystem) so
    # that if the preferred location can't be created we never silently land
    # back on the default tmpfs /tmp — overflowing that small ramdisk is what
    # surfaces as "Disk Quota Exceeded" (EDQUOT) for large mods.
    _disk_parent = _staging.parent if _staging else None
    extract_dir = None
    for _candidate in (_tmp_parent, _disk_parent, None):
        try:
            if _candidate is not None:
                Path(_candidate).mkdir(parents=True, exist_ok=True)
            extract_dir = tempfile.mkdtemp(prefix="modmgr_", dir=_candidate or None)
        except OSError:
            continue
        # If we fell through to the default /tmp but it's a small ramdisk that
        # can't hold the extraction, discard it and keep trying disk parents.
        if (
            _candidate is None
            and _extract_size_estimate
            and _path_is_tmpfs(extract_dir)
        ):
            try:
                _free = os.statvfs(extract_dir)
                if (_free.f_frsize * _free.f_bavail
                        < _extract_size_estimate + 512 * 1024 * 1024):
                    shutil.rmtree(extract_dir, ignore_errors=True)
                    extract_dir = None
                    continue
            except OSError:
                pass
        break
    if extract_dir is None:
        # Last resort — let mkdtemp pick the default location.
        extract_dir = tempfile.mkdtemp(prefix="modmgr_")

    # Record where the archive is being unpacked, and how much room is there,
    # so disk-full / "Disk Quota Exceeded" failures are diagnosable from the log.
    try:
        _st = os.statvfs(extract_dir)
        _free_gb = (_st.f_frsize * _st.f_bavail) / (1024 ** 3)
        _loc = "ramdisk" if _path_is_tmpfs(extract_dir) else "disk"
        log_fn(f"Extracting to {extract_dir} ({_loc}, {_free_gb:.1f} GB free)")
    except OSError:
        log_fn(f"Extracting to {extract_dir}")

    try:
        if not os.path.isfile(archive_path):
            raise FileNotFoundError(
                f"Archive not found (may have been deleted after a prior install): "
                f"'{os.path.basename(archive_path)}'"
            )

        # .dazip (Dragon Age DLC package) and .override (DAO-Modmanager) are
        # renamed ZIP archives — extract them with the ZIP path. The DAO handler's
        # install logic then restructures the unpacked tree.
        if ext.endswith((".zip", ".dazip", ".override")):
            import subprocess
            _zip_done = False
            # For large ZIPs, prefer native tools (7z or bsdtar) over Python's
            # single-threaded zipfile — they use C/multi-threaded extraction.
            _archive_mb = os.path.getsize(archive_path) / (1024 * 1024)
            _7z_bin = shutil.which("7zzs") or shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")
            _bsdtar_bin = shutil.which("bsdtar")
            _has_native = _7z_bin or _bsdtar_bin
            # Python's zipfile is pure-Python and single-threaded, so use it
            # only for very small archives (where native-tool subprocess
            # overhead dominates) or when no native tool is available.
            _use_python_zip = _archive_mb < 10 or not _has_native
            if _use_python_zip:
                try:
                    log_fn("Extracting with zipfile…")
                    with zipfile.ZipFile(archive_path, "r") as z:
                        members = z.infolist()
                        # Normalise Windows backslash paths so Linux extractors
                        # create the correct folder hierarchy instead of treating
                        # the whole path as a single filename.
                        _has_backslash = any("\\" in m.filename for m in members)
                        if _has_backslash:
                            for m in members:
                                m.filename = m.filename.replace("\\", "/")
                        if progress_fn is not None:
                            progress_fn(0, 0, "Extracting…")
                        z.extractall(extract_dir, members)
                    _zip_done = True
                except Exception as e_zip:
                    log_fn(f"zipfile failed ({e_zip}), retrying with native tools…")
            if not _zip_done and _7z_bin:
                shutil.rmtree(extract_dir, ignore_errors=True)
                os.makedirs(extract_dir, exist_ok=True)
                if progress_fn is not None:
                    progress_fn(0, 0, "Extracting…")
                result = subprocess.run(
                    [_7z_bin, "x", archive_path, f"-o{extract_dir}", "-y", "-mmt=on"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                )
                if result.returncode == 0:
                    _zip_done = True
                    log_fn("Extracted with 7z.")
                else:
                    log_fn(f"7z failed ({result.stderr.strip()}), retrying with bsdtar…")
            if not _zip_done and _bsdtar_bin:
                shutil.rmtree(extract_dir, ignore_errors=True)
                os.makedirs(extract_dir, exist_ok=True)
                if progress_fn is not None:
                    progress_fn(0, 0, "Extracting…")
                result = subprocess.run(
                    [_bsdtar_bin, "-xf", archive_path, "-C", extract_dir],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                )
                if result.returncode == 0:
                    _zip_done = True
                    log_fn("Extracted with bsdtar.")
                else:
                    log_fn(f"bsdtar failed ({result.stderr.strip()}).")
            if not _zip_done:
                # Last resort: Python zipfile if native tools failed or weren't tried
                try:
                    shutil.rmtree(extract_dir, ignore_errors=True)
                    os.makedirs(extract_dir, exist_ok=True)
                    log_fn("Extracting with zipfile (fallback)…")
                    with zipfile.ZipFile(archive_path, "r") as z:
                        members = z.infolist()
                        if any("\\" in m.filename for m in members):
                            for m in members:
                                m.filename = m.filename.replace("\\", "/")
                        if progress_fn is not None:
                            progress_fn(0, 0, "Extracting…")
                        z.extractall(extract_dir, members)
                    _zip_done = True
                except Exception as e_zip2:
                    raise RuntimeError(
                        f"All extraction methods failed for ZIP: {e_zip2}\n"
                        "The archive may use a compression method (e.g. "
                        "deflate64) that requires the '7z' binary. Install "
                        "the 'p7zip' package or use the Flatpak build."
                    )
            # Native extractors (7z/bsdtar) reproduce Windows backslash member
            # names as literal flat filenames; repair them into a real tree so
            # the rest of the install pipeline can resolve the paths.
            if _zip_done:
                _debackslash_extracted_tree(extract_dir, log_fn)
        elif ext.endswith(".7z"):
            import subprocess
            _7z_done = False
            _archive_bytes = os.path.getsize(archive_path)
            _archive_mb = _archive_bytes / (1024 * 1024)
            # Prefer native 7z binary (multi-threaded) or bsdtar (native C)
            # over py7zr which is single-threaded and globally serialized.
            _7z_bin = shutil.which("7zzs") or shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")
            _bsdtar_bin = shutil.which("bsdtar")
            if _7z_bin:
                if progress_fn is not None:
                    progress_fn(0, 0, "Extracting…")
                result = subprocess.run(
                    [_7z_bin, "x", archive_path, f"-o{extract_dir}", "-y", "-mmt=on"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                )
                # If extraction ran out of room on a small tmpfs /tmp (surfaces
                # as "Disk Quota Exceeded" / EDQUOT or ENOSPC), re-route to the
                # on-disk staging filesystem and retry once before giving up.
                if (result.returncode != 0
                        and _is_disk_full_error(result.stderr)
                        and _staging is not None
                        and _path_is_tmpfs(extract_dir)):
                    log_fn("Extraction filled the temp ramdisk — retrying on disk…")
                    extract_dir = _reroute_extract_dir(extract_dir, _staging.parent)
                    result = subprocess.run(
                        [_7z_bin, "x", archive_path, f"-o{extract_dir}", "-y", "-mmt=on"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                    )
                if result.returncode == 0:
                    _7z_done = True
                    log_fn("Extracted with 7z.")
                else:
                    log_fn(f"7z failed ({result.stderr.strip()}), trying next method…")
            if not _7z_done and _bsdtar_bin:
                shutil.rmtree(extract_dir, ignore_errors=True)
                os.makedirs(extract_dir, exist_ok=True)
                if progress_fn is not None:
                    progress_fn(0, 0, "Extracting…")
                result = subprocess.run(
                    [_bsdtar_bin, "-xf", archive_path, "-C", extract_dir],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                )
                if result.returncode == 0:
                    _7z_done = True
                    log_fn("Extracted with bsdtar.")
                else:
                    log_fn(f"bsdtar failed ({result.stderr.strip()}), trying py7zr…")
            if not _7z_done:
                # Fallback to py7zr — single-threaded and serialized, but works
                # when no native tools are available.
                # Check available RAM; keep a 512 MB safety margin.
                _avail_mb = _get_available_memory_bytes() / (1024 * 1024)
                _py7zr_safe = _archive_mb < (_avail_mb - 512)
                if _py7zr_safe:
                    try:
                        log_fn("Extracting with py7zr…")
                        if progress_fn is not None:
                            progress_fn(0, 0, "Extracting…")
                        with _py7zr_lock:
                            with py7zr.SevenZipFile(archive_path, "r") as z:
                                z.extractall(extract_dir)
                        _7z_done = True
                    except Exception as e7:
                        log_fn(f"py7zr failed ({e7}).")
                else:
                    log_fn(f"Archive is {_archive_mb:.0f} MB, only {_avail_mb:.0f} MB RAM available — skipping py7zr to avoid OOM.")
            if not _7z_done:
                raise RuntimeError(f"All extraction methods failed for 7z archive.")
        elif any(ext.endswith(s) for s in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar")):
            import subprocess
            _tar_done = False
            # Prefer native multi-threaded tools over Python's single-threaded
            # tarfile module — especially important for .tar.xz which is slow
            # to decompress in pure Python.
            _7z_bin = shutil.which("7zzs") or shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")
            _bsdtar_bin = shutil.which("bsdtar")
            if _bsdtar_bin:
                # bsdtar handles every tar variant natively and is the fastest
                # option for streaming tar decompression.
                log_fn("Extracting with bsdtar…")
                if progress_fn is not None:
                    progress_fn(0, 0, "Extracting…")
                result = subprocess.run(
                    [_bsdtar_bin, "-xf", archive_path, "-C", extract_dir],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                )
                if result.returncode == 0:
                    _tar_done = True
                    log_fn("Extracted with bsdtar.")
                else:
                    log_fn(f"bsdtar failed ({result.stderr.strip()}), trying 7z…")
            if not _tar_done and _7z_bin:
                # 7z needs two passes for .tar.gz/.tar.xz/.tar.bz2 (decompress,
                # then untar).  For a plain .tar a single pass suffices.
                if ext.endswith(".tar"):
                    shutil.rmtree(extract_dir, ignore_errors=True)
                    os.makedirs(extract_dir, exist_ok=True)
                    if progress_fn is not None:
                        progress_fn(0, 0, "Extracting…")
                    result = subprocess.run(
                        [_7z_bin, "x", archive_path, f"-o{extract_dir}", "-y", "-mmt=on"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                    )
                    if result.returncode == 0:
                        _tar_done = True
                        log_fn("Extracted with 7z.")
                    else:
                        log_fn(f"7z failed ({result.stderr.strip()}), trying tarfile…")
                else:
                    # Decompress to an intermediate .tar inside extract_dir,
                    # then untar that into extract_dir and remove the .tar.
                    shutil.rmtree(extract_dir, ignore_errors=True)
                    os.makedirs(extract_dir, exist_ok=True)
                    if progress_fn is not None:
                        progress_fn(0, 0, "Extracting…")
                    _stage_dir = tempfile.mkdtemp(prefix="modmgr_tar_",
                                                  dir=os.path.dirname(extract_dir) or None)
                    try:
                        r1 = subprocess.run(
                            [_7z_bin, "x", archive_path, f"-o{_stage_dir}", "-y", "-mmt=on"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                        )
                        if r1.returncode != 0:
                            log_fn(f"7z decompress failed ({r1.stderr.strip()}), trying tarfile…")
                        else:
                            _inner_tar = None
                            for _n in os.listdir(_stage_dir):
                                if _n.lower().endswith(".tar"):
                                    _inner_tar = os.path.join(_stage_dir, _n)
                                    break
                            if _inner_tar:
                                r2 = subprocess.run(
                                    [_7z_bin, "x", _inner_tar, f"-o{extract_dir}", "-y", "-mmt=on"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                )
                                if r2.returncode == 0:
                                    _tar_done = True
                                    log_fn("Extracted with 7z.")
                                else:
                                    log_fn(f"7z untar failed ({r2.stderr.strip()}), trying tarfile…")
                    finally:
                        shutil.rmtree(_stage_dir, ignore_errors=True)
            if not _tar_done:
                shutil.rmtree(extract_dir, ignore_errors=True)
                os.makedirs(extract_dir, exist_ok=True)
                log_fn("Extracting with tarfile (fallback)…")
                if progress_fn is not None:
                    progress_fn(0, 0, "Extracting…")
                with tarfile.open(archive_path, "r:*") as t:
                    t.extractall(extract_dir, filter="fully_trusted")
        elif ext.endswith(".rar"):
            import subprocess
            # Prefer native unrar — bsdtar/libarchive cannot handle RAR5
            # ("Declared dictionary size is not supported") and rarfile
            # delegates to whatever backend it finds, which on sandboxed
            # installs is often bsdtar.  Native unrar handles every RAR
            # variant, so try it first.  On Debian-family distros "unrar"
            # may actually be unrar-free (no RAR5), so fall through to
            # 7-Zip (official 7zz extracts RAR fine) and unar before the
            # rarfile/bsdtar last resorts.
            _rar_done = False
            if shutil.which("unrar"):
                log_fn("Extracting with unrar…")
                if progress_fn is not None:
                    progress_fn(0, 0, "Extracting…")
                result = subprocess.run(
                    ["unrar", "x", "-y", archive_path, extract_dir + os.sep],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                )
                if result.returncode != 0:
                    log_fn(f"unrar failed ({result.stderr.strip()}), trying 7z…")
                else:
                    _rar_done = True
            if not _rar_done:
                _7z_rar_bin = (shutil.which("7zzs") or shutil.which("7zz")
                               or shutil.which("7z") or shutil.which("7za"))
                if _7z_rar_bin:
                    shutil.rmtree(extract_dir, ignore_errors=True)
                    os.makedirs(extract_dir, exist_ok=True)
                    log_fn("Extracting with 7z…")
                    if progress_fn is not None:
                        progress_fn(0, 0, "Extracting…")
                    result = subprocess.run(
                        [_7z_rar_bin, "x", archive_path, f"-o{extract_dir}", "-y"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                    )
                    if result.returncode == 0:
                        _rar_done = True
                    else:
                        log_fn(f"7z failed ({result.stderr.strip()}), trying unar…")
            if not _rar_done and shutil.which("unar"):
                shutil.rmtree(extract_dir, ignore_errors=True)
                os.makedirs(extract_dir, exist_ok=True)
                log_fn("Extracting with unar…")
                if progress_fn is not None:
                    progress_fn(0, 0, "Extracting…")
                result = subprocess.run(
                    ["unar", "-force-overwrite", "-no-directory",
                     "-output-directory", extract_dir, archive_path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                )
                if result.returncode == 0:
                    _rar_done = True
                else:
                    log_fn(f"unar failed ({result.stderr.strip()}), trying rarfile…")
            if not _rar_done:
                try:
                    import rarfile
                    log_fn("Extracting with rarfile…")
                    if progress_fn is not None:
                        progress_fn(0, 0, "Extracting…")
                    with rarfile.RarFile(archive_path, "r") as r:
                        r.extractall(extract_dir)
                    _rar_done = True
                except ImportError:
                    pass
                except Exception as e_rar:
                    log_fn(f"rarfile failed ({e_rar}), trying bsdtar…")
            if not _rar_done:
                log_fn("Extracting with bsdtar…")
                if progress_fn is not None:
                    progress_fn(0, 0, "Extracting…")
                result = subprocess.run(
                    ["bsdtar", "-xf", archive_path, "-C", extract_dir],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        f"bsdtar failed: {result.stderr.strip()}\n"
                        "This archive likely requires 'unrar' to extract. "
                        "Install the 'unrar' package or use the Flatpak build."
                    )
                log_fn("Extracted with bsdtar.")
            # Sanity check: an extractor returning success with an empty
            # output directory means it silently skipped unsupported
            # entries (libarchive's RAR5 behaviour).  Treat as failure so
            # the caller doesn't delete the source archive.
            if not any(os.scandir(extract_dir)):
                raise RuntimeError(
                    f"RAR extraction produced no files — the archive "
                    f"format may be unsupported. Install 'unrar' and retry."
                )
        else:
            log_fn(f"Unsupported archive format: {os.path.basename(archive_path)}")
            log_fn("Supported formats: .zip, .7z, .rar, .tar.gz")
            return None

        # If the archive only contained a `.fomod` file (a renamed 7z/zip used
        # by older Fallout/Oblivion FOMOD packages), extract it again and use
        # the inner contents as the new working tree.  Without this pass,
        # detect_fomod() finds nothing and the install silently produces an
        # empty mod.
        _fomod_archive = _find_fomod_archive(extract_dir)
        if _fomod_archive:
            log_fn(f"Archive contains a .fomod wrapper — extracting {os.path.basename(_fomod_archive)}…")
            _inner_dir = tempfile.mkdtemp(
                prefix="modmgr_fomod_",
                dir=os.path.dirname(extract_dir) or None,
            )
            if _extract_fomod_archive(_fomod_archive, _inner_dir, log_fn):
                _outer_dir = extract_dir
                extract_dir = _inner_dir
                shutil.rmtree(_outer_dir, ignore_errors=True)
            else:
                shutil.rmtree(_inner_dir, ignore_errors=True)
                raise RuntimeError(f"Failed to extract inner .fomod archive: {os.path.basename(_fomod_archive)}")

        is_fomod_install = False
        is_bain_install = False
        fomod_result = detect_fomod(extract_dir)
        if not fomod_result and detect_scripted_fomod(extract_dir):
            # C#-scripted FOMOD (fomod/script.cs, no ModuleConfig.xml). The
            # XML installer dialog can't run these, so the mod is installed as
            # a plain full-file copy with no option prompts. Warn the user so
            # they know to look for an XML/manual version if they need choices.
            _scripted_warn = (
                "This is a scripted (C#) FOMOD installer, which this manager "
                "cannot run. All files have been installed without showing any "
                "install options.\n\nIf you need to choose optional components, "
                "look for an XML-based or manually-packaged version of this mod "
                "on Nexus.")
            log_fn("WARNING: " + _scripted_warn.replace("\n\n", " "))
            if parent_window is not None:
                try:
                    from gui.dialogs import show_warning
                    parent_window.after(0, lambda: show_warning(
                        "Scripted FOMOD", _scripted_warn,
                        parent=parent_window, height=260))
                except Exception:
                    pass  # log warning above is the fallback
        if fomod_result:
            mod_root, config_path = fomod_result
            config = parse_module_config(config_path)

            # info.xml <Name> is set by the mod author to the specific variant
            # name and is more reliable than ModuleConfig.xml <moduleName>,
            # which mod authors sometimes copy from a sibling variant and forget
            # to update.  Prefer info.xml when it exists and differs.
            _info_path = str(Path(config_path).parent / "info.xml")
            _mod_info = parse_mod_info(_info_path)
            if _mod_info.name and _mod_info.name != config.name:
                config.name = _mod_info.name

            if config.name:
                fomod_clean = _strip_title_metadata(config.name)
                seen = set()
                new_suggestions = []
                for c in (fomod_clean, config.name, *suggestions):
                    if c and c not in seen:
                        seen.add(c)
                        new_suggestions.append(c)
                suggestions = new_suggestions
                # Only let the FOMOD name override mod_name when the caller
                # didn't supply an explicit preferred_name (collection installs
                # use preferred_name to keep mods from the same page distinct).
                # Also skip the override when the filename already encodes a
                # specific variant name that differs from the FOMOD moduleName.
                # Example: file = "Security Overhaul SKSE - Regional Locks-…"
                #          FOMOD moduleName = "Security Overhaul SKSE - Add-ons"
                # Both share a common prefix but diverge — using the FOMOD name
                # would install into the wrong folder and load saved selections
                # from a different variant.
                # Rule: don't let the FOMOD name override when the filename name
                # and FOMOD name share a substantial common prefix but then
                # diverge (i.e. they are sibling variants, not the same thing).
                _orig_nexus_clean = re.sub(r"(-\d+)+$", "", raw_stem).strip()
                _filename_lower = _orig_nexus_clean.lower()
                _fomod_lower = fomod_clean.lower()
                # Find length of shared prefix (word-boundary aware: tokenize
                # on any non-alphanumeric run so punctuation-separated variants
                # like "Foo-Bar_Regional" vs "Foo-Bar_AddOns" split correctly).
                _fn_words = [w for w in re.split(r"[^a-z0-9]+", _filename_lower) if w]
                _fm_words = [w for w in re.split(r"[^a-z0-9]+", _fomod_lower) if w]
                _shared = 0
                for _a, _b in zip(_fn_words, _fm_words):
                    if _a == _b:
                        _shared += 1
                    else:
                        break
                # They are sibling variants if: they share words, but both have
                # additional words that differ from each other.
                _are_siblings = (
                    _shared > 0
                    and _shared < len(_fn_words)
                    and _shared < len(_fm_words)
                    and _fn_words[_shared:] != _fm_words[_shared:]
                )
                if not preferred_name.strip() and not _are_siblings:
                    mod_name = suggestions[0]

            installed_files: set[str] = set()
            active_files: set[str] = set()
            _plugins_dir = None
            if mod_panel is not None and getattr(mod_panel, "_modlist_path", None) is not None:
                _plugins_dir = mod_panel._modlist_path.parent
            elif profile_dir is not None:
                from pathlib import Path as _Path
                _plugins_dir = _Path(profile_dir)
            if _plugins_dir is not None:
                plugins_path = _plugins_dir / "plugins.txt"
                loadorder_path = _plugins_dir / "loadorder.txt"
                for entry in read_plugins(plugins_path):
                    installed_files.add(entry.name.lower())
                    if entry.enabled:
                        active_files.add(entry.name.lower())
                # Also add anything in loadorder.txt not already captured
                for name in read_loadorder(loadorder_path):
                    installed_files.add(name.lower())
            # Build the loose-file set from the current filemap (deployed mod
            # files). FOMOD <fileDependency> nodes may reference an arbitrary
            # asset path (e.g. "textures/foo.dds") rather than a plugin name;
            # MO2 evaluates those against the whole virtual file tree. The
            # filemap is "<rel_str>\t<mod_name>" per line, sitting next to
            # plugins.txt. Keys are lower-cased, forward-slash separated.
            loose_files: set[str] = set()
            if _plugins_dir is not None:
                _filemap_path = _plugins_dir / "filemap.txt"
                try:
                    with open(_filemap_path, "r", encoding="utf-8") as _fmf:
                        for _line in _fmf:
                            _rel = _line.split("\t", 1)[0].strip()
                            if _rel:
                                loose_files.add(_rel.replace("\\", "/").lower())
                except OSError:
                    pass

            # Add vanilla/DLC plugins. Engine loads these implicitly so they
            # never appear in plugins.txt. Use _vanilla_plugins_for_game which
            # prefers Data_Core/ (pristine backup) over Data/ (may contain
            # deployed mod plugins from a previous profile).
            if game is not None:
                try:
                    from gui.game_helpers import _vanilla_plugins_for_game
                    for _vname_lower in _vanilla_plugins_for_game(game).keys():
                        installed_files.add(_vname_lower)
                        active_files.add(_vname_lower)
                except Exception:
                    pass

            # <moduleDependencies> — pre-install gate. MO2 would block here;
            # we log prominently so the user knows the mod may not work, but
            # let the install proceed (the user may be installing prerequisites
            # out of order and will resolve it by enabling/installing other mods).
            _modgate_ok, _modgate_msg = check_module_dependencies(
                config, installed_files, active_files, loose_files
            )
            if not _modgate_ok:
                log_fn("WARNING: this mod's <moduleDependencies> gate is not "
                       "satisfied — the mod may not work until these are met:")
                for _line in _modgate_msg.splitlines():
                    log_fn(f"  {_line}")

            if fomod_auto_selections is None and defer_interactive_fomod:
                # Collection install: no auto-selections available — defer this
                # mod until all non-FOMOD mods have installed so its dependencies
                # are present before the user is prompted.
                log_fn("FOMOD installer detected — deferring until dependencies are installed.")
                return FOMOD_DEFERRED

            def _write_profile_fomod(selections):
                """Mirror fomod selections into <profile>/fomod/<mod>.json."""
                pdir = getattr(game, "_active_profile_dir", None)
                if not pdir:
                    return
                try:
                    pfomod = Path(pdir) / "fomod"
                    pfomod.mkdir(parents=True, exist_ok=True)
                    with open(pfomod / f"{mod_name}.json", "w", encoding="utf-8") as f:
                        json.dump(selections, f, indent=2)
                except OSError:
                    pass

            if fomod_auto_selections is not None:
                # Collection install: use the author's pre-chosen options,
                # skip the interactive wizard entirely. Write selections only
                # to the profile — never to the global config — so collection
                # choices don't clobber the user's manual FOMOD selections.
                log_fn("FOMOD installer detected — applying collection author's choices automatically.")
                final_selections = fomod_auto_selections
                game_name = getattr(game, "name", "")
                _write_profile_fomod(final_selections)
            else:
                log_fn("FOMOD installer detected — opening wizard...")
                saved_selections = None
                sel_path = None
                game_name = getattr(game, "name", "")
                if game_name:
                    sel_path = get_fomod_selections_path(game_name, mod_name)
                    if sel_path.is_file():
                        try:
                            with open(sel_path, "r", encoding="utf-8") as f:
                                saved_selections = json.load(f)
                            log_fn("Restored previous FOMOD selections.")
                        except (OSError, ValueError):
                            saved_selections = None

                if clear_progress_fn is not None:
                    clear_progress_fn()
                fomod_dialog_active.set()
                try:
                    if threading.current_thread() is threading.main_thread():
                        import tkinter as tk
                        _done_var = tk.BooleanVar(value=False)
                        _result_holder: list = [None]

                        def _on_done(result):
                            _result_holder[0] = result
                            _done_var.set(True)

                        _launch_fomod_dialog(parent_window, config, mod_root,
                                             installed_files, active_files,
                                             saved_selections, sel_path, _on_done,
                                             loose_files=loose_files)
                        try:
                            parent_window.wait_variable(_done_var)
                        except Exception:
                            import traceback as _tb; _tb.print_exc()
                        dialog_result = _result_holder[0]
                    else:
                        with _interactive_dialog_lock:
                            result_holder = [None]
                            done_event = threading.Event()
                            parent_window.after(
                                0,
                                lambda: _show_fomod_dialog_on_main(
                                    parent_window, config, mod_root,
                                    installed_files, active_files,
                                    saved_selections, sel_path,
                                    result_holder, done_event,
                                    loose_files=loose_files,
                                ),
                            )
                            done_event.wait()
                            dialog_result = result_holder[0]
                finally:
                    fomod_dialog_active.clear()

                if dialog_result is None:
                    log_fn("FOMOD install cancelled.")
                    return

                if game_name:
                    sel_path = get_fomod_selections_path(game_name, mod_name)
                    try:
                        with open(sel_path, "w", encoding="utf-8") as f:
                            json.dump(dialog_result, f, indent=2)
                    except OSError:
                        pass
                _write_profile_fomod(dialog_result)

                final_selections = dialog_result

            file_list = resolve_files(config, final_selections, installed_files,
                                      active_files, loose_files)
            is_fomod_install = True
            log_fn(f"FOMOD complete — {len(file_list)} file(s) to install.")

            if load_dev_mode():
                log_fn("[FOMOD DEV] === Selections ===")
                for step_key, group_map in final_selections.items():
                    # step_key may be a str(int) index or a step name
                    try:
                        step_idx = int(step_key)
                        step_label = config.steps[step_idx].name if step_idx < len(config.steps) else step_key
                    except (ValueError, IndexError):
                        step_label = step_key
                    for group_name, chosen in group_map.items():
                        log_fn(f"[FOMOD DEV]   Step '{step_label}' / Group '{group_name}': {chosen}")
                log_fn("[FOMOD DEV] === Files to install ===")
                for src, dst, is_folder in file_list:
                    kind = "[dir]" if is_folder else "[file]"
                    log_fn(f"[FOMOD DEV]   {kind} {src!r} → {dst!r}")
        elif getattr(game, "supports_bain", True) and (bain_subpkgs := detect_bain(
                bain_unwrap_single_folder(extract_dir),
                extra_exts=getattr(game, "plugin_extensions", None))):
            # --- BAIN (Wrye Bash bundled archive) install ---
            extract_dir = bain_unwrap_single_folder(extract_dir)
            mod_root = extract_dir
            log_fn(f"BAIN package detected — {len(bain_subpkgs)} sub-package(s).")

            # Optional package readme shown in the picker for context. Match
            # case-insensitively — BAIN packages are authored on Windows so the
            # file may be Package.txt, PACKAGE.TXT, etc. Note: wizard.txt is a
            # Wrye Bash installer *script* (non human-readable), not a readme, so
            # it is deliberately excluded here.
            readme_text = None
            _readme_names = ("package.txt", "readme.txt")
            try:
                _root_files = {e.name.lower(): e.path
                               for e in os.scandir(extract_dir) if e.is_file()}
            except OSError:
                _root_files = {}
            for _rn in _readme_names:
                _rp = _root_files.get(_rn)
                if _rp:
                    try:
                        readme_text = Path(_rp).read_text(encoding="utf-8",
                                                          errors="replace")
                    except OSError:
                        readme_text = None
                    break

            game_name = getattr(game, "name", "")
            sel_path = None
            saved_selections = None
            if game_name:
                sel_path = get_bain_selections_path(game_name, mod_name)
                if sel_path.is_file():
                    try:
                        with open(sel_path, "r", encoding="utf-8") as f:
                            saved_selections = json.load(f)
                        log_fn("Restored previous BAIN selections.")
                    except (OSError, ValueError):
                        saved_selections = None

            def _save_bain_selection(result):
                """Persist BAIN selection to both the global config sidecar and
                the active profile's bain/ folder (mirrors FOMOD)."""
                if game_name and sel_path is not None:
                    try:
                        with open(sel_path, "w", encoding="utf-8") as f:
                            json.dump(result, f, indent=2)
                    except OSError:
                        pass
                pdir = getattr(game, "_active_profile_dir", None)
                if pdir:
                    try:
                        pbain = Path(pdir) / "bain"
                        pbain.mkdir(parents=True, exist_ok=True)
                        with open(pbain / f"{mod_name}.json", "w",
                                  encoding="utf-8") as f:
                            json.dump(result, f, indent=2)
                    except OSError:
                        pass

            default_names = [p.name for p in bain_subpkgs if p.default_selected]
            if bain_auto_selections is None and defer_interactive_bain:
                # Collection install: no exported selections — defer this mod so
                # its picker is shown after all non-interactive mods install
                # (and before the deferred FOMODs).
                log_fn("BAIN installer detected — deferring until other mods are installed.")
                return BAIN_DEFERRED
            if bain_auto_selections is not None:
                # Workshop / collection install: apply the exported choices and
                # skip the interactive picker entirely.
                selected = bain_auto_selections.get("selected", [])
                _save_bain_selection(bain_auto_selections)
                log_fn("BAIN: applying exported selection automatically.")
            elif parent_window is None:
                # No UI available to show a picker (pure automation) — keep prior
                # choices if we have them, else per-package defaults (00 packages).
                selected = (saved_selections or {}).get("selected") or default_names
                log_fn("BAIN: non-interactive install — using "
                       + ("saved" if saved_selections else "default")
                       + " selection.")
            else:
                if clear_progress_fn is not None:
                    clear_progress_fn()
                fomod_dialog_active.set()
                try:
                    if threading.current_thread() is threading.main_thread():
                        import tkinter as tk
                        container = getattr(parent_window, '_mod_panel_container', None) or parent_window
                        _done_var = tk.BooleanVar(value=False)
                        _result_holder: list = [None]

                        def _on_done(result):
                            _result_holder[0] = result
                            _done_var.set(True)

                        panel = BainDialog(container, bain_subpkgs, mod_root,
                                           readme_text=readme_text,
                                           saved_selections=saved_selections,
                                           selections_path=sel_path,
                                           on_done=_on_done,
                                           mod_name=mod_name)
                        try:
                            if panel.winfo_exists():
                                panel.place(relx=0, rely=0, relwidth=1, relheight=1)
                                panel.lift()
                                panel.focus_set()
                                parent_window.wait_variable(_done_var)
                        except Exception:
                            import traceback as _tb; _tb.print_exc()
                        dialog_result = _result_holder[0]
                    else:
                        with _interactive_dialog_lock:
                            result_holder = [None]
                            done_event = threading.Event()
                            parent_window.after(
                                0,
                                lambda: _show_bain_dialog_on_main(
                                    parent_window, bain_subpkgs, mod_root,
                                    readme_text, saved_selections, sel_path,
                                    result_holder, done_event, mod_name,
                                ),
                            )
                            done_event.wait()
                            dialog_result = result_holder[0]
                finally:
                    fomod_dialog_active.clear()

                if dialog_result is None:
                    log_fn("BAIN install cancelled.")
                    return

                selected = dialog_result.get("selected", [])
                _save_bain_selection(dialog_result)

            file_list = resolve_bain_files(bain_subpkgs, set(selected))
            is_bain_install = True
            log_fn(f"BAIN complete — {len(selected)} sub-package(s), "
                   f"{len(file_list)} file(s) to install.")
        elif getattr(game, "mod_supports_bundles", False) and detect_re_bundle(_unwrap_single_folder(extract_dir)):
            # --- RE / Fluffy bundle install (single mod) ---
            # Installs as ONE normal mod: a single folder, meta.ini and modlist
            # row.  The original option folders are tucked into a hidden
            # <mod>/.mm_bundle/ library (invisible to the scanner); the selected
            # options are materialised (hardlinked) onto the mod root by
            # materialize_selection().  The bundle structure + selection live in
            # meta.ini's [Bundle] section; the Bundle Options dialog re-materialises
            # on change.  Downstream (scan/filemap/deploy/update) sees a normal mod.
            extract_dir = _unwrap_single_folder(extract_dir)
            layout: BundleLayout = detect_re_bundle(extract_dir)
            spec = layout_to_spec(layout)
            log_fn(f"Bundle detected: '{layout.bundle_name}' — {len(layout.groups)} group(s), "
                   f"{layout.variant_count} option(s). Installing as one mod '{mod_name}'.")

            if mod_panel is not None and mod_panel._modlist_path is not None:
                _profile_dir = mod_panel._modlist_path.parent
            elif profile_dir is not None:
                _profile_dir = profile_dir
            else:
                _profile_dir = game.get_profile_root() / "profiles" / "default"
            modlist_path = _profile_dir / "modlist.txt"

            dest_root = game.get_effective_mod_staging_path() / mod_name
            _bundle_was_existing = dest_root.exists()
            if _bundle_was_existing:
                # Reinstall / update: carry the user's saved option selection (and
                # order) onto the freshly detected spec before wiping the folder.
                # New options keep their default; removed options drop out.
                _old_spec = read_bundle_spec(dest_root / "meta.ini")
                if _old_spec is not None:
                    spec = merge_bundle_spec(spec, _old_spec)
                    log_fn("Bundle: preserved existing option selection across reinstall/update.")
                def _frc(func, path, _exc):
                    os.chmod(path, 0o700)
                    func(path)
                shutil.rmtree(dest_root, onexc=_frc)

            # Stash every extracted top-level folder under <mod>/.mm_bundle/.
            lib_dir = dest_root / BUNDLE_LIB_DIR
            lib_dir.mkdir(parents=True, exist_ok=True)
            for child in Path(extract_dir).iterdir():
                if child.is_dir():
                    _copy_file_list(_resolve_direct_files(str(child)),
                                    str(child), lib_dir / child.name, log_fn)

            # Persist the spec, materialise the default selection, stamp meta.
            meta_path = dest_root / "meta.ini"
            write_bundle_spec(meta_path, spec)
            materialize_selection(dest_root, spec)
            _stamp_meta_install_date(meta_path,
                                     installation_file=os.path.basename(archive_path))

            # Write Nexus metadata exactly like a normal install (preserves the
            # [Bundle] section).  Prebuilt meta is written directly; otherwise the
            # archive is resolved against the Nexus API in a background thread.
            _archive = Path(archive_path)
            _game_domain = getattr(game, "nexus_game_domain", "")
            if prebuilt_meta is not None:
                try:
                    prebuilt_meta.installation_file = os.path.basename(archive_path)
                    write_meta(meta_path, prebuilt_meta)
                    log_fn(f"Nexus: Saved metadata for '{mod_name}' (mod {prebuilt_meta.mod_id})")
                except OSError:
                    pass
            elif _game_domain and _archive.is_file():
                def _detect_bundle_meta(_mp=meta_path, _mn=mod_name):
                    try:
                        app = None
                        try:
                            app = parent_window.winfo_toplevel()
                        except Exception:
                            pass
                        api = getattr(app, "_nexus_api", None) if app else None
                        meta = resolve_nexus_meta_for_archive(
                            _archive, _game_domain, api=api,
                            log_fn=lambda m: (app.after(0, lambda msg=m: log_fn(msg)) if app else None),
                        )
                        if meta:
                            write_meta(_mp, meta)
                            msg = f"Nexus: Saved metadata for '{_mn}' (mod {meta.mod_id})"
                            if app:
                                app.after(0, lambda: log_fn(msg))
                        # Always also try mod.io (a mod can be on both sites).
                        _try_modio_resolve(
                            game, _archive, _mp,
                            lambda m: (app.after(0, lambda msg=m: log_fn(msg))
                                       if app else None),
                        )
                    except Exception:
                        pass
                threading.Thread(target=_detect_bundle_meta, daemon=True).start()

            # Index the mod normally — the scanner skips .mm_bundle/, so only the
            # materialised selection at the mod root is indexed.
            if not skip_index_update:
                try:
                    _strip_fs = frozenset() if _is_root_install else frozenset(
                        s.lower() for s in (getattr(game, "mod_folder_strip_prefixes", set()) or []))
                    _exts_fs  = frozenset(e.lower() for e in (getattr(game, "install_extensions", None) or []))
                    _root_fs  = frozenset(s.lower() for s in (getattr(game, "root_deploy_folders", None) or []))
                    _, _nf, _rf, _ = _scan_dir(mod_name, str(dest_root), _strip_fs, _exts_fs, _root_fs)
                    _index_path = _profile_dir / "modindex.bin"
                    _norm_case = getattr(game, "normalize_folder_case", True)
                    update_mod_index(_index_path, mod_name, _nf, _rf,
                                     normalize_folder_case=_norm_case)
                except Exception:
                    pass

            # Single modlist row.  A reinstall/update keeps its existing load-order
            # position (like any normal mod); a fresh install goes to the top.
            if _bundle_was_existing:
                ensure_mod_preserving_position(modlist_path, mod_name, enabled=True)
            else:
                prepend_mod(modlist_path, mod_name, enabled=True)

            log_fn(f"Installed bundle '{mod_name}' as one mod "
                   f"({len(spec.selected_folders())} of {layout.variant_count} option(s) active).")
            if not headless:
                _show_mod_notification(parent_window, f"Installed bundle: {mod_name}")
            _fire_on_installed(on_installed, is_fomod=False, installed_mod_name=mod_name)
            if mod_panel is not None and not headless and not skip_reload:
                mod_panel.after(0, mod_panel.reload_after_install)
            return mod_name
        elif getattr(game, "mod_supports_bundles", False) and detect_multi_mod(_unwrap_single_folder(extract_dir)):
            # --- Multi-mod archive: each subdir is a separate independent mod ---
            extract_dir = _unwrap_single_folder(extract_dir)
            multi_mods = detect_multi_mod(extract_dir)
            log_fn(f"Multi-mod archive detected: {len(multi_mods)} mod(s).")

            if mod_panel is not None and mod_panel._modlist_path is not None:
                _profile_dir = mod_panel._modlist_path.parent
            elif profile_dir is not None:
                _profile_dir = profile_dir
            else:
                _profile_dir = game.get_profile_root() / "profiles" / "default"
            modlist_path = _profile_dir / "modlist.txt"

            strip_prefixes     = getattr(game, "mod_folder_strip_prefixes", set())
            post_strip         = getattr(game, "mod_folder_strip_prefixes_post", set())
            required           = getattr(game, "mod_required_top_level_folders", set())
            auto_strip         = getattr(game, "mod_auto_strip_until_required", False)
            install_as_is      = getattr(game, "mod_install_as_is_if_no_match", False)
            install_prefix     = getattr(game, "mod_install_prefix", "")
            required_lower     = {r.lower() for r in required}

            installed_names: list[str] = []
            for m_name, m_path in multi_mods:
                m_file_list = _resolve_direct_files(m_path)
                if strip_prefixes and not _is_root_install:
                    m_file_list = _apply_strip_prefixes_to_file_list(m_file_list, strip_prefixes)
                if install_prefix and not _is_root_install:
                    _pfx = install_prefix.strip().strip("/").replace("\\", "/")
                    _pfx_parts = _pfx.lower().split("/")
                    new_mfl = []
                    for s, d, f in m_file_list:
                        d_parts = d.replace("\\", "/").split("/")
                        d_parts_lower = [p.lower() for p in d_parts]
                        if d_parts_lower[0] in required_lower:
                            new_mfl.append((s, d, f))
                            continue
                        match_len = 0
                        for i in range(len(_pfx_parts), 0, -1):
                            if d_parts_lower[:i] == _pfx_parts[-i:]:
                                match_len = i
                                break
                        missing = "/".join(_pfx.split("/")[:len(_pfx_parts) - match_len])
                        new_mfl.append((s, f"{missing}/{d}" if missing else d, f))
                    m_file_list = new_mfl
                if required and not _check_mod_top_level(m_file_list, required):
                    if auto_strip:
                        m_file_list, _ = _try_auto_strip_top_level(m_file_list, required)
                    if not m_file_list and install_as_is:
                        m_file_list = _resolve_direct_files(m_path)
                if post_strip and not _is_root_install:
                    m_file_list = _apply_strip_prefixes_to_file_list(m_file_list, post_strip)

                m_dest = game.get_effective_mod_staging_path() / m_name
                if m_dest.exists():
                    import shutil as _shutil
                    def _frc(func, path, _exc):
                        os.chmod(path, 0o700)
                        func(path)
                    _shutil.rmtree(m_dest, onexc=_frc)
                _copy_file_list(m_file_list, m_path, m_dest, log_fn)
                _stamp_meta_install_date(m_dest / "meta.ini",
                                         installation_file=os.path.basename(archive_path))

                if not skip_index_update:
                    try:
                        _strip_fs = frozenset() if _is_root_install else frozenset(s.lower() for s in (strip_prefixes or []))
                        _exts_fs  = frozenset(e.lower() for e in (getattr(game, "install_extensions", None) or []))
                        _root_fs  = frozenset(s.lower() for s in (getattr(game, "root_deploy_folders", None) or []))
                        _, _nf, _rf = _scan_dir(m_name, str(m_dest), _strip_fs, _exts_fs, _root_fs)
                        _index_path = _profile_dir / "modindex.bin"
                        _norm_case = getattr(game, "normalize_folder_case", True)
                        update_mod_index(_index_path, m_name, _nf, _rf, normalize_folder_case=_norm_case)
                    except Exception:
                        pass

                prepend_mod(modlist_path, m_name, enabled=True)
                log_fn(f"  Installed '{m_name}' → {m_dest}")
                installed_names.append(m_name)

            log_fn(f"Installed {len(multi_mods)} mod(s) from archive.")
            if not headless:
                _show_mod_notification(parent_window, f"Installed {len(multi_mods)} mods")
            _fire_on_installed(on_installed, is_fomod=False,
                               installed_mod_name=(installed_names[0] if installed_names else mod_name))
            if mod_panel is not None and not headless and not skip_reload:
                mod_panel.after(0, mod_panel.reload_after_install)
            return installed_names[0] if installed_names else mod_name
        else:
            mod_root = extract_dir
            file_list = _resolve_direct_files(extract_dir)
            log_fn(f"Direct install — {len(file_list)} file(s) to install.")

        dest_root = game.get_effective_mod_staging_path() / mod_name
        replace_selected_only = False
        replace_all = False
        overwrote_existing = False
        if dest_root.exists():
            _existing_meta_path = dest_root / "meta.ini"
            if _existing_meta_path.exists():
                try:
                    _preserved_endorsed = bool(read_meta(_existing_meta_path).endorsed)
                except Exception:
                    pass
            if overwrite_existing:
                # Auto Replace-All: delete the existing folder and reinstall
                # cleanly. Used by collection installs (headless) and by Quick
                # Update, where the name match is already confirmed so the
                # "Mod Already Exists" dialog would be pure friction.
                def _force_remove(func, path, _exc):
                    func(path)
                shutil.rmtree(dest_root, onexc=_force_remove)
                overwrote_existing = True
                log_fn(f"Install: removed existing '{mod_name}' for overwrite reinstall.")
            elif headless and overwrite_existing is None:
                # In headless (collection new-profile) installs, a pre-existing folder
                # means the mod was already installed (e.g. two collection entries share
                # the same archive, or a previous partial run installed it).
                # Just return the folder name — no dialog, no re-extraction.
                log_fn(f"Collection install: '{mod_name}' folder already exists — skipping re-install.")
                return mod_name
            else:
                replace_dialog = _prompt_replace_dialog(
                    parent_window, mod_name,
                    game.get_effective_mod_staging_path(), suggestions)
                if replace_dialog is None or replace_dialog.result == "cancel":
                    log_fn(f"Install cancelled — '{mod_name}' already exists.")
                    return
                if replace_dialog.result == "rename":
                    mod_name = replace_dialog.new_name
                    dest_root = game.get_effective_mod_staging_path() / mod_name
                elif replace_dialog.result == "selected":
                    replace_selected_only = True
                elif replace_dialog.result == "all":
                    replace_all = True

        if replace_selected_only:
            expanded = _expand_folders_for_dialog(file_list, mod_root)
            if threading.current_thread() is threading.main_thread():
                sel_dialog = _SelectFilesDialog(parent_window, expanded)
                parent_window.wait_window(sel_dialog)
            else:
                with _interactive_dialog_lock:
                    _rh2: list = [None]
                    _ev2 = threading.Event()
                    parent_window.after(0, lambda: _show_select_files_dialog_on_main(parent_window, expanded, _rh2, _ev2))
                    _ev2.wait()
                    sel_dialog = _rh2[0]
            if sel_dialog is None or sel_dialog.result is None:
                log_fn("Install cancelled — no files selected.")
                return None
            chosen = sel_dialog.result
            file_list = [(s, d, f) for s, d, f in expanded if d in chosen]
            log_fn(f"Replace selected: {len(file_list)} file(s) chosen.")

        strip_prefixes = getattr(game, "mod_folder_strip_prefixes", set())
        if strip_prefixes and not _is_root_install:
            file_list = _apply_strip_prefixes_to_file_list(file_list, strip_prefixes)

        required = getattr(game, "mod_required_top_level_folders", set())
        required_lower = {r.lower() for r in required}

        install_prefix = getattr(game, "mod_install_prefix", "")
        if install_prefix and not _is_root_install:
            install_prefix = install_prefix.strip().strip("/").replace("\\", "/")
            prefix_parts = install_prefix.lower().split("/")
            new_file_list = []
            for s, d, f in file_list:
                d_parts = d.replace("\\", "/").split("/")
                d_parts_lower = [p.lower() for p in d_parts]
                # Skip prefix if the top-level folder is already a required folder
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
        if not is_fomod_install and required and not _check_mod_top_level(file_list, required):
            if auto_strip:
                file_list, did_auto_strip = _try_auto_strip_top_level(file_list, required)
                if did_auto_strip:
                    log_fn("Auto-stripped top-level folder(s) so mod matches expected structure.")
            if not did_auto_strip and required_file_types:
                if _check_mod_top_level_file_types(file_list, required_file_types):
                    did_auto_strip = True
                    log_fn("Mod contains recognised top-level file type(s) — skipping prefix check.")
                elif auto_strip:
                    file_list, did_auto_strip = _try_auto_strip_for_file_types(file_list, required_file_types)
                    if did_auto_strip:
                        log_fn("Auto-stripped top-level folder(s) to expose recognised file type(s).")
            if not did_auto_strip:
                if install_as_is:
                    log_fn("Mod structure unrecognised — installing as-is (no prefix applied).")
                else:
                    dlg_result = None
                    if threading.current_thread() is threading.main_thread():
                        dlg = _SetPrefixDialog(parent_window, required, file_list, mod_name=mod_name)
                        parent_window.wait_window(dlg)
                        dlg_result = dlg.result
                    else:
                        with _interactive_dialog_lock:
                            result_holder = [None]
                            done_event = threading.Event()
                            parent_window.after(
                                0,
                                lambda: _show_set_prefix_dialog_on_main(
                                    parent_window, required, file_list, mod_name,
                                    result_holder, done_event,
                                ),
                            )
                            done_event.wait()
                            dlg_result = result_holder[0]
                    if dlg_result is None:
                        log_fn("Install cancelled — mod structure not mapped.")
                        return
                    action, prefix = dlg_result
                    if action == "prefix" and prefix:
                        prefix = prefix.strip().strip("/").replace("\\", "/")
                        file_list = [(s, f"{prefix}/{d}", f) for s, d, f in file_list]
                        log_fn(f"Remapped mod files under '{prefix}/'.")
        elif not is_fomod_install and not required and required_file_types and not _check_mod_top_level_file_types(file_list, required_file_types):
            if auto_strip:
                file_list, did_auto_strip = _try_auto_strip_for_file_types(file_list, required_file_types)
                if did_auto_strip:
                    log_fn("Auto-stripped top-level folder(s) to expose recognised file type(s).")
            if not did_auto_strip:
                if install_as_is:
                    log_fn("Mod structure unrecognised — installing as-is (no prefix applied).")
                else:
                    dlg_result = None
                    if threading.current_thread() is threading.main_thread():
                        dlg = _SetPrefixDialog(parent_window, set(), file_list, mod_name=mod_name)
                        parent_window.wait_window(dlg)
                        dlg_result = dlg.result
                    else:
                        with _interactive_dialog_lock:
                            result_holder = [None]
                            done_event = threading.Event()
                            parent_window.after(
                                0,
                                lambda: _show_set_prefix_dialog_on_main(
                                    parent_window, set(), file_list, mod_name,
                                    result_holder, done_event,
                                ),
                            )
                            done_event.wait()
                            dlg_result = result_holder[0]
                    if dlg_result is None:
                        log_fn("Install cancelled — mod structure not mapped.")
                        return
                    action, prefix = dlg_result
                    if action == "prefix" and prefix:
                        prefix = prefix.strip().strip("/").replace("\\", "/")
                        file_list = [(s, f"{prefix}/{d}", f) for s, d, f in file_list]
                        log_fn(f"Remapped mod files under '{prefix}/'.")

        post_strip_prefixes = getattr(game, "mod_folder_strip_prefixes_post", set())
        if post_strip_prefixes and not _is_root_install:
            file_list = _apply_strip_prefixes_to_file_list(file_list, post_strip_prefixes)

        dest_root = game.get_effective_mod_staging_path() / mod_name
        # overwrote_existing: the auto Replace-All path rmtree'd the folder
        # earlier, so exists() alone would misreport the mod as new and
        # prepend it to the top of the modlist instead of keeping its slot.
        was_existing_mod = dest_root.exists() or overwrote_existing
        if replace_all and dest_root.exists():
            def _force_remove(func, path, _exc):
                os.chmod(path, 0o700)
                func(path)
            shutil.rmtree(dest_root, onexc=_force_remove)
            log_fn(f"Cleared existing mod folder for clean reinstall.")
        _copy_file_list(file_list, mod_root, dest_root, log_fn)
        log_fn(f"Installed '{mod_name}' → {dest_root}")

        if is_fomod_install and load_dev_mode():
            log_fn("[FOMOD DEV] === Post-install verification ===")
            missing: list[str] = []
            for src, dst, is_folder in file_list:
                dst_norm = dst.replace("\\", "/").rstrip("/")
                if not dst_norm:
                    continue
                expected = dest_root / Path(dst_norm)
                if is_folder:
                    if not expected.is_dir():
                        missing.append(f"[dir]  {dst_norm!r}")
                else:
                    if not expected.is_file():
                        # Destination may have been a trailing-slash folder install
                        src_name = Path(src.replace("\\", "/")).name
                        alt = dest_root / Path(dst_norm) / src_name
                        if not alt.is_file():
                            missing.append(f"[file] {dst_norm!r}")
            if missing:
                log_fn(f"[FOMOD DEV] WARNING — {len(missing)} expected file(s) not found after install:")
                for m in missing:
                    log_fn(f"[FOMOD DEV]   MISSING {m}")
            else:
                log_fn(f"[FOMOD DEV] All {len(file_list)} FOMOD file(s) verified present.")

        _stamp_meta_install_date(dest_root / "meta.ini",
                                  installation_file=os.path.basename(archive_path),
                                  is_fomod=is_fomod_install,
                                  is_bain=is_bain_install)

        # Interactive == a normal single-mod install with a real UI. Collection
        # installs defer/auto-resolve their wizards and headless installs have no
        # UI, so hooks that pop dialogs (e.g. DAO's duplicate-override warning)
        # should stay silent in those modes.
        _interactive_install = not (headless
                                    or defer_interactive_fomod
                                    or defer_interactive_bain)
        for fn in getattr(game, "additional_install_logic", []):
            try:
                if "interactive" in inspect.signature(fn).parameters:
                    fn(dest_root, mod_name, log_fn,
                       interactive=_interactive_install)
                else:
                    fn(dest_root, mod_name, log_fn)
            except Exception as e:
                # A wizard hook may signal a user cancellation (marker attr) —
                # abort the whole install and clean up the staged folder rather
                # than leaving a half-installed mod in the modlist.
                if getattr(e, "install_cancelled", False):
                    log_fn(f"Install cancelled by user — removing '{mod_name}'.")
                    if not was_existing_mod:
                        def _force_remove(func, path, _exc):
                            os.chmod(path, 0o700)
                            func(path)
                        try:
                            shutil.rmtree(dest_root, onexc=_force_remove)
                        except OSError as rm_exc:
                            log_fn(f"  cleanup failed: {rm_exc}")
                    return
                log_fn(f"Additional install logic failed: {e}")

        # Resolve which profile directory to write modlist/plugins into.
        # Priority: explicit profile_dir arg > mod_panel's path > default.
        if mod_panel is not None and mod_panel._modlist_path is not None:
            _profile_dir = mod_panel._modlist_path.parent
        elif profile_dir is not None:
            _profile_dir = profile_dir
        else:
            _profile_dir = game.get_profile_root() / "profiles" / "default"
        modlist_path = _profile_dir / "modlist.txt"

        # Update the mod index for just this mod so the next filemap rebuild
        # reads from the index instead of rescanning all mod folders.
        # Skipped for collection installs (skip_index_update=True) — the
        # collection does one bulk rebuild_mod_index after all mods are done,
        # which is far faster than N concurrent read→merge→write passes.
        if not skip_index_update:
            try:
                _strip = frozenset(s.lower() for s in (getattr(game, "strip_prefixes", None) or []))
                _exts  = frozenset(e.lower() for e in (getattr(game, "install_extensions", None) or []))
                _root  = frozenset(s.lower() for s in (getattr(game, "root_deploy_folders", None) or []))
                _, normal_files, root_files = _scan_dir(
                    mod_name, str(dest_root), _strip, _exts, _root,
                )
                if mod_panel is not None and mod_panel._modlist_path is not None:
                    _ml = mod_panel._modlist_path
                else:
                    _ml = game.get_profile_root() / "profiles" / "default" / "modlist.txt"
                _index_path = _ml.parent / "modindex.bin"
                _norm_case = getattr(game, "normalize_folder_case", True)
                update_mod_index(_index_path, mod_name, normal_files, root_files,
                                 normalize_folder_case=_norm_case)
                # Incrementally update the BSA index too (Bethesda games only).
                _archive_exts = frozenset(getattr(game, "archive_extensions", frozenset()) or frozenset())
                if _archive_exts:
                    update_bsa_index(
                        _ml.parent / "bsa_index.bin", mod_name, dest_root, _archive_exts,
                    )
            except (OSError, ValueError, KeyError):
                pass  # non-fatal — next rebuild will fall back to a full rescan

        plugin_exts = getattr(game, "plugin_extensions", [])
        if plugin_exts and mod_panel is not None and mod_panel._modlist_path is not None:
            plugins_path = mod_panel._modlist_path.parent / "plugins.txt"
            loadorder_path = mod_panel._modlist_path.parent / "loadorder.txt"
            _sp = getattr(game, "plugins_use_star_prefix", True)
            exts_lower = {ext.lower() for ext in plugin_exts}
            new_plugins: list[str] = []
            if dest_root.is_dir():
                for entry in dest_root.iterdir():
                    if entry.is_file() and entry.suffix.lower() in exts_lower:
                        # Normalise extension to lowercase (e.g. plugin.ESP → plugin.esp)
                        if entry.suffix != entry.suffix.lower():
                            normalised = entry.with_suffix(entry.suffix.lower())
                            entry.rename(normalised)
                            entry = normalised
                        append_plugin(plugins_path, entry.name, enabled=True, star_prefix=_sp)
                        new_plugins.append(entry.name)
            if new_plugins:
                existing_lo = read_loadorder(loadorder_path)
                existing_lo_lower = {n.lower() for n in existing_lo}
                for name in new_plugins:
                    if name.lower() not in existing_lo_lower:
                        existing_lo.append(name)
                        existing_lo_lower.add(name.lower())
                write_loadorder(loadorder_path, [PluginEntry(name=n, enabled=True) for n in existing_lo])
                log_fn(f"plugins.txt / loadorder.txt: added {len(new_plugins)} plugin(s) from '{mod_name}'.")

            if was_existing_mod:
                ensure_mod_preserving_position(modlist_path, mod_name, enabled=True)
            else:
                prepend_mod(modlist_path, mod_name, enabled=True)

            log_fn(f"Added '{mod_name}' to modlist.")

        meta_path = dest_root / "meta.ini"
        _archive = Path(archive_path)
        _game_domain = getattr(game, "nexus_game_domain", "")
        if prebuilt_meta is not None:
            # Caller already has full metadata — write it directly, no API calls needed.
            try:
                prebuilt_meta.installation_file = os.path.basename(archive_path)
                if _preserved_endorsed:
                    prebuilt_meta.endorsed = True
                write_meta(meta_path, prebuilt_meta)
                log_fn(f"Nexus: Saved metadata for '{mod_name}' "
                       f"(mod {prebuilt_meta.mod_id})")
            except OSError:
                pass
        elif _game_domain and _archive.is_file():
            def _detect_meta():
                try:
                    app = None
                    try:
                        app = parent_window.winfo_toplevel()
                    except Exception:
                        pass
                    api = getattr(app, "_nexus_api", None) if app else None

                    meta = resolve_nexus_meta_for_archive(
                        _archive, _game_domain,
                        api=api,
                        log_fn=lambda m: (
                            app.after(0, lambda msg=m: log_fn(msg))
                            if app else None
                        ),
                    )
                    if meta:
                        write_meta(meta_path, meta)
                        msg = f"Nexus: Saved metadata for '{mod_name}' (mod {meta.mod_id})"
                        if app:
                            app.after(0, lambda: log_fn(msg))
                    # Always also try mod.io (a mod can be on both sites).
                    _try_modio_resolve(
                        game, _archive, meta_path,
                        lambda m: (app.after(0, lambda msg=m: log_fn(msg))
                                   if app else None),
                    )
                except Exception:
                    pass
            threading.Thread(target=_detect_meta, daemon=True).start()

        if not headless and not suppress_notification:
            _show_mod_notification(parent_window, f"Installed: {mod_name}")

        _fire_on_installed(on_installed, is_fomod=is_fomod_install,
                           installed_mod_name=mod_name)

        if mod_panel is not None and not headless and not skip_reload:
            mod_panel.after(0, mod_panel.reload_after_install)

        _maybe_queue_rename_after_install(
            parent_window, mod_panel, headless=headless,
            mod_name=mod_name, suggestions=suggestions,
        )

        return mod_name

    except Exception as e:
        import traceback
        log_fn(f"Install error: {e}")
        log_fn(traceback.format_exc())
        return None
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
        if _tmp_claimed:
            with _tmp_space_lock:
                _tmp_space_reserved = max(0, _tmp_space_reserved - _extract_size_estimate)
