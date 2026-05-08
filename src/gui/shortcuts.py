"""
Global keyboard shortcuts for the Mod Manager main window.

Bindings:
    F2              Rename the selected mod or separator (modlist panel)
    F5              Refresh the modlist (with notification)
    Delete          Remove selected mod(s) (modlist panel)
    Return          Toggle enable/disable for selected mods (modlist panel)
    Home            Scroll active list panel to the top
    End             Scroll active list panel to the bottom
    Ctrl+F          Focus the search bar of the active list panel
    Ctrl+A          Select all mods in the active separator (modlist panel)
                    or all plugins in the plugin panel
    Ctrl+D          Deploy
    Ctrl+R          Restore
    Alt+Up          Move selected mods/plugins/separators up
    Alt+Down        Move selected mods/plugins/separators down
    Shift+E         Expand/collapse all separators
    Shift+F         Toggle filter panel for the active list panel
    Shift+Scroll    4x scroll speed

Reorder shortcuts require the Alt modifier so plain Up/Down can still be
used for normal navigation/scrolling without accidentally shuffling the
selection. Alt+arrow matches the "move line" convention from VS Code /
JetBrains.

Alt+Up/Down and F2 dispatch to whichever panel (modlist or plugin) was
most recently interacted with via mouse. Shortcuts are suppressed while a
text input widget (Entry/Text/etc.) has focus so typing isn't hijacked.
"""

import tkinter as tk


_TEXT_WIDGET_CLASSES = {
    "Entry", "TEntry", "Text", "TCombobox", "Spinbox", "TSpinbox",
    "CTkEntry",
}


def _focus_is_text_input(app) -> bool:
    try:
        w = app.focus_get()
    except Exception:
        return False
    if w is None:
        return False
    try:
        return w.winfo_class() in _TEXT_WIDGET_CLASSES
    except Exception:
        return False


def _focus_is_in_dialog(app) -> bool:
    """True when focus is inside a Toplevel other than the main app window.

    Used to suppress bind_all shortcuts (Return, Delete, etc.) while a modal
    dialog is open, so pressing Enter to confirm a dialog doesn't also trigger
    the modlist toggle behind it.
    """
    try:
        w = app.focus_get()
    except Exception:
        return False
    if w is None:
        return False
    try:
        top = w.winfo_toplevel()
    except Exception:
        return False
    return top is not app


def _active_list_panel(app):
    """Return ("mod", panel) or ("plugin", panel) based on last-interacted panel.

    Falls back to the modlist panel if neither has been touched yet.
    """
    which = getattr(app, "_last_list_panel", "mod")
    if which == "plugin":
        panel = getattr(app, "_plugin_panel", None)
        if panel is not None:
            return "plugin", panel
    panel = getattr(app, "_mod_panel", None)
    if panel is not None:
        return "mod", panel
    return None, None


def _rename_selected(app):
    kind, panel = _active_list_panel(app)
    if kind != "mod" or panel is None:
        return
    sel = sorted(panel._sel_set) if panel._sel_set else (
        [panel._sel_idx] if panel._sel_idx >= 0 else []
    )
    for idx in sel:
        if not (0 <= idx < len(panel._entries)):
            continue
        entry = panel._entries[idx]
        if entry.is_separator:
            panel._rename_separator(idx)
            return
        else:
            panel._rename_mod(idx)
            return


def _deploy(app):
    topbar = getattr(app, "_topbar", None)
    if topbar is not None and hasattr(topbar, "_on_deploy"):
        topbar._on_deploy()


def _restore(app):
    topbar = getattr(app, "_topbar", None)
    if topbar is not None and hasattr(topbar, "_on_restore"):
        topbar._on_restore()


def _move_up(app):
    kind, panel = _active_list_panel(app)
    if panel is None:
        return
    if kind == "mod":
        panel._move_up()
    else:
        panel._move_plugins_up()


def _move_down(app):
    kind, panel = _active_list_panel(app)
    if panel is None:
        return
    if kind == "mod":
        panel._move_down()
    else:
        panel._move_plugins_down()


def _delete_selected(app):
    kind, panel = _active_list_panel(app)
    if kind != "mod" or panel is None:
        return
    if getattr(panel, "_modlist_path", None) is None:
        return
    sel = sorted(panel._sel_set) if panel._sel_set else (
        [panel._sel_idx] if panel._sel_idx >= 0 else []
    )
    # Filter out separators and locked mods
    removable = []
    for idx in sel:
        if not (0 <= idx < len(panel._entries)):
            continue
        entry = panel._entries[idx]
        if entry.is_separator:
            continue
        if getattr(entry, "locked", False):
            continue
        removable.append(idx)
    if not removable:
        return
    if len(removable) == 1:
        panel._remove_mod(removable[0])
    else:
        panel._remove_selected_mods(removable)


