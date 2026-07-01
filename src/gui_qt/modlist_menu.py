"""Right-click context menu for the modlist.

Mirrors the Tk menu (gui/modlist_panel.py `_populate_context_menu`) for all three
target types — normal mods, separators, and the Overwrite folder. Each item is
SHOWN only when its Tk condition holds and HIDDEN otherwise (Tk omits items; it
never disables them). The only greyed items are the handful still awaiting a Qt
backend (Bundle options…, Reinstall Mod, Change separator color, Separator
settings…), and even those appear only when their Tk show-condition passes.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMenu, QInputDialog
from PySide6.QtGui import QAction

from gui_qt.modlist_model import COL_NAME


def show_context_menu(view, global_pos, index):
    """Build + exec the context menu for *index* at *global_pos*."""
    menu = build_context_menu(view, index)
    if menu is not None:
        menu.exec(global_pos)


def build_context_menu(view, index):
    """Construct (but don't exec) the context QMenu for *index* — split out so
    headless tests can inspect the actions. Returns None if there's no menu."""
    model = view.model()
    if not index.isValid():
        return None
    row = index.row()
    entry = model.entry(row)
    # Fresh meta.ini memo per build — the gate helpers re-read the same metas
    # many times per selected mod (see _read_mod_meta).
    view._menu_meta_cache = {}

    # Selected rows (mods + separators tracked separately for the bulk actions).
    sel_rows = sorted({i.row() for i in view.selectionModel().selectedRows()
                       or view.selectionModel().selectedIndexes()})
    sel_mods = [r for r in sel_rows
                if not model.entry(r).is_separator]
    sel_seps = [r for r in sel_rows
                if model.entry(r).is_separator
                and model.entry(r).name not in _boundary_names()]
    multi_mods = len(sel_mods) > 1
    multi_seps = len(sel_seps) > 1

    menu = QMenu(view)
    # Track whether the current group emitted anything, so dividers only appear
    # between non-empty groups (Tk behaviour).
    state = {"group_started": False, "any": False}

    def _connect(action, slot):
        # QAction.triggered emits a `checked` bool. If a slot captures data via a
        # default arg (e.g. `lambda ns=names:`), Qt passes `checked` positionally
        # and clobbers that default. Wrap so the bool is always swallowed.
        action.triggered.connect(lambda _checked=False, _s=slot: _s())

    def act(label, slot, enabled=True):
        a = QAction(label, menu)
        _connect(a, slot)
        a.setEnabled(enabled)
        menu.addAction(a)
        state["group_started"] = True
        state["any"] = True
        return a

    def stub(label):
        # Greyed-out placeholder for an action not yet wired.
        return act(label, lambda: None, enabled=False)

    def submenu(label, items, enabled=True):
        """Add a nested QMenu. *items* is a list of (text, slot) pairs — one
        action each. Used for Copy/Move to profile (the profile list nests as a
        submenu instead of opening a picker window)."""
        sub = QMenu(label, menu)
        sub.setEnabled(enabled)
        for text, slot in items:
            a = QAction(text, sub)
            _connect(a, slot)
            sub.addAction(a)
        menu.addMenu(sub)
        state["group_started"] = True
        state["any"] = True
        return sub

    def divider():
        if state["group_started"]:
            menu.addSeparator()
            state["group_started"] = False

    if entry.is_separator and entry.name in _boundary_names():
        # Overwrite gets a Log item; Root Folder gets nothing.
        from Utils.filemap import OVERWRITE_NAME
        if entry.name == OVERWRITE_NAME and not multi_mods and not multi_seps:
            act("Log", lambda: _show_overwrite_log(view),
                enabled=getattr(view, "game", None) is not None)
            return menu
        return None

    if entry.is_separator:
        _build_separator_menu(view, model, row, entry, sel_seps, multi_seps,
                              act, stub, divider)
    else:
        _build_mod_menu(view, model, row, entry, sel_mods, multi_mods,
                        act, stub, divider, submenu)
    return menu


def _build_separator_menu(view, model, row, entry, sel_seps, multi, act, stub, divider):
    if multi:
        # ≥2 separators selected.
        all_locked = all(model.is_sep_locked(model.entry(r).display_name)
                         for r in sel_seps)
        n = len(sel_seps)
        act(("Unlock Separators" if all_locked else "Lock Separators") + f" ({n})",
            lambda: _set_sep_locks_multi(view, model, sel_seps, not all_locked))
        divider()
        act(f"Remove separators ({n})",
            lambda: _remove_separators_multi(view, model, sel_seps))
        return
    stub("Change separator color")
    locked = model.is_sep_locked(entry.display_name)
    act("Unlock Separator" if locked else "Lock Separator",
        lambda: _toggle_sep_lock(view, model, row))
    divider()
    act("Rename separator", lambda: _rename(view, model, row))
    stub("Separator settings…")
    act("Add separator above", lambda: _add_separator(view, model, row, True))
    act("Add separator below", lambda: _add_separator(view, model, row, False))
    divider()
    act("Remove separator", lambda: _remove_separator(view, model, row))


def _build_mod_menu(view, model, row, entry, sel_mods, multi, act, stub, divider,
                    submenu):
    if multi:
        n = len(sel_mods)
        _names = [model.entry(r).name for r in sel_mods]
        _staging_ok = getattr(view, "staging_dir", None) is not None
        # Group: files — Root Folder toggles gate on the non-empty subset each
        # applies to (Tk root_folder_enable_multi / _disable_multi).
        if _staging_ok:
            _rf_disable = [nm for nm in _names if _is_root_folder(view, nm)]
            _rf_enable = [nm for nm in _names if not _is_root_folder(view, nm)]
            if _rf_disable:
                act(f"Disable Root Folder install ({len(_rf_disable)})",
                    lambda ns=_rf_disable: _toggle_root_folder(view, ns, False))
            if _rf_enable:
                act(f"Enable Root Folder install ({len(_rf_enable)})",
                    lambda ns=_rf_enable: _toggle_root_folder(view, ns, True))
        divider()
        # Group: Nexus — each item shows only when it has valid targets (Tk).
        _endorse_multi = [nm for nm in _names
                          if _has_nexus_id(view, nm) and _is_endorsed(view, nm)]
        _abstain_multi = [nm for nm in _names
                          if _has_nexus_id(view, nm) and not _is_endorsed(view, nm)]
        _check_multi = [nm for nm in _names
                        if _has_nexus_id(view, nm) or bool(_modio_url(view, nm))]
        _nexus_multi = [nm for nm in _names if _has_nexus_page(view, nm)]
        _reqs_multi = [nm for nm in _names if _has_missing_reqs(view, nm)]
        _qu = [nm for nm in _names if _has_update_flag(view, nm)]
        if _abstain_multi:
            act(f"Abstain selected ({len(_abstain_multi)})",
                lambda ns=_abstain_multi: _endorse(view, ns, False))
        if _check_multi:
            act(f"Check Updates ({len(_check_multi)})",
                lambda ns=_check_multi: _check_updates(view, ns))
        if _endorse_multi:
            act(f"Endorse selected ({len(_endorse_multi)})",
                lambda ns=_endorse_multi: _endorse(view, ns, True))
        if _reqs_multi:
            act(f"Missing Requirements ({len(_reqs_multi)})",
                lambda ns=_reqs_multi: _missing_reqs(view, ns))
        if _nexus_multi:
            act(f"Open on Nexus ({len(_nexus_multi)})",
                lambda ns=_nexus_multi: _open_on_nexus_multi(view, ns))
        if _qu:
            act(f"Quick Update ({len(_qu)})",
                lambda ns=_qu: _quick_update(view, ns))
        divider()
        # Group: organise
        _others = _other_profiles(view)
        if _others:
            submenu(f"Copy to profile ({n})",
                    _profile_submenu_items(view, _names, sel_mods, _others, False))
            submenu(f"Move to profile ({n})",
                    _profile_submenu_items(view, _names, sel_mods, _others, True))
        act(f"Disable selected ({n})",
            lambda: _set_enabled(view, model, sel_mods, False))
        act(f"Enable selected ({n})",
            lambda: _set_enabled(view, model, sel_mods, True))
        if _separator_choices(model):
            act(f"Move to separator ({n})",
                lambda: _pick_separator(view, model, sel_mods))
        if len(sel_mods) >= 2:
            act(f"Sort Alphabetically ({n})",
                lambda: _sort_selected_alphabetically(view, model, sel_mods))
        divider()
        # Group: notes
        act(f"Add note ({n})", lambda: _open_note_editor(view, _names))
        _note_remove = [nm for nm in _names if _mod_note(view, nm)]
        if _note_remove:
            act(f"Remove note ({len(_note_remove)})",
                lambda ns=_note_remove: _remove_notes(view, ns))
        divider()
        # Group: remove
        act(f"Remove mod ({n})",
            lambda: _remove_mods_multi(view, model, sel_mods))
        return

    locked = entry.locked
    name = entry.name
    _staging_ok = getattr(view, "staging_dir", None) is not None
    # Group 1: manage
    act("Open folder", lambda: _open_folder(view, model, row))
    # Bundle options… — unwired stub, shown greyed only when a bundle spec exists.
    if _has_bundle_spec(view, name):
        stub("Bundle options…")
    if _staging_ok:
        act("Create empty mod below", lambda: _create_empty_mod(view, model, row))
    # Reinstall Mod — unwired stub, shown greyed only when the install archive
    # is still on disk (Tk: ctx_meta present + _find_installation_archive).
    if _installation_archive(view, name) is not None:
        stub("Reinstall Mod")
    act("Rename mod", lambda: _rename(view, model, row), enabled=not locked)
    divider()
    # Group 2: files & install options
    if _staging_ok:
        _is_rf = _is_root_folder(view, name)
        act("Disable Root Folder install" if _is_rf else "Enable Root Folder install",
            lambda: _toggle_root_folder(view, [name], not _is_rf))
    divider()
    # Group 3: Nexus / online & updates — each item shows only when applicable.
    _endorsed = _is_endorsed(view, name)
    _has_id = _has_nexus_id(view, name)
    if _has_id:
        act("Abstain from Endorsement" if _endorsed else "Endorse Mod",
            lambda: _endorse(view, [name], not _endorsed))
        act("Change Version", lambda: _change_version(view, name))
    if _has_id or bool(_modio_url(view, name)):
        act("Check Updates", lambda: _check_updates(view, [name]))
    if _has_missing_reqs(view, name):
        act("Missing Requirements", lambda: _missing_reqs(view, [name]))
    if _modio_url(view, name):
        act("Open on mod.io", lambda: _open_on_modio(view, name))
    if _has_nexus_page(view, name):
        act("Open on Nexus", lambda: _open_on_nexus(view, name))
    if _has_update_flag(view, name):
        act("Quick Update", lambda: _quick_update(view, [name]))
    divider()
    # Group 4: organise / layout
    act("Add separator above", lambda: _add_separator(view, model, row, True))
    act("Add separator below", lambda: _add_separator(view, model, row, False))
    _others = _other_profiles(view)
    if _others:
        submenu("Copy to profile",
                _profile_submenu_items(view, [name], [row], _others, False))
        submenu("Move to profile",
                _profile_submenu_items(view, [name], [row], _others, True))
    if not locked and _separator_choices(model):
        act("Move to separator", lambda: _pick_separator(view, model, [row]))
    if not locked:
        act("Set priority…", lambda: _set_priority(view, model, row))
    divider()
    # Group 5: info / conflicts / notes
    _has_note = bool(_mod_note(view, name))
    act("Edit note" if _has_note else "Add note",
        lambda: _open_note_editor(view, [name]))
    if _has_conflict(model, row):
        act("Show Conflicts", lambda: _show_conflicts(view, name))
    divider()
    # Group 6: remove
    act("Remove mod", lambda: _remove(view, model, row), enabled=not locked)


def _boundary_names():
    from gui_qt.modlist_model import _BOUNDARY_NAMES
    return _BOUNDARY_NAMES


# ---- action implementations (model-level; backend ops come later) ---------

def _set_enabled(view, model, rows, state):
    # One save + one enabled_changed for the whole selection (toggle() per row
    # would write modlist.txt and re-sync plugins N times).
    model.set_rows_enabled(rows, state)


def _open_folder(view, model, row):
    """Open the mod's staging folder via the platform opener (Utils.xdg)."""
    name = model.entry(row).name
    staging = getattr(view, "staging_dir", None)
    if staging is None:
        return
    path = staging / name
    try:
        from Utils.xdg import xdg_open
        xdg_open(str(path))
    except Exception:
        pass


def _check_updates(view, names):
    """Run a Nexus update check limited to *names* (the window installs the
    callback in _reload_modlist). No-op if it isn't wired (e.g. headless)."""
    cb = getattr(view, "on_check_updates", None)
    if cb is not None and names:
        cb(set(names))


def _change_version(view, name):
    """Open the Change Version picker for *name* (the window installs the
    callback in _reload_modlist). No-op if it isn't wired (e.g. headless)."""
    cb = getattr(view, "on_change_version", None)
    if cb is not None and name:
        cb(name)


def _has_update_flag(view, name: str) -> bool:
    """True if *name* currently carries the pending-update flag (FLAG_UPDATE),
    i.e. Check Updates found a newer file and it isn't ignored. Read straight
    off the model's flag bitmask so it matches what the row paints."""
    try:
        model = view.model()
    except Exception:
        return False
    from gui_qt.modlist_data import FLAG_UPDATE
    bits = model._flags.get(name, 0) if hasattr(model, "_flags") else 0
    return bool(bits & FLAG_UPDATE)


def _has_missing_reqs(view, name: str) -> bool:
    """True if *name* carries the missing-requirements flag (FLAG_MISSING_REQS).
    Read off the model's flag bitmask (same source the row paints), so the menu
    matches Tk's `mod_name in self._missing_reqs`."""
    try:
        model = view.model()
    except Exception:
        return False
    from gui_qt.modlist_data import FLAG_MISSING_REQS
    bits = model._flags.get(name, 0) if hasattr(model, "_flags") else 0
    return bool(bits & FLAG_MISSING_REQS)


def _quick_update(view, names):
    """Auto-install the latest name-matched version for each update-flagged mod
    in *names* (the window installs the callback in _reload_modlist). No-op if
    it isn't wired (e.g. headless)."""
    cb = getattr(view, "on_quick_update", None)
    targets = [n for n in names if _has_update_flag(view, n)]
    if cb is not None and targets:
        cb(targets)


def _missing_reqs(view, names):
    """Open the Missing Requirements panel for *names* (1 = single, N = multi).
    The window installs the callback in _reload_modlist; no-op if unwired."""
    cb = getattr(view, "on_missing_reqs", None)
    if cb is not None and names:
        cb(names[0] if len(names) == 1 else set(names))


def _has_conflict(model, row) -> bool:
    """True if the row has a loose OR BSA conflict (so Show Conflicts is useful)."""
    from gui_qt.modlist_model import COL_CONFLICTS, ConflictRole, BsaConflictRole
    idx = model.index(row, COL_CONFLICTS)
    loose = model.data(idx, ConflictRole) or 0
    bsa = model.data(idx, BsaConflictRole) or 0
    return bool(loose) or bool(bsa)


def _show_conflicts(view, name):
    """Open the Show Conflicts tab for *name* (window installs the callback in
    _reload_modlist). No-op if it isn't wired (e.g. headless)."""
    cb = getattr(view, "on_show_conflicts", None)
    if cb is not None and name:
        cb(name)


def _mod_nexus_url(view, name: str) -> str:
    """The mod's Nexus page URL from its meta.ini ("" if none / no staging)."""
    staging = getattr(view, "staging_dir", None)
    if staging is None:
        return ""
    meta_path = staging / name / "meta.ini"
    if not meta_path.is_file():
        return ""
    try:
        from Nexus.nexus_meta import read_meta
        return read_meta(meta_path).nexus_page_url or ""
    except Exception:
        return ""


def _has_nexus_page(view, name: str) -> bool:
    return bool(_mod_nexus_url(view, name))


def _open_on_nexus(view, name: str):
    url = _mod_nexus_url(view, name)
    if not url:
        return
    try:
        from Utils.xdg import open_url
        open_url(url)
    except Exception:
        pass


def _modio_url(view, name: str) -> str:
    """The mod's stored mod.io profile URL from meta.ini ("" if none). The
    slug-based URL is captured at install/update time (BG3 mod.io mods)."""
    staging = getattr(view, "staging_dir", None)
    if staging is None:
        return ""
    meta_path = staging / name / "meta.ini"
    if not meta_path.is_file():
        return ""
    try:
        import configparser
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(str(meta_path), encoding="utf-8")
        return (cp.get("General", "modioProfileUrl", fallback="") or "").strip()
    except Exception:
        return ""


def _open_on_modio(view, name: str):
    url = _modio_url(view, name)
    if not url:
        return
    try:
        from Utils.xdg import open_url
        open_url(url)
    except Exception:
        pass


# ---- Move to separator -----------------------------------------------------
def _separator_choices(model):
    """(display, internal_name) for every non-boundary separator, in list order."""
    from gui_qt.modlist_model import _BOUNDARY_NAMES
    out = []
    for r in range(model.rowCount()):
        e = model.entry(r)
        if e.is_separator and e.name not in _BOUNDARY_NAMES:
            out.append((e.display_name, e.name))
    return out


def _pick_separator(view, model, mod_rows):
    choices = _separator_choices(model)
    if not choices:
        return
    from gui_qt.list_picker_overlay import ListPickerOverlay
    ListPickerOverlay.show_over(
        view, "Move to separator", choices,
        lambda sep_name: _move_to_separator(view, model, mod_rows, sep_name)
        if sep_name else None,
        select_label="Move")


def _move_to_separator(view, model, mod_rows, sep_name):
    """Reposition the selected mods directly below *sep_name* (lowest-priority end
    of its group in the reverse-priority display, matching Tk). Rebuilds the body
    without the moved mods, then inserts them right after the separator."""
    from gui_qt.modlist_model import _BOUNDARY_NAMES
    rows = sorted(r for r in mod_rows
                  if not model.entry(r).is_separator
                  and model.entry(r).name not in _BOUNDARY_NAMES)
    if not rows:
        return
    moved_names = {model.entry(r).name for r in rows}
    moved = [model.entry(r) for r in rows]           # preserve selection order
    # Body = all non-boundary entries, minus the ones we're moving.
    body = [model.entry(r) for r in range(model.rowCount())
            if model.entry(r).name not in _BOUNDARY_NAMES
            and model.entry(r).name not in moved_names]
    sep_idx = next((i for i, e in enumerate(body)
                    if e.is_separator and e.name == sep_name), None)
    if sep_idx is None:
        return
    body[sep_idx + 1:sep_idx + 1] = moved
    model.set_entries(body)
    try:
        model.save()
    except Exception:
        pass


# ---- Copy / Move to profile ------------------------------------------------
def _other_profiles(view):
    """Profile names for this game, excluding the current one ([] if none)."""
    game = getattr(view, "game", None)
    pdir = getattr(view, "profile_dir", None)
    if game is None or pdir is None:
        return []
    try:
        from gui.game_helpers import _profiles_for_game
        cur = pdir.name
        return [p for p in _profiles_for_game(game.name) if p != cur]
    except Exception:
        return []


def _profile_submenu_items(view, names, mod_rows, others, move: bool):
    """Build the (profile_name, slot) list for the Copy/Move-to-profile submenu.
    Each entry copies/moves *names* to that profile (Tk lists the profiles as a
    submenu rather than opening a picker window)."""
    model = view.model()
    enabled_map = {}
    for r in mod_rows:
        e = model.entry(r)
        if not e.is_separator:
            enabled_map[e.name] = e.enabled
    return [
        (prof, (lambda p=prof: _copy_to_profile(
            view, names, dict(enabled_map), p, move)))
        for prof in others
    ]


def _copy_to_profile(view, names, enabled_map, target_profile, move):
    """Delegate the copy/move to the window (needs game, worker thread, collision
    overlay, and — for move — remove_mods + reload)."""
    cb = getattr(view, "on_copy_to_profile", None)
    if cb is not None and names and target_profile:
        cb(list(names), dict(enabled_map), target_profile, move)


def _read_mod_meta(view, name):
    """Read a mod's meta.ini (or None). Central so the menu helpers agree.
    Memoised per menu build (the gate helpers each want the same meta 5-7
    times per selected mod; build_context_menu resets the memo)."""
    cache = getattr(view, "_menu_meta_cache", None)
    if cache is not None and name in cache:
        return cache[name]
    meta = None
    staging = getattr(view, "staging_dir", None)
    if staging is not None:
        meta_path = staging / name / "meta.ini"
        if meta_path.is_file():
            try:
                from Nexus.nexus_meta import read_meta
                meta = read_meta(meta_path)
            except Exception:
                meta = None
    if cache is not None:
        cache[name] = meta
    return meta


def _has_bundle_spec(view, name: str) -> bool:
    """True if the mod carries an RE/Fluffy bundle spec (Tk `_bundle_spec_path`).
    Read off the model's FLAG_BUNDLE bit (computed by read_meta_for_entries from
    the same meta.ini) instead of re-parsing the spec on every right-click."""
    try:
        model = view.model()
    except Exception:
        return False
    from gui_qt.modlist_data import FLAG_BUNDLE
    bits = model._flags.get(name, 0) if hasattr(model, "_flags") else 0
    return bool(bits & FLAG_BUNDLE)


def _installation_archive(view, name: str):
    """Path to the mod's original install archive if it still exists, else None
    (Tk `_find_installation_archive`). Gates the (still-unwired) Reinstall Mod
    item. Searches the user's Downloads dir + the game's configured caches +
    any extra download locations, matching the Tk lookup."""
    meta = _read_mod_meta(view, name)
    filename = getattr(meta, "installation_file", "") if meta is not None else ""
    if not filename:
        return None
    import os
    from pathlib import Path
    game = getattr(view, "game", None)
    game_name = getattr(game, "name", "") or ""
    search_dirs = []
    try:
        from Utils.config_paths import list_all_cache_dirs
        from Utils.download_locations import (
            is_default_downloads_disabled, load_extra_download_locations)
        if not is_default_downloads_disabled():
            xdg = os.environ.get("XDG_DOWNLOAD_DIR")
            search_dirs.append(Path(xdg) if xdg else Path.home() / "Downloads")
        search_dirs.extend(list_all_cache_dirs(game_name))
        search_dirs.extend(Path(p) for p in load_extra_download_locations())
    except Exception:
        return None
    for d in search_dirs:
        cand = Path(d) / filename
        if cand.is_file():
            return cand
    return None


# ---- Root Folder install toggle -------------------------------------------
def _is_root_folder(view, name) -> bool:
    m = _read_mod_meta(view, name)
    return bool(getattr(m, "root_folder", False)) if m is not None else False


def _toggle_root_folder(view, names, enable: bool):
    """Set rootFolder=enable in each mod's meta.ini (skips ones already there),
    then ask the window to rescan + rebuild the filemap (the index caches
    strip-applied vs verbatim paths). Port of Tk _set_root_folder_flag_multi."""
    staging = getattr(view, "staging_dir", None)
    if staging is None:
        return
    from Nexus.nexus_meta import read_meta, write_meta, NexusModMeta
    changed = []
    for nm in names:
        meta_path = staging / nm / "meta.ini"
        try:
            meta = read_meta(meta_path) if meta_path.is_file() else NexusModMeta()
            if bool(meta.root_folder) == enable:
                continue
            meta.root_folder = enable
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            write_meta(meta_path, meta)
            changed.append(nm)
        except Exception:
            continue
    if changed:
        cb = getattr(view, "on_root_folder_changed", None)
        if cb is not None:
            cb(changed)


# ---- Endorse / Abstain -----------------------------------------------------
def _is_endorsed(view, name) -> bool:
    m = _read_mod_meta(view, name)
    return bool(getattr(m, "endorsed", False)) if m is not None else False


def _has_nexus_id(view, name) -> bool:
    m = _read_mod_meta(view, name)
    return bool(getattr(m, "mod_id", 0)) if m is not None else False


def _endorse(view, names, endorse: bool):
    """Endorse/abstain the mods — delegated to the window (needs the shared
    Nexus API + a worker thread; see app._on_modlist_endorse)."""
    cb = getattr(view, "on_endorse", None)
    if cb is not None and names:
        cb(list(names), endorse)


# ---- Notes -----------------------------------------------------------------
def _profile_notes(view):
    """(profile_dir, {name: note}) for the active profile, or (None, {})."""
    pdir = getattr(view, "profile_dir", None)
    if pdir is None:
        return None, {}
    try:
        from Utils.profile_state import read_mod_notes
        return pdir, read_mod_notes(pdir)
    except Exception:
        return pdir, {}


def _mod_note(view, name) -> str:
    _pdir, notes = _profile_notes(view)
    return notes.get(name, "")


def _open_note_editor(view, names):
    """Open the note editor for one mod (existing text) or many (append on
    save). Port of Tk _open_note_editor_by_name / _for_multi."""
    pdir, notes = _profile_notes(view)
    if pdir is None or not names:
        return
    from Utils.profile_state import write_mod_notes
    single = len(names) == 1
    title = names[0] if single else f"{len(names)} mods"
    initial = notes.get(names[0], "") if single else ""

    def _save(text: str):
        text = (text or "").strip()
        cur = dict(notes)
        if single:
            if text:
                cur[names[0]] = text
            else:
                cur.pop(names[0], None)
        else:
            if not text:
                return
            for nm in names:
                existing = cur.get(nm, "").rstrip()
                cur[nm] = f"{existing}\n{text}" if existing else text
        try:
            write_mod_notes(pdir, cur)
        except Exception:
            pass
        cb = getattr(view, "on_notes_changed", None)
        if cb is not None:
            cb(list(names))

    def _remove():
        cur = dict(notes)
        for nm in names:
            cur.pop(nm, None)
        try:
            write_mod_notes(pdir, cur)
        except Exception:
            pass
        cb = getattr(view, "on_notes_changed", None)
        if cb is not None:
            cb(list(names))

    from gui_qt.note_editor_overlay import NoteEditorOverlay
    NoteEditorOverlay.show_over(view, title, initial, _save, _remove,
                                allow_remove=any(notes.get(nm) for nm in names))


def _remove_notes(view, names):
    """Remove the note from each mod without opening the editor."""
    pdir, notes = _profile_notes(view)
    if pdir is None or not names:
        return
    from Utils.profile_state import write_mod_notes
    cur = dict(notes)
    removed = False
    for nm in names:
        if cur.pop(nm, None) is not None:
            removed = True
    if removed:
        try:
            write_mod_notes(pdir, cur)
        except Exception:
            pass
        cb = getattr(view, "on_notes_changed", None)
        if cb is not None:
            cb(list(names))


def _open_on_nexus_multi(view, names):
    """Open each selected mod's Nexus page (skips mods without one)."""
    try:
        from Utils.xdg import open_url
    except Exception:
        return
    for nm in names:
        url = _mod_nexus_url(view, nm)
        if url:
            try:
                open_url(url)
            except Exception:
                pass


def _sort_selected_alphabetically(view, model, mod_rows):
    """Sort the SELECTED mods A→Z, writing them back into the same row slots the
    selection occupied (other rows + separators stay put). Port of Tk
    _sort_selected_alphabetically."""
    from gui_qt.modlist_model import _BOUNDARY_NAMES
    rows = sorted(r for r in mod_rows
                  if not model.entry(r).is_separator
                  and model.entry(r).name not in _BOUNDARY_NAMES)
    if len(rows) < 2:
        return
    # Sorted copy of just the selected entries.
    sorted_entries = sorted((model.entry(r) for r in rows),
                            key=lambda e: e.display_name.casefold())
    # Rebuild the full body (non-boundary rows in current order); at each selected
    # slot drop in the next sorted entry. set_entries re-appends boundaries.
    body: list = []
    it = iter(sorted_entries)
    row_set = set(rows)
    for r in range(model.rowCount()):
        e = model.entry(r)
        if e.name in _BOUNDARY_NAMES:
            continue
        body.append(next(it) if r in row_set else e)
    model.set_entries(body)
    try:
        model.save()
    except Exception:
        pass


def _create_empty_mod(view, model, row):
    """Prompt for a name, create an empty staging folder + minimal meta.ini, and
    insert a new mod row just below *row*. Port of Tk _create_empty_mod."""
    from PySide6.QtWidgets import QMessageBox
    staging = getattr(view, "staging_dir", None)
    if staging is None:
        return
    name, ok = QInputDialog.getText(view, "Create empty mod", "Mod name:")
    if not ok:
        return
    name = name.strip()
    if not name:
        return
    # Name-collision guard (mods + separators, by display name).
    existing = set()
    for r in range(model.rowCount()):
        e = model.entry(r)
        existing.add(e.name)
        existing.add(e.display_name)
    if name in existing:
        QMessageBox.warning(view, "Name conflict",
                            f"A mod or separator named '{name}' already exists.")
        return
    try:
        from datetime import datetime
        mod_dir = staging / name
        mod_dir.mkdir(parents=True, exist_ok=True)
        installed = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        (mod_dir / "meta.ini").write_text(
            f"[General]\ninstalled={installed}\n", encoding="utf-8")
    except OSError as exc:
        QMessageBox.warning(view, "Create empty mod",
                            f"Could not create the mod folder:\n{exc}")
        return
    model.insert_mod(row, name, above=False)


def _show_overwrite_log(view):
    """Show the read-only Overwrite-log overlay (files swept into overwrite/ per
    restore). Path = game.get_effective_overwrite_path()/OVERWRITE_LOG_NAME."""
    game = getattr(view, "game", None)
    if game is None:
        return
    text = ""
    try:
        from Utils.deploy_shared import OVERWRITE_LOG_NAME
        log_path = game.get_effective_overwrite_path() / OVERWRITE_LOG_NAME
        if log_path.is_file():
            text = log_path.read_text(encoding="utf-8")
    except Exception:
        text = ""
    from gui_qt.overwrite_log_overlay import OverwriteLogOverlay, parse_overwrite_log
    OverwriteLogOverlay.show_over(view, parse_overwrite_log(text))


def _toggle_collapse(view, model, row):
    view._toggle_collapse_row(row)


def _toggle_sep_lock(view, model, row):
    view._toggle_lock_row(row)


def _rename(view, model, row):
    e = model.entry(row)
    new, ok = QInputDialog.getText(view, "Rename", "New name:",
                                   text=e.display_name)
    if not ok or not new.strip() or new.strip() == e.display_name:
        return
    if e.is_separator:
        # No folder on disk — a pure modlist.txt edit is the whole rename.
        model.rename(row, new.strip())
        return
    # Mods must go through the window: staging folder rename + modindex +
    # per-mod state migration (strip prefixes / disabled plugins / excluded
    # files / notes), not just the modlist.txt line.
    cb = getattr(view, "on_rename_mod", None)
    if callable(cb):
        cb(e.name, new.strip())


def _set_priority(view, model, row):
    cur = model.data(model.index(row, COL_NAME), 0)
    val, ok = QInputDialog.getInt(view, "Set priority",
                                  f"Priority for {cur}:", 0, 0, 99999)
    if ok:
        model.set_priority(row, val)


def _add_separator(view, model, row, above):
    name, ok = QInputDialog.getText(view, "Add separator", "Separator name:")
    if ok and name.strip():
        model.add_separator(row, name.strip(), above)


def _remove(view, model, row):
    """Fully remove a mod: undeploy its files, delete its staging folder, drop
    its index/BSA/plugins entries, then remove the modlist row. (Not just the
    list line — that left the files on disk so the mod still read as installed.)"""
    from PySide6.QtWidgets import QMessageBox
    e = model.entry(row)
    if e is None or e.is_separator:
        return
    if QMessageBox.question(
            view, "Remove mod",
            f"Remove '{e.display_name}'?\n\nThis deletes the mod folder and "
            "cannot be undone.") != QMessageBox.Yes:
        return
    name = e.name
    game = getattr(view, "game", None)
    profile_dir = getattr(view, "profile_dir", None)
    if game is not None and profile_dir is not None:
        try:
            from Utils.mod_remove import remove_mods
            remove_mods(game, profile_dir, [name],
                        log_fn=lambda m: print(f"[remove] {m}", flush=True))
        except Exception as exc:
            print(f"[gui_qt] mod removal failed: {exc}", flush=True)
    model.remove_row(row)


# ---- new wired handlers (separator remove / multi, mod multi-remove) -------

def _remove_separator(view, model, row):
    from PySide6.QtWidgets import QMessageBox
    e = model.entry(row)
    if e is None or not e.is_separator:
        return
    if QMessageBox.question(
            view, "Remove separator",
            f"Remove separator '{e.display_name}'?") != QMessageBox.Yes:
        return
    model.remove_row(row)


def _remove_separators_multi(view, model, sep_rows):
    from PySide6.QtWidgets import QMessageBox
    if not sep_rows:
        return
    if QMessageBox.question(
            view, "Remove separators",
            f"Remove {len(sep_rows)} separator(s)?") != QMessageBox.Yes:
        return
    # Remove high→low so earlier removals don't shift later row indices.
    for r in sorted(sep_rows, reverse=True):
        e = model.entry(r)
        if e is not None and e.is_separator:
            model.remove_row(r)


def _set_sep_locks_multi(view, model, sep_rows, lock):
    """Lock/unlock every selected separator to *lock*, then save once."""
    changed = False
    for r in sep_rows:
        e = model.entry(r)
        if e is None or not e.is_separator:
            continue
        if model.is_sep_locked(e.display_name) != lock:
            model.toggle_sep_lock(r)
            changed = True
    if changed:
        view._save_separator_state()
        view.viewport().update()


def _remove_mods_multi(view, model, mod_rows):
    """Fully remove every selected mod (one confirm), then drop the rows."""
    from PySide6.QtWidgets import QMessageBox
    rows = [r for r in mod_rows
            if (e := model.entry(r)) is not None
            and not e.is_separator and not e.locked]
    if not rows:
        return
    names = [model.entry(r).name for r in rows]
    if QMessageBox.question(
            view, "Remove mods",
            f"Remove {len(names)} mod(s)?\n\nThis deletes their folders and "
            "cannot be undone.") != QMessageBox.Yes:
        return
    game = getattr(view, "game", None)
    profile_dir = getattr(view, "profile_dir", None)
    if game is not None and profile_dir is not None:
        try:
            from Utils.mod_remove import remove_mods
            remove_mods(game, profile_dir, names,
                        log_fn=lambda m: print(f"[remove] {m}", flush=True))
        except Exception as exc:
            print(f"[gui_qt] mod removal failed: {exc}", flush=True)
    for r in sorted(rows, reverse=True):
        model.remove_row(r)
