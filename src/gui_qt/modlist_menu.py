"""Right-click context menu for the modlist.

Surfaces the full Tk menu (gui/modlist_panel.py `_populate_context_menu`) for all
three target types — normal mods, separators, and the Overwrite folder — so they
can be wired one at a time. Items with existing model/backend support are WIRED;
the rest are greyed-out (disabled) STUBS, matching the Tk labels/order/grouping.
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

    def act(label, slot, enabled=True):
        a = QAction(label, menu)
        a.triggered.connect(slot)
        a.setEnabled(enabled)
        menu.addAction(a)
        state["group_started"] = True
        state["any"] = True
        return a

    def stub(label):
        # Greyed-out placeholder for an action not yet wired.
        return act(label, lambda: None, enabled=False)

    def divider():
        if state["group_started"]:
            menu.addSeparator()
            state["group_started"] = False

    if entry.is_separator and entry.name in _boundary_names():
        # Overwrite gets a (stubbed) Log item; Root Folder gets nothing.
        from Utils.filemap import OVERWRITE_NAME
        if entry.name == OVERWRITE_NAME and not multi_mods and not multi_seps:
            stub("Log")
            return menu
        return None

    if entry.is_separator:
        _build_separator_menu(view, model, row, entry, sel_seps, multi_seps,
                              act, stub, divider)
    else:
        _build_mod_menu(view, model, row, entry, sel_mods, multi_mods,
                        act, stub, divider)
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
    collapsed = model.is_collapsed(entry.display_name)
    act("Expand" if collapsed else "Collapse",
        lambda: _toggle_collapse(view, model, row))
    divider()
    act("Rename separator", lambda: _rename(view, model, row))
    stub("Separator settings…")
    act("Add separator above", lambda: _add_separator(view, model, row, True))
    act("Add separator below", lambda: _add_separator(view, model, row, False))
    divider()
    act("Remove separator", lambda: _remove_separator(view, model, row))


def _build_mod_menu(view, model, row, entry, sel_mods, multi, act, stub, divider):
    if multi:
        n = len(sel_mods)
        # Group: files (stub)
        stub(f"Disable Root Folder install ({n})")
        stub(f"Enable Root Folder install ({n})")
        divider()
        # Group: Nexus (stub)
        stub(f"Abstain selected ({n})")
        act(f"Check Updates ({n})",
            lambda: _check_updates(view, [model.entry(r).name for r in sel_mods]))
        stub(f"Endorse selected ({n})")
        act(f"Missing Requirements ({n})",
            lambda: _missing_reqs(view, [model.entry(r).name for r in sel_mods]))
        stub(f"Open on Nexus ({n})")
        stub(f"Quick Update ({n})")
        divider()
        # Group: organise
        stub(f"Copy to profile ({n})")
        stub(f"Move to profile ({n})")
        act(f"Disable selected ({n})",
            lambda: _set_enabled(view, model, sel_mods, False))
        act(f"Enable selected ({n})",
            lambda: _set_enabled(view, model, sel_mods, True))
        stub(f"Move to separator ({n})")
        stub(f"Sort Alphabetically ({n})")
        divider()
        # Group: notes (stub)
        stub(f"Add note ({n})")
        stub(f"Remove note ({n})")
        divider()
        # Group: remove
        act(f"Remove mod ({n})",
            lambda: _remove_mods_multi(view, model, sel_mods))
        return

    locked = entry.locked
    # Group 1: manage
    act("Open folder", lambda: _open_folder(view, model, row))
    stub("Bundle options…")
    stub("Create empty mod below")
    stub("Reinstall Mod")
    act("Rename mod", lambda: _rename(view, model, row), enabled=not locked)
    divider()
    # Group 2: files & install options
    stub("Disable Plugins…")
    stub("INI files")
    stub("Enable Root Folder install")
    divider()
    # Group 3: Nexus / online & updates
    stub("Abstain from Endorsement")
    act("Change Version", lambda: _change_version(view, entry.name))
    act("Check Updates", lambda: _check_updates(view, [entry.name]))
    stub("Endorse Mod")
    act("Missing Requirements", lambda: _missing_reqs(view, [entry.name]))
    stub("Open on mod.io")
    act("Open on Nexus", lambda: _open_on_nexus(view, entry.name),
        enabled=_has_nexus_page(view, entry.name))
    stub("Quick Update")
    divider()
    # Group 4: organise / layout
    act("Add separator above", lambda: _add_separator(view, model, row, True))
    act("Add separator below", lambda: _add_separator(view, model, row, False))
    stub("Copy to profile")
    stub("Move to profile")
    stub("Move to separator")
    act("Set priority…", lambda: _set_priority(view, model, row))
    divider()
    # Group 5: info / conflicts / notes
    stub("Add note")
    stub("Show Conflicts")
    divider()
    # Group 6: remove
    act("Remove mod", lambda: _remove(view, model, row), enabled=not locked)


def _boundary_names():
    from gui_qt.modlist_model import _BOUNDARY_NAMES
    return _BOUNDARY_NAMES


# ---- action implementations (model-level; backend ops come later) ---------

def _set_enabled(view, model, rows, state):
    for r in rows:
        e = model.entry(r)
        if not e.is_separator and not e.locked and e.enabled != state:
            model.toggle(r)


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


def _missing_reqs(view, names):
    """Open the Missing Requirements panel for *names* (1 = single, N = multi).
    The window installs the callback in _reload_modlist; no-op if unwired."""
    cb = getattr(view, "on_missing_reqs", None)
    if cb is not None and names:
        cb(names[0] if len(names) == 1 else set(names))


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


def _toggle_collapse(view, model, row):
    view._toggle_collapse_row(row)


def _toggle_sep_lock(view, model, row):
    view._toggle_lock_row(row)


def _rename(view, model, row):
    e = model.entry(row)
    new, ok = QInputDialog.getText(view, "Rename", "New name:",
                                   text=e.display_name)
    if ok and new.strip():
        model.rename(row, new.strip())


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