def _scroll_list(app, fraction: float):
    kind, panel = _active_list_panel(app)
    if panel is None:
        return
    if kind == "mod":
        canvas = getattr(panel, "_canvas", None)
        redraw = getattr(panel, "_schedule_redraw", None) or getattr(panel, "_redraw", None)
    else:
        canvas = getattr(panel, "_pcanvas", None)
        redraw = getattr(panel, "_schedule_predraw", None)
    if canvas is None:
        return
    canvas.yview_moveto(fraction)
    if redraw is not None:
        redraw()


def _scroll_to_top(app):
    _scroll_list(app, 0.0)


def _scroll_to_bottom(app):
    _scroll_list(app, 1.0)


def _toggle_all_seps(app):
    kind, panel = _active_list_panel(app)
    if kind != "mod" or panel is None:
        return
    panel._toggle_all_separators()


def _toggle_filters(app):
    kind, panel = _active_list_panel(app)
    if panel is None:
        return
    if kind == "mod":
        panel._on_open_filters()
    else:
        panel._toggle_plugin_filter_panel()


def _focus_search(app):
    """Focus the search bar of the active list panel.

    When the plugin panel was last interacted with and its Plugins tab is
    active, focus the plugin search. Otherwise fall back to the modlist
    search bar.
    """
    kind, panel = _active_list_panel(app)
    entry = None
    if kind == "plugin" and panel is not None:
        try:
            current_tab = panel._tabs.get() if hasattr(panel, "_tabs") else ""
        except Exception:
            current_tab = ""
        if current_tab == "Plugins":
            entry = getattr(panel, "_plugin_search_entry", None)
        elif current_tab == "Ini Files":
            entry = getattr(panel, "_ini_search_entry", None)
    if entry is None:
        mod_panel = getattr(app, "_mod_panel", None)
        if mod_panel is not None:
            entry = getattr(mod_panel, "_search_entry", None)
    if entry is None:
        return
    try:
        entry.focus_set()
        entry.select_range(0, "end")
        entry.icursor("end")
    except Exception:
        pass


def _select_all_plugins(panel):
    entries = getattr(panel, "_plugin_entries", None)
    if not entries:
        return
    filtered = getattr(panel, "_plugin_filtered_indices", None)
    if filtered is not None:
        new_sel = set(filtered)
    else:
        new_sel = set(range(len(entries)))
    if not new_sel:
        return
    panel._psel_set = new_sel
    panel._sel_idx = min(new_sel)
    if hasattr(panel, "_predraw"):
        panel._predraw()


def _select_all_in_separator(app):
    """Select all mods under the active separator.

    - If a mod is selected, select every mod in the same separator.
    - If a separator is selected, select every mod in that separator.
    - If there are no separators (other than the synthetic Overwrite /
      Root_Folder rows), select every normal mod.
    - If the plugin panel was last interacted with, select every plugin
      (or every plugin matching the active filter).
    """
    kind, panel = _active_list_panel(app)
    if kind == "plugin" and panel is not None:
        _select_all_plugins(panel)
        return
    if kind != "mod" or panel is None:
        return
    entries = getattr(panel, "_entries", None)
    if not entries:
        return

    # Locate the separator row containing the current selection.
    anchor_idx = -1
    sel_set = getattr(panel, "_sel_set", set())
    sel_idx = getattr(panel, "_sel_idx", -1)
    if sel_set:
        anchor_idx = min(sel_set)
    elif sel_idx >= 0:
        anchor_idx = sel_idx

    sep_idx = -1
    if anchor_idx >= 0 and anchor_idx < len(entries):
        e = entries[anchor_idx]
        if e.is_separator:
            sep_idx = anchor_idx
        else:
            for i in range(anchor_idx - 1, -1, -1):
                if entries[i].is_separator:
                    sep_idx = i
                    break

    if sep_idx >= 0:
        start = sep_idx + 1
    else:
        start = 0
    end = len(entries)
    for i in range(start, len(entries)):
        if entries[i].is_separator:
            end = i
            break

    visible_indices = getattr(panel, "_visible_indices", None)
    if visible_indices:
        visible_set = set(visible_indices)
        new_sel = {
            i for i in range(start, end)
            if not entries[i].is_separator and i in visible_set
        }
    else:
        new_sel = {i for i in range(start, end) if not entries[i].is_separator}
    if not new_sel:
        return
    panel._sel_set = new_sel
    panel._sel_idx = min(new_sel)
    if hasattr(panel, "_redraw"):
        panel._redraw()


def _refresh_modlist(app):
    mod_panel = getattr(app, "_mod_panel", None)
    if mod_panel is None or not hasattr(mod_panel, "_reload"):
        return
    mod_panel._reload()
    try:
        from gui.install_mod import _show_mod_notification
        _show_mod_notification(app, "Modlist Refreshed", state="info")
    except Exception:
        pass


def _toggle_selected(app):
    """Toggle enable/disable on all selected non-separator mods.

    Flips all mods in one batch, then performs the expensive save / filemap
    rebuild / redraw steps once (mirrors the bulk separator-toggle path).
    """
    kind, panel = _active_list_panel(app)
    if kind != "mod" or panel is None:
        return
    if getattr(panel, "_modlist_path", None) is None:
        return
    entries = getattr(panel, "_entries", None)
    check_vars = getattr(panel, "_check_vars", None)
    if not entries or not check_vars:
        return
    sel = sorted(panel._sel_set) if panel._sel_set else (
        [panel._sel_idx] if panel._sel_idx >= 0 else []
    )
    toggle_indices: list[int] = []
    for idx in sel:
        if not (0 <= idx < len(entries)):
            continue
        entry = entries[idx]
        if entry.is_separator:
            continue
        if getattr(entry, "locked", False):
            continue
        if idx >= len(check_vars) or check_vars[idx] is None:
            continue
        toggle_indices.append(idx)
    if not toggle_indices:
        return

    pool_data_idx = getattr(panel, "_pool_data_idx", [])
    pool_check_vars = getattr(panel, "_pool_check_vars", [])
    for idx in toggle_indices:
        var = check_vars[idx]
        new_state = not var.get()
        var.set(new_state)
        entries[idx].enabled = new_state
        for s, di in enumerate(pool_data_idx):
            if di == idx and s < len(pool_check_vars):
                pool_check_vars[s].set(new_state)
                break
        panel._sync_plugins_for_toggle(entries[idx].name, new_state)

    panel._vis_dirty = True
    panel._save_modlist()
    panel._rebuild_filemap()
    panel._scan_missing_reqs_flags()
    if hasattr(panel, "_update_enable_disable_all_btn"):
        panel._update_enable_disable_all_btn()
    panel._redraw()
    panel._update_info()


def register_shortcuts(app) -> None:
    """Install the global keyboard shortcuts on the main App window."""
    app._last_list_panel = "mod"

    def _guard(fn):
        def _handler(event=None):
            if _focus_is_text_input(app):
                return
            if _focus_is_in_dialog(app):
                return
            fn(app)
            return "break"
        return _handler

    def _unguarded(fn):
        """Variant of _guard that fires even when a text input has focus."""
        def _handler(event=None):
            fn(app)
            return "break"
        return _handler

    app.bind_all("<F2>",           _guard(_rename_selected), add="+")
    app.bind_all("<F5>",           _unguarded(_refresh_modlist), add="+")
    app.bind_all("<Control-d>",    _guard(_deploy),          add="+")
    app.bind_all("<Control-D>",    _guard(_deploy),          add="+")
    app.bind_all("<Control-r>",    _guard(_restore),         add="+")
    app.bind_all("<Control-R>",    _guard(_restore),         add="+")
    app.bind_all("<Control-f>",    _unguarded(_focus_search), add="+")
    app.bind_all("<Control-F>",    _unguarded(_focus_search), add="+")
    app.bind_all("<Control-a>",    _guard(_select_all_in_separator), add="+")
    app.bind_all("<Control-A>",    _guard(_select_all_in_separator), add="+")
    app.bind_all("<Alt-Up>",       _guard(_move_up),         add="+")
    app.bind_all("<Alt-Down>",     _guard(_move_down),       add="+")
    app.bind_all("<Delete>",       _guard(_delete_selected), add="+")
    app.bind_all("<Return>",       _guard(_toggle_selected), add="+")
    app.bind_all("<KP_Enter>",     _guard(_toggle_selected), add="+")
    app.bind_all("<Home>",         _guard(_scroll_to_top),    add="+")
    app.bind_all("<End>",          _guard(_scroll_to_bottom), add="+")
    app.bind_all("<Shift-E>",      _guard(_toggle_all_seps),  add="+")
    app.bind_all("<Shift-F>",      _guard(_toggle_filters),   add="+")

    # Shift+mousewheel = 4x scroll speed
    # Generate 3 extra scroll events so total = 4x normal speed.
    def _fast_scroll(event):
        w = event.widget
        num = getattr(event, "num", None)
        delta = getattr(event, "delta", 0) or 0
        up = num == 4 or delta > 0
        if float(tk.TkVersion) >= 8.7:
            d = 120 if up else -120
            for _ in range(3):
                w.event_generate("<MouseWheel>", delta=d)
        else:
            btn = 4 if up else 5
            for _ in range(3):
                w.event_generate(f"<Button-{btn}>")

    app.bind_all("<Shift-MouseWheel>", _fast_scroll, add="+")
    app.bind_all("<Shift-Button-4>", _fast_scroll, add="+")
    app.bind_all("<Shift-Button-5>", _fast_scroll, add="+")
