"""Qt main window — header / body / footer rows + bottom log panel.

  header row : top bar (game/profile + actions) | play bar      (fixed split)
  body row   : modlist ║ plugins                                (draggable)
  footer row : mod tools | plugin tools                         (fixed split)
  log panel  : drag-resizable log text area + control bar
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QSize, Signal, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QMainWindow, QToolButton, QWidget, QSplitter, QApplication,
    QLabel, QVBoxLayout, QHBoxLayout, QPlainTextEdit,
    QFrame, QLineEdit, QPushButton, QMenu, QStackedWidget,
)

from gui_qt.theme_qt import apply_theme, active_palette, _c
from gui_qt.icons import icon
from gui_qt.modlist_model import ModListModel, COL_SIZE
from gui_qt.modlist_view import ModListView
from gui_qt.selector_button import SelectorButton
from gui_qt.game_state import GameState
from gui_qt.detachable_tabs import DetachableTabWidget
from gui_qt import glue
from Utils.proton_tools import DOTNET_VERSIONS


class MainWindow(QMainWindow):
    # Carries (generation, ConflictData) from a worker thread to the UI thread
    # (queued connection — thread-safe). See _rebuild_conflicts_async.
    _conflicts_ready = Signal(int, object)
    # Deploy/restore worker → UI thread (thread-safe queued connections).
    _op_progress = Signal(int, int, object)   # (done, total, phase|None)
    _op_log = Signal(str)
    _op_done = Signal(str, bool, object)       # (kind, success, warnings-list)
    # Install worker → UI thread.
    _install_done = Signal(int, int, object)   # (ok_count, total, names-list)
    _prepared_ready = Signal(object)           # (PreparedInstall|None)
    _one_install_done = Signal(object)         # (installed name|None)
    # Worker asks the UI to show the Set-Prefix overlay; payload carries a
    # result holder + threading.Event the worker blocks on.
    _need_prefix = Signal(object)              # (dict with required/file_list/...)
    # Worker asks the UI to show the Mod-Already-Exists overlay (same blocking
    # holder+Event handshake as _need_prefix).
    _mod_exists = Signal(object)               # (dict with mod_name/conflict/holder/event)
    # Proton-tools installer worker → UI thread.
    _proton_done = Signal(str, bool)           # (title, success)
    # Nexus validate() worker → UI thread (username or None).
    _nexus_validated = Signal(object)          # (username str | None)
    # LOOT Sort Plugins worker → UI thread (SortResult | None on error).
    _sort_plugins_ready = Signal(object)
    # Filter-data disk scan worker → UI thread (gen, payload dict | None).
    _filter_data_ready = Signal(int, object)
    # Nexus OAuth client (background thread) → UI thread.
    # (kind, payload): kind in {"token","error","status"}.
    _oauth_event = Signal(str, object)
    # Check-for-updates worker → UI thread ((updates, missing) | None on error).
    _updates_ready = Signal(object)
    # Endorse/abstain worker → UI thread ({"ok": n, "endorse": bool}).
    _endorse_done = Signal(object)
    # Copy/Move-to-profile worker → UI thread (result dict).
    _copy_done = Signal(object)
    # Install-a-Nexus-mod-by-id flow (used by Missing Requirements) → UI thread.
    _qu_resolved = Signal(object, object)         # (queue list, skipped list)
    _qu_downloaded = Signal(object, object)       # (dl_items list, failed list)
    _req_install_files = Signal(object, object)   # (ctx dict, files|None)
    _req_install_dl = Signal(object, object)      # (archive|None, meta|None)
    # Collection reset-load-order worker → UI thread (result dict).
    _reset_done = Signal(object)
    # Collection INSTALL worker → UI thread. Every callback is a single emit; the
    # connected slots (UI thread) update the install overlay. (verb, payload)
    # batches keep the surface small. See _install_collection.
    _col_status = Signal(str)
    _col_progress = Signal(object)             # float | None
    _col_agg = Signal(int, int, float)         # bytes cur, total, MB/s
    _col_dl = Signal(str, object)              # ("start"|"update"|"finish", payload)
    _col_extract = Signal(str, object)         # ("queue"|"add"|"remove", payload)
    _col_row = Signal(int)                     # file_id installed
    _col_finished = Signal(str, object)        # ("done"|"paused"|"cancelled", payload)
    _col_import_done = Signal(object)          # (profile_name, installed, total, skipped)
    _import_file_picked = Signal(object)       # portal picker result (Path|None) → UI thread
    # Worker blocks on these to show the deferred FOMOD / BAIN pickers (holder+Event).
    _col_fomod = Signal(object)
    _col_bain = Signal(object)

    _PLAY_BAR_W = 380       # play-bar (header right) fixed width
    _BTN_H = 42          # consistent height for all header buttons (~30% bigger)
    _ICON_PX = 24        # header button icon size
    _FOOT_BTN_H = 28     # compact height for footer tool buttons
    _FOOT_ICON_PX = 16   # footer button icon size

    def __init__(self, app):
        super().__init__()
        self._app = app
        self._pal = active_palette()
        self._gs = GameState()
        self._gs.load()
        self._conflicts_ready.connect(self._on_conflicts_ready)
        # Deploy/restore state + notification host.
        self._deploy_running = False
        self._deploy_rerun_pending = False
        self._progress_popup = None
        self._notifier = None
        self._op_progress.connect(self._on_op_progress)
        self._op_log.connect(self._append_log)
        self._op_done.connect(self._on_op_done)
        self._install_running = False
        self._install_done.connect(self._on_install_done)
        self._prepared_ready.connect(self._on_prepared_ready)
        self._one_install_done.connect(self._on_one_install_done)
        self._need_prefix.connect(self._on_need_prefix_ui)
        self._mod_exists.connect(self._on_mod_exists_ui)
        self._proton_busy = False
        self._proton_done.connect(self._on_proton_done)
        # Game-scoped panel views (lazily built; closed on game change).
        self._profile_settings_view = None
        self._dll_overrides_view = None
        self._sort_running = False
        self._sort_plugins_ready.connect(self._on_sort_plugins_ready)
        self._filter_data_gen = 0
        self._filter_data_ready.connect(self._on_filter_data_ready)
        self.setWindowTitle("Amethyst Mod Manager")
        self.setMinimumSize(1280, 800)   # Steam Deck is the floor
        self.resize(1280, 800)
        # Centre on the primary screen. The WM otherwise defaults the window to
        # (0,0) in GLOBAL coords, which lands OFF-SCREEN on a multi-head / offset
        # layout (e.g. a primary screen whose origin isn't at x=0) — the window
        # then "doesn't open" because it's drawn where you can't see it.
        try:
            scr = (app or QApplication.instance()).primaryScreen()
            if scr is not None:
                ag = scr.availableGeometry()
                self.move(ag.center().x() - 640, ag.center().y() - 400)
        except Exception:
            pass

        # Header+body+footer go in a vertical splitter with the log text area
        # so the log is drag-resizable; the log control bar stays fixed below.
        main_content = QWidget()
        mc = QVBoxLayout(main_content)
        mc.setContentsMargins(0, 0, 0, 0)
        mc.setSpacing(0)
        mc.addWidget(self._build_header_row())
        mc.addWidget(self._build_body_row(), 1)
        # (The tool footers now live inside each panel — see _build_body_row /
        # _build_modlist_area — not in a separate window-wide row.)

        # The main content is the permanent first tab; overlay-style views (Add
        # Game, Nexus browser, …) open as further tabs that can be detached.
        self._tabs = DetachableTabWidget()
        self._tabs.add_permanent(main_content, "Mods")

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setObjectName("LogView")
        self._log_view.setMinimumHeight(0)   # can collapse fully

        self._vsplit = QSplitter(Qt.Vertical)
        self._vsplit.addWidget(self._tabs)
        self._vsplit.addWidget(self._log_view)
        self._vsplit.setStretchFactor(0, 1)
        self._vsplit.setStretchFactor(1, 0)
        self._vsplit.setCollapsible(0, True)     # drag past min → log fills window
        self._vsplit.setCollapsible(1, True)     # log collapses to 0; handle stays
        self._vsplit.setHandleWidth(4)
        self._vsplit.splitterMoved.connect(lambda *_: self._sync_log_controls())

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._vsplit, 1)
        outer.addWidget(self._build_log_bar())   # fixed control bar
        self.setCentralWidget(central)

        wired = glue.register_all(
            app, log=self._append_log, parent_window=self,
        )
        print("[gui_qt] glue wired:", ", ".join(wired))

        # Start with the log collapsed (deferred until the layout has real size).
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda: (self._vsplit.setSizes(
            [self._vsplit.height(), 0]), self._sync_log_controls()))

        # Populate selectors from discovered games and load the active modlist.
        self._populate_selectors()
        self._reload_modlist()
        self._reload_plugins()
        self._update_deployed_profile_highlight()

        # Global keyboard shortcuts (F2/F5/Ctrl+D/… — Tk parity) + last-panel
        # tracking so shortcuts route to the modlist or plugins panel.
        from gui_qt.shortcuts import register_shortcuts
        register_shortcuts(self)

        # Connect to Nexus (if logged in) so the footer can show the username +
        # rate limits; validate runs on a worker so startup isn't blocked.
        self._nexus_api = None
        self._nexus_validated.connect(self._on_nexus_validated)
        # Live OAuth login client while a browser login is in flight (kept so the
        # "Paste login code" fallback can feed its session). None when idle.
        self._oauth_client = None
        self._oauth_event.connect(self._on_oauth_event)
        # Check-for-updates: re-entrancy guard + worker→UI signal.
        self._updates_running = False
        self._updates_ready.connect(self._on_updates_ready)
        # Quick Update: resolve (worker) → download (worker) → install (batch).
        self._quick_updating = False
        self._qu_resolved.connect(self._on_qu_resolved)
        self._qu_downloaded.connect(self._on_qu_downloaded)
        self._endorse_done.connect(self._on_endorse_done)
        self._copy_done.connect(self._on_copy_done)
        # Install-a-Nexus-mod-by-id (Missing Requirements) flow.
        self._req_installing = False
        self._req_install_files.connect(self._on_req_install_files)
        self._req_install_dl.connect(self._on_req_install_dl)
        self._reset_done.connect(self._on_reset_done)
        self._reset_running = False
        # Collection install state + Signal→UI-thread wiring.
        self._col_install_running = False
        self._col_install_overlay = None
        self._col_install_control = None
        self._col_install_slug = ""
        self._col_bundle_zip = ""      # local .amethyst pending bundle extraction
        self._col_status.connect(self._on_col_status)
        self._col_progress.connect(self._on_col_progress)
        self._col_agg.connect(self._on_col_agg)
        self._col_dl.connect(self._on_col_dl)
        self._col_extract.connect(self._on_col_extract)
        self._col_row.connect(self._on_col_row)
        self._col_finished.connect(self._on_col_finished)
        self._col_import_done.connect(self._on_import_bundle_done)
        self._import_file_picked.connect(self._on_import_file_picked)
        self._col_fomod.connect(self._on_col_fomod_ui)
        self._col_bain.connect(self._on_col_bain_ui)
        QTimer.singleShot(0, self._ensure_nexus_api)

    def _populate_selectors(self):
        """Fill the game/profile selectors from the current GameState."""
        gs = self._gs
        if gs.game_name:
            self._game_selector.set_items(gs.game_names, current=gs.game_name)
        self._play_game_selector.set_items(
            gs.game_names or ["—"], current=gs.game_name or "—")
        profs = gs.profiles()
        if profs:
            self._profile_selector.set_items(profs, current=gs.profile)

    # ---------------------------------------------------------- header row
    def _build_header_row(self) -> QWidget:
        """Full-width top bar (selectors + action buttons) — spans the whole
        window so the buttons have room. The Play section now lives above the
        plugins panel (see _build_body_row), not here."""
        return self._left_header()

    # ---------------------------------------------------------- body row
    def _build_body_row(self) -> QWidget:
        # Each side is a self-contained column: the modlist column owns its tool
        # footer (buttons + search), and the plugins column owns the Play bar on
        # top plus the plugins tool footer at the bottom. The footers live INSIDE
        # the panels (not a separate window-wide row) so they move/resize with
        # their panel. The splitter divides the two columns.
        right_col = QWidget()
        rc = QVBoxLayout(right_col)
        rc.setContentsMargins(0, 0, 0, 0)
        rc.setSpacing(0)
        play = self._play_bar()
        self._play_bar_widget = play
        rc.addWidget(play)
        # Build the plugins panel FIRST — it creates the sub-tab views (incl.
        # _text_files_view) that the footers below reference.
        plugins_body = self._build_plugins()
        # The plugins column footer is a stack: it swaps to the active sub-tab's
        # tools (Plugins tools ↔ Mod Files Pack/Unpack + search).
        self._plugin_footer_stack = QStackedWidget()
        self._plugin_footer_stack.addWidget(self._plugins_footer())       # page 0
        self._plugin_footer_stack.addWidget(self._mod_files_footer())     # page 1
        self._plugin_footer_stack.addWidget(self._data_footer())          # page 2
        self._plugin_footer_stack.addWidget(self._downloads_footer())     # page 3
        self._plugin_footer_stack.addWidget(self._text_files_footer())    # page 4
        # The whole plugins panel (sub-tab strip + content + footer, but NOT the
        # Play bar) lives in a stack so a panel-scoped tab (Change Version) can
        # take it over entirely. Page 0 = the plugins panel + its footer.
        plugins_panel = QWidget()
        pp = QVBoxLayout(plugins_panel)
        pp.setContentsMargins(0, 0, 0, 0)
        pp.setSpacing(0)
        pp.addWidget(plugins_body, 1)
        pp.addWidget(self._plugin_footer_stack)
        self._plugins_panel_stack = QStackedWidget()
        self._plugins_panel_stack.addWidget(plugins_panel)               # page 0
        rc.addWidget(self._plugins_panel_stack, 1)
        self._right_col = right_col

        # The modlist column lives in a stack so a panel-scoped tab (e.g. an
        # image preview) can take over JUST the modlist region while the plugins
        # panel (with the Mod Files tree) stays live. Page 0 = the real modlist.
        self._modlist_panel_stack = QStackedWidget()
        self._modlist_panel_stack.addWidget(self._build_modlist_area())

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._modlist_panel_stack)
        split.addWidget(right_col)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 4)
        # Wider handle so a panel collapsed to one edge still has an easy-to-grab
        # grip (the default 1px line is nearly invisible / hard to click).
        split.setHandleWidth(8)
        split.setChildrenCollapsible(True)
        split.setSizes([620, 480])
        self._body_split = split
        self._wire_cross_panel()
        return split

    def _wire_cross_panel(self):
        """Connect modlist ↔ plugins selection so picking a mod highlights its
        plugins (+ conflict-tinted ones) and picking a plugin highlights its
        owning mod (Tk parity)."""
        self._conflict_data = None
        mv, pv = self._modlist_view, self._plugin_view
        mv.selectionModel().selectionChanged.connect(
            lambda *_: self._on_mod_selection_changed())
        pv.selectionModel().selectionChanged.connect(
            lambda *_: self._on_plugin_selection_changed())

    def _suppress_xpanel(self) -> bool:
        return getattr(self, "_xpanel_busy", False)

    def _on_mod_selection_changed(self):
        if self._suppress_xpanel():
            return
        self._xpanel_busy = True
        try:
            mv, pv = self._modlist_view, self._plugin_view
            names = mv.selected_mod_names()
            # Modlist rows: green/red tint on ALL conflict partners (loose+BSA).
            higher, lower = mv.conflict_partners(names)
            # Tk quirk: the [Overwrite] band lights GREEN when it wins over the
            # selection (so the user sees Overwrite is active), even though a
            # normal winning mod would be red. Flip it from lower→higher.
            from Utils.filemap import OVERWRITE_NAME
            if OVERWRITE_NAME in lower:
                lower = lower - {OVERWRITE_NAME}
                higher = higher | {OVERWRITE_NAME}
            mv.model().set_highlights(higher=higher, lower=lower)
            # Plugins panel: orange the selected mods' plugins. Green/red is
            # applied ONLY for BSA conflicts (Tk parity) — loose-file conflicts
            # do NOT colour plugins — and only to plugins that own a BSA.
            cd = self._conflict_data
            owner = cd.plugin_owner if cd else {}
            bsa_higher, bsa_lower = mv.bsa_conflict_partners(names)
            pv.set_highlight_from_mods(
                names, bsa_higher, bsa_lower, owner,
                bsa_index_path=self._gs.bsa_index_path())
            # Picking a mod clears any plugin selection (mutual exclusivity).
            pv.clearSelection()
            # Feed the Mod Files tab the single selected mod (real mods only).
            self._update_mod_files_selection(names)
        finally:
            self._xpanel_busy = False

    def _update_mod_files_selection(self, _names):
        """Show the single selected mod in the Mod Files tab. A separator or a
        multi-selection shows nothing (the separator overview is a later step)."""
        mv = getattr(self, "_mod_files_view", None)
        if mv is None:
            return
        rows = self._modlist_view.selectionModel().selectedRows()
        if len(rows) != 1:
            mv.show_mod(None)
            return
        e = self._modlist_model.entry(rows[0].row())
        from gui_qt.modlist_model import _BOUNDARY_NAMES
        if e.is_separator or e.name in _BOUNDARY_NAMES:
            mv.show_mod(None)
        else:
            mv.show_mod(e.name)

    def _open_settings_tab(self):
        """Open the Settings tab scoped over the MODLIST panel (like the image
        preview / text editor): it shows in the modlist region (in the shared top
        tab bar) while the plugins panel and the rest of the UI stay live.
        Re-clicking the gear focuses the existing tab."""
        from gui_qt.settings_view import SettingsView
        if self._tabs.has_key("settings"):
            self._tabs.focus_key("settings")
            return
        view = SettingsView(self)
        self._tabs.open_scoped_tab(
            view, "Settings", self._modlist_panel_stack, key="settings")

    def _open_image_preview_tab(self, path, rel_str):
        """Open an image/.dds preview as a MODLIST-PANEL-SCOPED tab: it shows in
        the modlist region (in the shared top tab bar) while the Mod Files tree
        in the plugins panel stays live. Reuses one preview tab — browsing to a
        new image swaps it in place (Tk parity)."""
        from pathlib import Path as _P
        from gui_qt.image_preview import ImagePreview
        name = rel_str.replace("\\", "/").rsplit("/", 1)[-1]
        existing = getattr(self, "_image_preview_widget", None)
        if existing is not None and self._tabs.has_key("mf_image_preview"):
            existing.set_image(_P(path), name)
            self._tabs.focus_key("mf_image_preview")
            self._tabs.set_tab_title("mf_image_preview", name)
            return
        widget = ImagePreview(_P(path), name)
        self._image_preview_widget = widget
        self._tabs.open_scoped_tab(
            widget, name, self._modlist_panel_stack, key="mf_image_preview")

    def _open_text_editor_tab(self, path, rel_str, find_kw=None):
        """Open a text file in a save-capable editor as a MODLIST-PANEL-SCOPED tab
        (the other panels stay live). Reuses one editor — clicking another file
        swaps it in place. The tab title gets a '*' while there are unsaved edits.
        *find_kw* (the active content-search keyword) is pre-highlighted."""
        from pathlib import Path as _P
        from gui_qt.text_editor import TextEditor
        name = rel_str.replace("\\", "/").rsplit("/", 1)[-1]
        existing = getattr(self, "_text_editor_widget", None)
        if existing is not None and self._tabs.has_key("tf_text_editor"):
            existing.load_file(_P(path), name)
            if find_kw:
                existing.find_text(find_kw)
            self._tabs.focus_key("tf_text_editor")
            self._tabs.set_tab_title("tf_text_editor", name)
            return
        widget = TextEditor(_P(path), name)
        self._text_editor_widget = widget
        widget.dirty_changed.connect(self._on_text_editor_dirty)
        widget.saved.connect(self._on_text_editor_saved)
        self._tabs.open_scoped_tab(
            widget, name, self._modlist_panel_stack, key="tf_text_editor")
        if find_kw:
            widget.find_text(find_kw)

    def _on_text_editor_dirty(self, dirty):
        w = getattr(self, "_text_editor_widget", None)
        if w is not None and self._tabs.has_key("tf_text_editor"):
            self._tabs.set_tab_title(
                "tf_text_editor", (w.name + " *") if dirty else w.name)

    def _on_text_editor_saved(self):
        # File content changed on disk → the Text Files content search may shift.
        if hasattr(self, "_text_files_view"):
            self._text_files_view.mark_dirty()
        self._notify("Saved", "success")

    def _on_mod_files_changed(self):
        """A Top Level / Disable edit changed deploy state — force a full index
        rescan (strip prefixes apply at scan time) + rebuild conflicts."""
        # Instant feedback: update the modlist "modified in Mod Files" eye flag
        # now (the async rescan below refreshes the rest a moment later).
        if hasattr(self, "_modlist_model"):
            self._modlist_model.set_modified_mf(self._build_modified_mf_mods())
        self._rebuild_conflicts_async(rescan_index=True)

    def _on_plugin_selection_changed(self):
        if self._suppress_xpanel():
            return
        self._xpanel_busy = True
        try:
            mv, pv = self._modlist_view, self._plugin_view
            owner = (self._conflict_data.plugin_owner
                     if self._conflict_data else {})
            mods = pv.selected_owner_mods(owner)
            # Plugin selected → orange its owning mod, clear mod conflict tint.
            mv.set_highlighted_mods(mods)
            mv.clearSelection()
            # Green-tick the selected plugin's masters in the plugin marker
            # strip (Tk parity).
            pv.set_master_highlight(self._selected_plugin_masters())
        finally:
            self._xpanel_busy = False

    def _selected_plugin_masters(self) -> set:
        """Lowercase master filenames of the currently-selected plugin(s), read
        from each plugin's resolved on-disk path. Empty if nothing selected or
        paths are unavailable."""
        pv = self._plugin_view
        rows = pv.selectionModel().selectedRows()
        if not rows:
            return set()
        paths = getattr(self, "_plugin_paths", None)
        if not paths:
            return set()
        from Utils.plugin_parser import read_masters
        masters: set = set()
        for idx in rows:
            r = self._plugin_model.row(idx.row())
            p = paths.get(r.name.lower())
            if p is None:
                continue
            try:
                masters.update(m.lower() for m in read_masters(p))
            except Exception:
                pass
        return masters

    def _on_data_select_mod(self, mod):
        """A Data-tab file row was selected → orange-highlight its winning mod in
        the modlist (Tk parity); a folder row clears it."""
        if self._suppress_xpanel():
            return
        self._xpanel_busy = True
        try:
            mv = self._modlist_view
            mv.set_highlighted_mods({mod} if mod else set())
            mv.clearSelection()
        finally:
            self._xpanel_busy = False

    # ---------------------------------------------------------- panel footers
    def _modlist_footer(self) -> QWidget:
        """Buttons row + search box — lives at the bottom of the modlist panel."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        # Stats row (pills) above the buttons: enabled / disabled. Spacing matches
        # the button row (4px) so, once the first pill is sized to the first
        # button's width, the second pill lines up under the second button.
        from gui_qt.stats_bar import StatsBar
        self._modlist_stats = StatsBar(placeholder="…", spacing=4)
        v.addWidget(self._modlist_stats)

        btns = QHBoxLayout()
        btns.setSpacing(4)
        # label -> handler ("" = no-op stub, needs a dialog/auth — wired later).
        _handlers = {
            "Expand all": self._on_toggle_collapse_all,
            "Enable all": self._on_toggle_enable_all,
            "Filters": self._toggle_modlist_filters,
            "Refresh Modlist": self._on_refresh_modlist,
            "Check Updates": self._on_check_updates,
            "Restore backup": self._open_restore_backup_tab,
        }
        self._modlist_footer_btns: list[QToolButton] = []
        for label in ["Expand all", "Enable all", "Check Updates", "Filters",
                      "Restore backup", "Refresh Modlist"]:
            b = self._text_button(label, compact=True)
            b.setFixedHeight(self._FOOT_BTN_H)
            # Reserve the label's natural width so it never squashes at min size.
            b.setMinimumWidth(b.sizeHint().width())
            if label in _handlers:
                b.clicked.connect(_handlers[label])
            if label == "Filters":
                b.setProperty("active", False)
                self._modlist_filters_btn = b
            elif label == "Expand all":
                self._expand_all_btn = b
            elif label == "Enable all":
                self._enable_all_btn = b
            elif label == "Check Updates":
                self._check_updates_btn = b
            btns.addWidget(b)
            self._modlist_footer_btns.append(b)
        btns.addStretch(1)
        v.addLayout(btns)

        # Search box, capped to the same width as the button row so its right
        # edge lines up with the last button (a trailing stretch absorbs the
        # leftover, instead of the box spanning the whole footer).
        search = QLineEdit()
        search.setPlaceholderText("Search mods…")
        search.setClearButtonEnabled(True)
        search.textChanged.connect(self._on_modlist_search)
        self._modlist_search = search
        srow = QHBoxLayout()
        srow.setContentsMargins(0, 0, 0, 0)
        srow.addWidget(search)
        srow.addStretch(1)
        v.addLayout(srow)
        # Match the search width to the button row once layout has settled
        # (sizeHints are only final after the widgets are realised).
        QTimer.singleShot(0, self._sync_modlist_search_width)
        return bar

    def _sync_modlist_search_width(self):
        """Cap the modlist search box to the combined width of the footer button
        row so its right edge aligns with the last button, and size the stat
        pills so each lines up under its button (Enabled↔Expand all,
        Disabled↔Disable all)."""
        btns = getattr(self, "_modlist_footer_btns", None)
        search = getattr(self, "_modlist_search", None)
        if not btns or search is None:
            return
        spacing = 4
        total = sum(b.sizeHint().width() for b in btns) + spacing * (len(btns) - 1)
        search.setFixedWidth(total)
        # Align the pills: fix all-but-the-last pill to the matching button's
        # width so the next pill (and the button below it) share the same left
        # edge; matching row spacing + equal text inset lines up the letters.
        stats = getattr(self, "_modlist_stats", None)
        if stats is not None and stats._pills:
            n = len(stats._pills)
            widths = [btns[i].sizeHint().width() if i < len(btns) else 0
                      for i in range(n)]
            widths[-1] = 0            # last pill keeps its natural width
            stats.align_pills_to_widths(widths)

    def _plugins_footer(self) -> QWidget:
        """Colored tool buttons + search, under the plugins."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        # Stats row (pills) above the buttons: total / ESL / non-ESL plugins.
        # Spacing matches the button row so the pills can line up under buttons.
        from gui_qt.stats_bar import StatsBar
        self._plugin_stats = StatsBar(placeholder="…", spacing=4)
        v.addWidget(self._plugin_stats)

        btns = QHBoxLayout()
        btns.setSpacing(4)
        _made = {}
        self._plugin_footer_btns: list = []
        for label, key in [
            ("Sort Plugins", "BTN_SUCCESS"),
            ("Groups", "BTN_INFO"),
            ("Plugin Rules", "BTN_INFO"),
            ("Filters", "BTN_INFO"),
        ]:
            b = self._color_button(label, _c(self._pal, key), compact=True)
            b.setFixedHeight(self._FOOT_BTN_H)
            btns.addWidget(b)
            _made[label] = b
            self._plugin_footer_btns.append(b)
        btns.addStretch(1)
        v.addLayout(btns)
        # Store + wire the buttons that are implemented (Groups / Plugin Rules
        # are deferred — they stay as inert placeholders for now).
        self._plugin_sort_btn = _made["Sort Plugins"]
        self._plugin_groups_btn = _made["Groups"]
        self._plugin_rules_btn = _made["Plugin Rules"]
        self._plugin_filters_btn = _made["Filters"]
        self._plugin_sort_btn.clicked.connect(self._on_sort_plugins)
        self._plugin_filters_btn.clicked.connect(self._toggle_plugin_filters)

        search = QLineEdit()
        search.setPlaceholderText("Search plugins…")
        search.setClearButtonEnabled(True)
        search.textChanged.connect(self._on_plugin_search)
        v.addWidget(search)
        self._plugins_search = search
        # Align the stat pills under their buttons once layout has settled.
        QTimer.singleShot(0, self._sync_plugin_stats_align)
        return bar

    def _sync_plugin_stats_align(self):
        """Size the plugin stat pills so each lines up under its button
        (Plugins↔Sort Plugins, ESL↔Groups, Non-ESL↔Plugin Rules)."""
        btns = getattr(self, "_plugin_footer_btns", None)
        stats = getattr(self, "_plugin_stats", None)
        if not btns or stats is None or not stats._pills:
            return
        n = len(stats._pills)
        widths = [btns[i].sizeHint().width() if i < len(btns) else 0
                  for i in range(n)]
        widths[-1] = 0
        stats.align_pills_to_widths(widths)

    def _mod_files_footer(self) -> QWidget:
        """Pack/Unpack BSA + Filters buttons + search, shown under the plugins
        column when the Mod Files sub-tab is active (replaces the plugin tools)."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        btns = QHBoxLayout()
        btns.setSpacing(4)
        self._mf_pack_btn = self._color_button(
            "Pack BSA", _c(self._pal, "BTN_SUCCESS"), compact=True)
        self._mf_pack_btn.setFixedHeight(self._FOOT_BTN_H)
        self._mf_pack_btn.setEnabled(False)
        self._mf_pack_btn.clicked.connect(
            lambda: self._mod_files_view._on_pack())
        self._mf_unpack_btn = self._color_button(
            "Unpack BSA", _c(self._pal, "BTN_DANGER"), compact=True)
        self._mf_unpack_btn.setFixedHeight(self._FOOT_BTN_H)
        self._mf_unpack_btn.setEnabled(False)
        self._mf_unpack_btn.clicked.connect(
            lambda: self._mod_files_view._on_unpack())
        self._mf_filters_btn = self._color_button(
            "Filters", _c(self._pal, "BTN_INFO"), compact=True)
        self._mf_filters_btn.setFixedHeight(self._FOOT_BTN_H)
        self._mf_filters_btn.clicked.connect(self._toggle_mod_files_filters)
        self._mf_expand_btn = self._text_button("⊞ Expand all", compact=True)
        self._mf_expand_btn.setFixedHeight(self._FOOT_BTN_H)
        self._mf_expand_btn.clicked.connect(self._on_mf_expand_clicked)
        btns.addWidget(self._mf_pack_btn)
        btns.addWidget(self._mf_unpack_btn)
        btns.addWidget(self._mf_filters_btn)
        btns.addWidget(self._mf_expand_btn)
        btns.addStretch(1)
        v.addLayout(btns)

        search = QLineEdit()
        search.setPlaceholderText("Search files…")
        search.setClearButtonEnabled(True)
        search.textChanged.connect(
            lambda t: self._mod_files_view._on_search(t))
        v.addWidget(search)
        self._mod_files_search = search
        return bar

    def _data_footer(self) -> QWidget:
        """Filters + Expand-all + search, shown under the plugins column when the
        Data sub-tab is active (no Pack/Unpack — Data is read-only)."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        btns = QHBoxLayout()
        btns.setSpacing(4)
        self._data_filters_btn = self._color_button(
            "Filters", _c(self._pal, "BTN_INFO"), compact=True)
        self._data_filters_btn.setFixedHeight(self._FOOT_BTN_H)
        self._data_filters_btn.clicked.connect(self._toggle_data_filters)
        self._data_expand_btn = self._text_button("⊞ Expand all", compact=True)
        self._data_expand_btn.setFixedHeight(self._FOOT_BTN_H)
        self._data_expand_btn.clicked.connect(self._on_data_expand_clicked)
        btns.addWidget(self._data_filters_btn)
        btns.addWidget(self._data_expand_btn)
        btns.addStretch(1)
        v.addLayout(btns)

        search = QLineEdit()
        search.setPlaceholderText("Search files…")
        search.setClearButtonEnabled(True)
        search.textChanged.connect(lambda t: self._data_view._on_search(t))
        v.addWidget(search)
        self._data_search = search
        return bar

    def _on_data_expand_clicked(self):
        expanded = self._data_view._toggle_expand_all()
        self._data_expand_btn.setText("⊟ Collapse all" if expanded
                                      else "⊞ Expand all")

    def _downloads_footer(self) -> QWidget:
        """Install Selected / Remove Selected / Locations / Filters + search,
        shown under the plugins column when the Downloads sub-tab is active."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        btns = QHBoxLayout()
        btns.setSpacing(4)
        self._dl_install_btn = self._color_button(
            "Install Selected", _c(self._pal, "BTN_SUCCESS"), compact=True)
        self._dl_install_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_install_btn.setEnabled(False)
        self._dl_install_btn.clicked.connect(
            lambda: self._downloads_view.install_selected())
        self._dl_remove_btn = self._color_button(
            "Remove Selected", _c(self._pal, "BTN_DANGER"), compact=True)
        self._dl_remove_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_remove_btn.setEnabled(False)
        self._dl_remove_btn.clicked.connect(self._on_downloads_remove)
        self._dl_locations_btn = self._color_button(
            "Locations", _c(self._pal, "BTN_INFO"), compact=True)
        self._dl_locations_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_locations_btn.clicked.connect(self._on_downloads_locations)
        self._dl_filters_btn = self._color_button(
            "Filters", _c(self._pal, "BTN_INFO"), compact=True)
        self._dl_filters_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_filters_btn.clicked.connect(self._toggle_downloads_filters)
        btns.addWidget(self._dl_install_btn)
        btns.addWidget(self._dl_remove_btn)
        btns.addWidget(self._dl_locations_btn)
        btns.addWidget(self._dl_filters_btn)
        btns.addStretch(1)
        v.addLayout(btns)

        search = QLineEdit()
        search.setPlaceholderText("Search downloads…")
        search.setClearButtonEnabled(True)
        search.textChanged.connect(lambda t: self._downloads_view._on_search(t))
        v.addWidget(search)
        self._dl_search = search
        return bar

    def _update_downloads_footer(self):
        n = self._downloads_view.checked_count()
        for attr, label in (("_dl_install_btn", "Install Selected"),
                            ("_dl_remove_btn", "Remove Selected")):
            b = getattr(self, attr, None)
            if b is not None:
                b.setEnabled(n > 0)
                b.setText(f"{label} ({n})" if n else label)

    def _on_downloads_locations(self):
        from gui_qt.download_locations_dialog import DownloadLocationsDialog
        dlg = DownloadLocationsDialog(self)
        if dlg.exec():
            self._downloads_view.refresh()

    def _on_downloads_remove(self):
        from PySide6.QtWidgets import QMessageBox
        paths = self._downloads_view.checked_paths()
        if not paths:
            return
        names = "\n".join(Path(p).name for p in paths[:20])
        more = f"\n… and {len(paths) - 20} more" if len(paths) > 20 else ""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Remove archives")
        box.setText(f"Permanently delete {len(paths)} archive(s) from disk?")
        box.setInformativeText(names + more)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return
        removed = 0
        for p in paths:
            try:
                Path(p).unlink()
                removed += 1
            except OSError as exc:
                print(f"[gui_qt] remove failed: {p}: {exc}", flush=True)
        self._notify(f"Removed {removed} archive(s)", "info")
        self._downloads_view.clear_checks()
        self._downloads_view.refresh()

    def _text_files_footer(self) -> QWidget:
        """Search Content / Filters + search, shown under the plugins column when
        the Text Files sub-tab is active (read-only — no Pack/Install)."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        # Inline content-search popup (hidden until "Search Content" is clicked).
        self._tf_content_bar = QWidget()
        cbl = QHBoxLayout(self._tf_content_bar)
        cbl.setContentsMargins(0, 0, 0, 0)
        cbl.setSpacing(4)
        lbl = QLabel("Find in files:")
        lbl.setStyleSheet(f"color:{_c(self._pal,'TEXT_DIM')};")
        cbl.addWidget(lbl)
        self._tf_content_input = QLineEdit()
        self._tf_content_input.setPlaceholderText("Text to search for…")
        self._tf_content_input.setClearButtonEnabled(True)
        self._tf_content_input.returnPressed.connect(self._run_tf_content_search)
        cbl.addWidget(self._tf_content_input, 1)
        go = self._color_button("Search", _c(self._pal, "BTN_SUCCESS"), compact=True)
        go.setFixedHeight(self._FOOT_BTN_H)
        go.clicked.connect(self._run_tf_content_search)
        cbl.addWidget(go)
        close = self._text_button("✕", compact=True)
        close.setFixedHeight(self._FOOT_BTN_H)
        close.clicked.connect(self._close_tf_content_bar)
        cbl.addWidget(close)
        self._tf_content_bar.setVisible(False)
        v.addWidget(self._tf_content_bar)

        btns = QHBoxLayout()
        btns.setSpacing(4)
        self._tf_content_btn = self._color_button(
            "Search Content", _c(self._pal, "BTN_INFO"), compact=True)
        self._tf_content_btn.setFixedHeight(self._FOOT_BTN_H)
        self._tf_content_btn.clicked.connect(self._on_text_files_content_search)
        self._tf_filters_btn = self._color_button(
            "Filters", _c(self._pal, "BTN_INFO"), compact=True)
        self._tf_filters_btn.setFixedHeight(self._FOOT_BTN_H)
        self._tf_filters_btn.clicked.connect(self._toggle_text_files_filters)
        btns.addWidget(self._tf_content_btn)
        btns.addWidget(self._tf_filters_btn)
        btns.addStretch(1)
        # A dim status label showing the active content-search keyword.
        self._tf_content_status = QLabel("")
        self._tf_content_status.setStyleSheet(
            f"color:{_c(self._pal,'TEXT_DIM')};")
        btns.addWidget(self._tf_content_status)
        v.addLayout(btns)
        self._text_files_view.content_status_changed.connect(
            self._on_tf_content_status)

        search = QLineEdit()
        search.setPlaceholderText("Search files…")
        search.setClearButtonEnabled(True)
        search.textChanged.connect(lambda t: self._text_files_view._on_search(t))
        v.addWidget(search)
        self._tf_search = search
        return bar

    def _on_text_files_content_search(self):
        """Toggle the inline content-search bar. When a search is active, this
        clears it; otherwise it opens the input bar above the footer + focuses it."""
        if self._text_files_view._content_keyword:
            self._text_files_view.clear_content_search()
            self._tf_content_input.clear()
            self._tf_content_bar.setVisible(False)
            return
        showing = not self._tf_content_bar.isVisible()
        self._tf_content_bar.setVisible(showing)
        if showing:
            self._tf_content_input.setFocus()
            self._tf_content_input.selectAll()

    def _run_tf_content_search(self):
        kw = self._tf_content_input.text().strip()
        if kw:
            self._text_files_view.run_content_search(kw)
        else:
            self._text_files_view.clear_content_search()

    def _close_tf_content_bar(self):
        self._tf_content_bar.setVisible(False)
        self._tf_content_input.clear()
        if self._text_files_view._content_keyword:
            self._text_files_view.clear_content_search()

    def _on_tf_content_status(self, keyword):
        if keyword:
            self._tf_content_status.setText(f'Content: "{keyword}"')
            self._tf_content_btn.setText("Clear Content")
        else:
            self._tf_content_status.setText("")
            self._tf_content_btn.setText("Search Content")

    def _left_header(self) -> QWidget:
        # Single row: game/profile selectors, then the mod-action buttons.
        header = QWidget()
        header.setObjectName("HeaderBar")
        h = QHBoxLayout(header)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(6)

        # Game selector — no label; the game names make it self-evident.
        self._game_selector = SelectorButton(
            items=["Stardew Valley", "Cyberpunk 2077", "Fallout 4",
                   "Hogwarts Legacy"],
            current="Stardew Valley",
            actions=[
                ("Add game…", lambda: self._on_game_action("add")),
                ("Configure game…", lambda: self._on_game_action("configure")),
                ("Define custom game…", lambda: self._on_game_action("custom")),
                ("Open", [
                    ("Game folder",     lambda: self._open_game_dir("game")),
                    ("Prefix folder",   lambda: self._open_game_dir("prefix")),
                    ("My Games folder", lambda: self._open_game_dir("mygames")),
                    ("AppData folder",  lambda: self._open_game_dir("appdata")),
                    ("Staging folder",  lambda: self._open_game_dir("staging")),
                    ("Profile folder",  lambda: self._open_game_dir("profile")),
                    (".config folder",  lambda: self._open_game_dir("config")),
                ]),
            ],
            on_select=self._on_game_changed,
        )
        self._game_selector.setFixedHeight(self._BTN_H)
        h.addWidget(self._game_selector)

        # Profile selector — "Profile:" prefix baked into the button text.
        self._profile_selector = SelectorButton(
            items=["default"],
            current="default",
            prefix="Profile: ",
            min_width=150,
            actions=[
                ("Add new profile…", lambda: self._on_profile_action("add")),
                ("Profile settings…", lambda: self._on_profile_action("settings")),
                ("Export profile…", lambda: self._on_profile_action("export")),
                ("Import profile…", lambda: self._on_profile_action("import")),
            ],
            on_select=self._on_profile_changed,
        )
        self._profile_selector.setFixedHeight(self._BTN_H)
        h.addWidget(self._profile_selector)

        h.addWidget(self._group_sep())

        # Plain mod-action buttons.
        self._action_buttons = []
        _handlers = {"Install Mod": self._on_install_mod,
                     "Deploy": self._on_deploy, "Restore": self._on_restore}
        for label, ico in [
            ("Install Mod", "install.png"),
            ("Deploy",      "deploy.png"),
            ("Restore",     "restore.png"),
        ]:
            b = self._action_button(label, ico)
            b.setFixedHeight(self._BTN_H)
            b.setToolTip(label)
            b._full_label = label
            if label in _handlers:
                b.clicked.connect(_handlers[label])
                if label == "Deploy":
                    self._deploy_btn = b
                elif label == "Restore":
                    self._restore_btn = b
                elif label == "Install Mod":
                    self._install_btn = b
            self._action_buttons.append(b)
            h.addWidget(b)

        # Split menu buttons (placeholder menus — wired up in a later phase).
        for label, ico, items in [
            ("Proton", "proton.png", [
                ("Run winecfg", self._proton_winecfg),
                ("Run winetricks", self._proton_winetricks),
                ("Run an .exe in this prefix…", self._proton_run_exe),
                None,
                ("Open wine registry", self._proton_regedit),
                ("Wine DLL overrides", self._proton_dll_overrides),
                None,
                ("Install VC++ Redistributable", self._proton_install_vcredist),
                ("Install d3dcompiler_47", self._proton_install_d3dcompiler),
                (".NET runtime", [
                    (f".NET {v}", (lambda v=v: self._proton_install_dotnet(v)))
                    for v in DOTNET_VERSIONS
                ]),
            ]),
            ("Wizard", "wizard.png", [
                ("Mod wizards…", None),
                ("Tool wizards…", None),
            ]),
            ("Nexus", "nexus.png", [
                ("Open Nexus Mods", self._open_nexus_browser_tab),
                None,
                ("Login to Nexus", [
                    ("Login via SSO", self._nexus_login_sso),
                    ("Paste login code…", self._nexus_paste_code),
                    ("Clear credentials", self._nexus_clear_credentials),
                    (self._nxm_menu_label(), self._nexus_toggle_nxm),
                ]),
                ("Collections", [
                    ("Browse collections…", self._open_collections_tab),
                    ("Reset load order", self._reset_collection_load_order),
                ]),
            ]),
        ]:
            b = self._menu_action_button(label, ico, items)
            b.setFixedHeight(self._BTN_H)
            b.setToolTip(label)
            b._full_label = label
            self._action_buttons.append(b)
            h.addWidget(b)

        h.addStretch(1)

        # Settings — icon-only square button on the far right. Opens a Settings
        # tab scoped over the Plugins panel.
        self._settings_button = self._icon_square_button(
            "settings.png", tooltip="Settings", tint=_c(self._pal, "TEXT_MAIN"))
        self._settings_button.clicked.connect(self._open_settings_tab)
        h.addWidget(self._settings_button)

        self._left_header_widget = header
        return header

    def _icon_square_button(self, icon_name: str, tooltip: str = "",
                            tint: str | None = None) -> QToolButton:
        """A compact square icon-only button (e.g. Settings) for the toolbar.

        *tint* recolours a mono glyph to a theme colour so it stays visible in
        both light and dark modes (the raw PNGs are white)."""
        b = QToolButton()
        b.setIcon(icon(icon_name, self._ICON_PX, color=tint))
        b.setIconSize(QSize(self._ICON_PX, self._ICON_PX))
        b.setToolButtonStyle(Qt.ToolButtonIconOnly)
        b.setObjectName("IconButton")
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedSize(self._BTN_H, self._BTN_H)
        if tooltip:
            b.setToolTip(tooltip)
        return b

    # The action buttons always show text+icon: the full-width top bar has room
    # for them even at the 1280 minimum, so the old icon-only collapse (with its
    # threshold/flicker tuning) is no longer needed.

    # ---- selector handlers -------------------------------------------------
    def _on_game_changed(self, name):
        if name == self._gs.game_name:
            return
        self._gs.set_game(name)
        # The Profile Settings tab is scoped to the previous game — close it.
        if self._tabs.has_key("profile_settings"):
            self._tabs.close_tab("profile_settings")
            self._profile_settings_view = None
        # The Wine DLL overrides tab is game-scoped too — close it.
        if self._tabs.has_key("dll_overrides"):
            self._tabs.close_tab("dll_overrides")
            self._dll_overrides_view = None
        # Retarget the Nexus / Collections browsers (if open) at the new game so
        # they show the new game's mods instead of holding a stale domain. A
        # missing/empty Nexus domain closes them (nothing to show).
        self._retarget_browsers_for_game()
        # Reflect the new game's profiles + keep both game selectors in sync.
        profs = self._gs.profiles()
        if profs:
            self._profile_selector.set_items(profs, current=self._gs.profile)
        self._game_selector.set_current(name)
        self._play_game_selector.set_current(name)
        self._clear_search_boxes()
        self._reload_modlist()
        self._reload_plugins()
        self._update_deployed_profile_highlight()

    def _retarget_browsers_for_game(self):
        """On game switch, point any open Nexus / Collections browser at the new
        game (or close them if the new game has no Nexus domain). Per-collection
        detail tabs are game-specific, so they're closed unconditionally."""
        game = self._gs.game
        domain = (getattr(game, "nexus_game_domain", "") or "") if game else ""

        # Close per-collection detail tabs (they belong to the previous game).
        try:
            for key in [k for k in list(self._tabs._keys)
                        if k.startswith("collection_detail_")]:
                self._tabs.close_tab(key)
        except Exception:
            pass

        for attr, tab_key in (("_nexus_view", "nexus_browser"),
                              ("_collections_view", "collections")):
            view = getattr(self, attr, None)
            if view is None:
                continue
            if not domain:
                # New game can't show Nexus content — close the tab.
                if self._tabs.has_key(tab_key):
                    self._tabs.close_tab(tab_key)
                setattr(self, attr, None)
                continue
            try:
                view.set_game(game, domain)
            except Exception as exc:
                self._append_log(f"[nexus] retarget failed: {exc}")

    def _clear_search_boxes(self):
        """Reset both search boxes on a game/profile switch (Tk parity — a
        stale query silently filters the freshly-loaded lists)."""
        for attr, text_attr in (("_modlist_search", "_modlist_search_text"),
                                ("_plugins_search", "_plugin_search_text")):
            box = getattr(self, attr, None)
            if box is not None and box.text():
                box.clear()   # textChanged → debounced apply sees empty
            setattr(self, text_attr, "")

    def _on_profile_changed(self, name):
        if name == self._gs.profile:
            return
        self._gs.set_profile(name)
        self._profile_selector.set_current(name)
        self._clear_search_boxes()
        self._reload_modlist()
        self._reload_plugins()
        self._update_deployed_profile_highlight()
        # Keep the Profile Settings ★ marker in sync if that tab is open.
        if self._tabs.has_key("profile_settings"):
            v = getattr(self, "_profile_settings_view", None)
            if v is not None:
                v.set_current_profile(name)
        # The collections browser's "Open Current" depends on the active profile.
        cv = getattr(self, "_collections_view", None)
        if cv is not None:
            cv.refresh_open_current()

    def _on_game_action(self, which):
        if which == "add":
            self._open_add_game_tab()
        elif which == "configure":
            game = self._gs.game
            if game is not None:
                self._open_configure_game_tab(game)
            else:
                self._append_log("[game] no active game to configure")
        elif which == "custom":
            self._open_custom_game_tab()
        else:
            self._append_log(f"[game] {which} (not wired yet)")

    def _open_custom_game_tab(self):
        """Open the Define-Custom-Game form as a (detachable) tab. On save, chain
        to the Configure-Game tab so the user can set the install path/prefix
        (a fresh custom game has no path yet → not yet selectable)."""
        if self._tabs.has_key("custom_game"):
            self._tabs.focus_key("custom_game")
            return
        from gui_qt.custom_game_view import CustomGameView

        def _done(saved_defn, deleted):
            self._tabs.close_tab("custom_game")
            if saved_defn is None:
                return
            from gui.game_helpers import _load_games, _GAMES
            names = _load_games()
            self._gs.game_names = names
            self._game_selector.set_items(names, current=self._gs.game_name)
            self._append_log(f"[game] custom game defined: {saved_defn['name']}")
            game = _GAMES.get(saved_defn["name"])
            if game is not None:
                self._open_configure_game_tab(game)

        view = CustomGameView(on_done=_done)
        self._tabs.open_tab(view, "Define custom game", key="custom_game")

    def _open_add_game_tab(self):
        """Open the Add Game card-grid picker as a (detachable) tab."""
        from gui_qt.add_game_view import AddGameView
        from gui.game_helpers import _load_games, _GAMES
        _load_games()   # refresh registry (populates _GAMES with ALL games)
        page = AddGameView(dict(_GAMES),
                           on_select=self._on_add_game_select,
                           on_add=self._on_add_game_add)
        self._tabs.open_tab(page, "Add game", key="add_game")

    def _ensure_nexus_api(self):
        """Build the shared NexusAPI from saved OAuth tokens (idempotent) and
        kick off a background validate() to learn the username. Returns the API
        or None (not logged in / connection failed). Reused by the footer and
        the Nexus browser tab so there's a single instance whose passively
        captured rate limits the footer can read."""
        if getattr(self, "_nexus_api", None) is not None:
            return self._nexus_api
        from Nexus.nexus_oauth import load_oauth_tokens
        from Nexus.nexus_api import NexusAPI
        tokens = load_oauth_tokens()
        if tokens is None:
            return None
        try:
            self._nexus_api = NexusAPI.from_oauth(tokens)
        except Exception as exc:
            self._append_log(f"[nexus] api init failed: {exc}")
            return None
        # Validate off-thread (one rate-limited call; result cached 5 min).
        import threading

        def _worker():
            name = None
            try:
                name = self._nexus_api.validate().name
            except Exception as exc:
                self._append_log(f"[nexus] validate failed: {exc}")
            self._nexus_validated.emit(name)
        threading.Thread(target=_worker, daemon=True).start()
        return self._nexus_api

    def _on_nexus_validated(self, name):
        """Worker reported the validated username (or None) — update the footer."""
        if name:
            self._append_log(f"[nexus] logged in as {name}")
        if hasattr(self, "_nexus_footer"):
            self._nexus_footer.set_username(name)

    def _open_nexus_browser_tab(self):
        """Open the Nexus Mods browser as a detachable tab. Needs a configured
        game with a Nexus domain and existing OAuth tokens (login UI deferred)."""
        if self._tabs.has_key("nexus_browser"):
            self._tabs.focus_key("nexus_browser")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return
        domain = getattr(game, "nexus_game_domain", "") or ""
        if not domain:
            self._notify(f"'{game.name}' has no Nexus Mods page.", "warning")
            return
        # Reuse the shared API (built at startup); falls back to building it now
        # if startup couldn't (e.g. the user logged in afterwards).
        api = self._ensure_nexus_api()
        if api is None:
            self._notify("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO.",
                         "warning")
            self._append_log("[nexus] no OAuth tokens — login required")
            return
        from gui_qt.nexus_browser_view import NexusBrowserView
        view = NexusBrowserView(api, domain, game,
                                install_fn=self._install_paths,
                                log_fn=self._append_log)
        self._nexus_view = view
        # Drop the reference when the tab/window is gone so we stop refreshing it.
        view.destroyed.connect(lambda *_: setattr(self, "_nexus_view", None))
        self._tabs.open_tab(view, "Nexus", key="nexus_browser")

    def _open_collections_tab(self):
        """Open the Nexus Collections browser as a detachable tab (view-only —
        the install/detail flow is a separate feature). Same guards as the mods
        browser: a configured game with a Nexus domain + OAuth tokens."""
        if self._tabs.has_key("collections"):
            cv = getattr(self, "_collections_view", None)
            if cv is not None:
                cv.refresh_open_current()
            self._tabs.focus_key("collections")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return
        domain = getattr(game, "nexus_game_domain", "") or ""
        if not domain:
            self._notify(f"'{game.name}' has no Nexus Mods page.", "warning")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO.",
                         "warning")
            self._append_log("[nexus] no OAuth tokens — login required")
            return
        from gui_qt.collections_browser_view import CollectionsBrowserView
        view = CollectionsBrowserView(
            api, domain, game, log_fn=self._append_log,
            on_open_detail=self._open_collection_detail_tab)
        self._collections_view = view
        view.destroyed.connect(
            lambda *_: setattr(self, "_collections_view", None))
        self._tabs.open_tab(view, "Collections", key="collections")

    def _open_collection_detail_tab(self, collection, revision_number=None):
        """Open a collection's detail panel as a NEW detachable tab (the
        collections browser tab stays open). Card View passes no revision (latest);
        the browser's 'Open Current' passes the installed revision."""
        # Key by slug when the id is 0 (Open Current builds a bare NexusCollection).
        key = f"collection_detail_{collection.id or collection.slug}"
        if revision_number is not None:
            key = f"{key}_r{revision_number}"
        if self._tabs.has_key(key):
            self._tabs.focus_key(key)
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return
        domain = (getattr(game, "nexus_game_domain", "")
                  or getattr(collection, "game_domain", "") or "")
        api = self._ensure_nexus_api()
        if api is None:
            self._notify("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO.",
                         "warning")
            return
        from gui_qt.collection_detail_view import CollectionDetailView
        view = CollectionDetailView(
            api, collection, game, log_fn=self._append_log,
            revision_number=revision_number)
        view.set_install_handler(
            lambda chosen, skipped, intent="install": self._install_collection(
                collection, view, chosen, skipped, intent))
        title = f"Collection: {collection.name or collection.slug}"
        self._tabs.open_tab(view, title, key=key)

    # ---- Collection install (automatic / premium) ------------------------
    def _install_collection(self, collection, detail_view, chosen, skipped,
                            intent="install"):
        """Start an automatic (premium) collection install: gate on premium,
        create a new profile, then run the neutral orchestrator on a daemon
        thread with every callback marshaled through a Signal (UI-thread slots
        update the overlay). Deferred FOMOD/BAIN mods block the worker on a
        wizard via _col_fomod / _col_bain (same handshake as _make_exists_cb)."""
        import threading
        if self._col_install_running:
            self._notify("A collection install is already running.", "warning")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify("Log in first: Nexus ▸ Login to Nexus.", "warning")
            return
        dl_path = getattr(detail_view, "download_link_path", "") or ""
        revision_number = getattr(detail_view, "_revision_number", None)
        # Manifest rule: this collection must be installed as a NEW profile.
        recommend_new = bool(getattr(detail_view, "_recommend_new_profile", False))
        # Local-manifest import (Import profile): a parsed manifest to feed straight
        # into the orchestrator + a local .amethyst whose bundled mods/profile files
        # are extracted after install. Both empty for a normal Nexus collection.
        local_manifest = getattr(detail_view, "_local_manifest", None)
        bundle_zip = getattr(detail_view, "_bundle_zip_path", "") or ""
        mods = detail_view.install_mods(skipped)
        if not mods:
            self._notify("This collection has no installable mods.", "info")
            return
        slug = getattr(collection, "slug", "") or ""
        domain = (getattr(game, "nexus_game_domain", "")
                  or getattr(collection, "game_domain", "") or "")

        # Premium gate runs off-thread (validate() is rate-limited); on success it
        # creates the profile + starts the pipeline, all marshaled back to the UI.
        self._col_install_running = True
        self._notify("Checking Nexus account…", "info")

        def _premium_worker():
            try:
                user = api.validate()
                is_premium = bool(getattr(user, "is_premium", False))
            except Exception as exc:
                self._op_log.emit(f"[collection] premium check failed: {exc}")
                is_premium = False
            try:
                from Utils.ui_config import load_force_manual_install
                force_manual = bool(load_force_manual_install())
            except Exception:
                force_manual = False
            self._col_finished.emit(
                "_premium", {"ok": is_premium and not force_manual,
                             "collection": collection, "domain": domain,
                             "slug": slug, "dl_path": dl_path,
                             "revision": revision_number, "mods": mods,
                             "skipped": set(skipped), "game": game, "api": api,
                             "recommend_new": recommend_new, "intent": intent,
                             "local_manifest": local_manifest,
                             "bundle_zip": bundle_zip})

        threading.Thread(target=_premium_worker, daemon=True,
                         name="col-premium").start()

    def _choose_collection_mode(self, info):
        """UI thread: show the New/Append (or Continue) mode overlay, then start
        the pipeline with the chosen mode. Mirrors Tk _continue_install_collection:
        if this exact collection+revision URL is already in a profile → Continue;
        else → New/Append."""
        from gui.game_helpers import (
            find_profile_with_collection_url, _profiles_for_game)
        game = info["game"]; slug = info["slug"]; domain = info["domain"]
        rev = info["revision"]
        url = f"https://www.nexusmods.com/{domain}/collections/{slug}"
        if rev is not None:
            url += f"/revisions/{rev}"
        try:
            existing = find_profile_with_collection_url(game.name, url)
        except Exception:
            existing = None

        def _done(result):
            if result is None:
                self._col_install_running = False
                self._notify("Collection install cancelled.", "info")
                return
            info["mode_result"] = result
            self._start_collection_pipeline(info)

        if existing:
            from gui_qt.collection_mode_overlay import ContinueOverlay
            ContinueOverlay.show_over(self, existing, _done)
        else:
            from gui_qt.collection_mode_overlay import ModeOverlay
            try:
                profiles = _profiles_for_game(game.name)
            except Exception:
                profiles = []
            # Manifest rule (collectionConfig.recommendNewProfile) → disable Append.
            force_new = bool(info.get("recommend_new"))
            ModeOverlay.show_over(self, profiles, _done, force_new_profile=force_new)

    def _resume_collection_install(self, info):
        """UI thread: RESUME a paused install — clear the paused flag + registry,
        then continue into the profile that already claims this collection.
        Already-installed mods skip by file_id (Tk parity)."""
        game = info["game"]; slug = info["slug"]
        from gui.game_helpers import find_profile_with_collection_slug
        from Utils.profile_state import write_collection_install_paused
        try:
            pname = find_profile_with_collection_slug(game.name, slug)
        except Exception:
            pname = None
        if not pname:
            self._col_install_running = False
            self._notify("Could not find the paused profile.", "error")
            return
        profile_dir = game.get_profile_root() / "profiles" / pname
        try:
            write_collection_install_paused(profile_dir, False)
        except Exception:
            pass
        try:
            from gui_qt.collection_detail_view import _PAUSED_COLLECTIONS
            _PAUSED_COLLECTIONS.discard(slug)
        except Exception:
            pass
        self._refresh_open_collection_buttons()
        info["mode_result"] = ("continue", pname, False, False)
        self._start_collection_pipeline(info)

    def _run_collection_update(self, info):
        """UI thread: UPDATE an installed collection to the viewed revision.
        Port of Tk _on_update_collection + _apply_collection_update: compute the
        diff, confirm via UpdateOverlay, remove stale/bundled/patched mods, stash
        an order-preserving update_context, then continue-install."""
        game = info["game"]; slug = info["slug"]; mods = info["mods"]
        from gui.game_helpers import find_profile_with_collection_slug
        try:
            pname = find_profile_with_collection_slug(game.name, slug)
        except Exception:
            pname = None
        if not pname:
            self._col_install_running = False
            self._notify("Could not find the installed collection profile.", "error")
            return
        profile_dir = game.get_profile_root() / "profiles" / pname

        # Guard: the collection's profile must be the active one (cross-profile
        # removal is out of scope, matching Tk).
        active = getattr(game, "_active_profile_dir", None)
        if active is None or Path(active).resolve() != profile_dir.resolve():
            self._col_install_running = False
            self._notify(f"Switch to profile '{pname}' first, then Update.",
                         "warning")
            return

        # Compute the diff (old cached manifest vs the new mod list).
        import json as _json
        from Utils.modlist import read_modlist
        from Utils.collection_diff import diff_collection
        from Utils.profile_state import read_collection_revision
        old_manifest = {}
        try:
            mf = profile_dir / "collection.json"
            if mf.is_file():
                old_manifest = _json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            old_manifest = {}
        modlist_path = profile_dir / "modlist.txt"
        try:
            installed_names_lower = {
                e.name.lower() for e in read_modlist(modlist_path)
                if not e.is_separator} if modlist_path.is_file() else set()
        except Exception:
            installed_names_lower = set()
        try:
            staging_path = game.get_effective_mod_staging_path()
        except Exception:
            staging_path = None
        try:
            diff = diff_collection(
                old_manifest=old_manifest, new_mods=mods,
                staging_path=Path(staging_path) if staging_path else Path("."),
                installed_names_lower=installed_names_lower,
                collection_slug=slug)
        except Exception as exc:
            self._col_install_running = False
            self._notify(f"Could not compute update diff: {exc}", "error")
            return

        # Human labels for the update/add buckets via the new mod list.
        fid_to_name = {getattr(m, "file_id", 0): (getattr(m, "mod_name", "")
                       or f"file {getattr(m, 'file_id', 0)}") for m in mods}
        to_update_labels = [
            f"{old} → {fid_to_name.get(fid, fid)}"
            for old, fid in zip(diff.to_update_old, diff.to_update_new_fids)]
        to_add_labels = [fid_to_name.get(fid, str(fid))
                         for fid in diff.to_install_fids]

        from_rev = read_collection_revision(profile_dir)
        to_rev = info["revision"]

        def _done(apply_it):
            if not apply_it:
                self._col_install_running = False
                self._notify("Collection update cancelled.", "info")
                return
            self._apply_collection_update(info, profile_dir, pname, slug, diff)

        from gui_qt.collection_update_overlay import UpdateOverlay
        UpdateOverlay.show_over(
            self, profile_name=pname, from_rev=from_rev, to_rev=to_rev,
            to_remove=list(diff.to_remove), to_update=to_update_labels,
            to_add=to_add_labels, orphans=list(diff.orphans), on_done=_done)

    def _apply_collection_update(self, info, profile_dir, pname, slug, diff):
        """UI thread: user confirmed — snapshot the modlist, remove stale +
        bundled/patched mods, build update_context, then continue-install."""
        import configparser
        from Utils.modlist import read_modlist
        from Utils import mod_remove
        game = info["game"]
        try:
            snapshot = list(read_modlist(profile_dir / "modlist.txt")) \
                if (profile_dir / "modlist.txt").is_file() else []
        except Exception:
            snapshot = []

        # Bundled/patched mods for THIS collection are force-reinstalled (their
        # contents/patches may have changed) — port of Tk 1974-2001.
        slug_lower = (slug or "").strip().lower()
        bundled_or_patched: set[str] = set()
        try:
            staging_path = Path(game.get_effective_mod_staging_path())
        except Exception:
            staging_path = None
        if staging_path is not None and staging_path.is_dir() and slug_lower:
            for mod_dir in staging_path.iterdir():
                if not mod_dir.is_dir():
                    continue
                meta = mod_dir / "meta.ini"
                if not meta.is_file():
                    continue
                cp = configparser.ConfigParser()
                try:
                    cp.read(meta, encoding="utf-8")
                except Exception:
                    continue
                if not cp.has_section("General"):
                    continue
                if (cp["General"].get("fromCollection") or "").strip().lower() != slug_lower:
                    continue
                if (cp["General"].getboolean("fromCollectionBundled", fallback=False)
                        or cp["General"].getboolean("fromCollectionPatched", fallback=False)):
                    bundled_or_patched.add(mod_dir.name.lower())

        all_remove_lower = {n.lower() for n in diff.removals} | bundled_or_patched
        removed_lower: set[str] = set()
        if all_remove_lower:
            to_remove_names = [e.name for e in snapshot
                               if not e.is_separator
                               and e.name.lower() in all_remove_lower]
            if to_remove_names:
                try:
                    mod_remove.remove_mods(
                        game, profile_dir, to_remove_names,
                        log_fn=lambda m: self._append_log(str(m)))
                    removed_lower = {n.lower() for n in to_remove_names}
                except Exception as exc:
                    self._col_install_running = False
                    self._notify(f"Update failed during removal: {exc}", "error")
                    return

        filtered_snapshot = [
            e for e in snapshot
            if e.is_separator or e.name.lower() not in removed_lower]

        info["update_context"] = {"snapshot": filtered_snapshot, "schema_order": {}}
        info["mode_result"] = ("continue", pname, False, False)
        self._start_collection_pipeline(info)

    def _refresh_open_collection_buttons(self):
        """Refresh the Install/Update/Resume button on any open collection detail
        tab (after a pause/resume state change)."""
        try:
            from gui_qt.collection_detail_view import CollectionDetailView
            for view in self.findChildren(CollectionDetailView):
                try:
                    view._update_install_btn_state()
                except Exception:
                    pass
        except Exception:
            pass

    def _start_collection_pipeline(self, info):
        """UI thread: premium confirmed — create the profile, open the overlay,
        and launch the orchestrator on a daemon thread."""
        import threading
        game = info["game"]; api = info["api"]; collection = info["collection"]
        slug = info["slug"]; domain = info["domain"]
        revision_number = info["revision"]; mods = info["mods"]
        skipped = info["skipped"]; dl_path = info["dl_path"]
        self._col_install_slug = slug
        update_context = info.get("update_context")
        # Local-manifest import: feed the parsed manifest to the orchestrator (no
        # Nexus fetch) and remember the .amethyst so its bundled mods + profile
        # files are extracted once the Nexus mods finish installing.
        local_manifest = info.get("local_manifest")
        self._col_bundle_zip = info.get("bundle_zip") or ""

        # Resolve the install mode chosen in the mode overlay (default: new).
        mode_result = info.get("mode_result") or ("new", None, False, False)
        mode, append_profile_name, ov_existing, skip_existing = mode_result
        # Mods whose optional was unticked — the orchestrator removes these from
        # an existing profile (append/continue).
        skipped_mods = [m for m in mods if getattr(m, "file_id", None) in skipped]

        from gui.game_helpers import (
            _create_profile, _profiles_for_game, save_collection_url_to_profile)
        from Utils.profile_state import (
            write_collection_revision, write_collection_optional_skipped)
        import re as _re

        # overwrite_existing is None for new/continue (fresh modlist write); a bool
        # for append (preserves existing load order via _append_reconcile_modlist).
        overwrite_existing = None
        skip_existing_arg = False

        if mode == "new":
            raw = collection.name or slug or "Collection"
            base = _re.sub(r"[^\w\s\-]", "", raw).strip().replace(" ", "_") or "Collection"
            profile_name = (f"{base}_Rev{revision_number}"[:64]
                            if revision_number is not None else base[:64])
            _existing = set(_profiles_for_game(game.name))
            _pn = profile_name
            _i = 2
            while _pn in _existing:
                _pn = f"{profile_name}_{_i}"[:64]
                _i += 1
            profile_name = _pn
            try:
                profile_dir = Path(_create_profile(
                    game.name, profile_name, profile_specific_mods=True))
            except Exception as exc:
                self._col_install_running = False
                self._notify(f"Could not create profile: {exc}", "error")
                return
            # New/continue claim the collection: record URL + revision.
            self._stamp_collection_profile(
                profile_dir, domain, slug, revision_number, skipped)
        else:
            # append / continue → install into an existing profile.
            profile_dir = game.get_profile_root() / "profiles" / append_profile_name
            if not profile_dir.is_dir():
                self._col_install_running = False
                self._notify(f"Profile '{append_profile_name}' not found.", "error")
                return
            if mode == "append":
                overwrite_existing = bool(ov_existing)
                skip_existing_arg = bool(skip_existing)
                # Append doesn't claim the collection URL; still record skipped set.
                try:
                    write_collection_optional_skipped(profile_dir, skipped)
                except Exception:
                    pass
            else:  # continue
                self._stamp_collection_profile(
                    profile_dir, domain, slug, revision_number, skipped)

        old_profile_dir = getattr(game, "_active_profile_dir", None)

        # Overlay + control.
        from gui_qt.collection_install_overlay import CollectionInstallOverlay
        from Utils.collection_install import CollectionInstallControl
        control = CollectionInstallControl()
        self._col_install_control = control
        title = f"Installing collection: {collection.name or slug}"
        self._col_install_overlay = CollectionInstallOverlay.show_over(
            self, title, on_pause=self._on_col_pause_clicked,
            on_cancel=self._on_col_cancel_clicked)

        callbacks = self._build_collection_callbacks()

        def _worker():
            from Utils.collection_install import run_collection_install
            from Nexus.nexus_download import NexusDownloader
            from Utils.config_paths import get_download_cache_dir_for_game
            downloader = NexusDownloader(
                api, download_dir=get_download_cache_dir_for_game(game.name or ""))
            try:
                run_collection_install(
                    game=game, api=api, downloader=downloader, mods=mods,
                    download_link_path=dl_path, profile_dir=profile_dir,
                    old_profile_dir=old_profile_dir, collection_slug=slug,
                    revision_number=revision_number, skipped_fids=skipped,
                    skipped_mods=skipped_mods, overwrite_existing=overwrite_existing,
                    skip_existing=skip_existing_arg, update_context=update_context,
                    collection_schema_cache=local_manifest,
                    callbacks=callbacks, control=control)
            except Exception as exc:
                import traceback
                self._op_log.emit(f"[collection] install error: {exc}\n"
                                  f"{traceback.format_exc()}")
                self._col_finished.emit("cancelled", {"profile_dir": str(profile_dir)})

        threading.Thread(target=_worker, daemon=True, name="col-install").start()

    def _stamp_collection_profile(self, profile_dir, domain, slug, revision_number,
                                  skipped):
        """Record the collection URL + revision + skipped-optionals on a profile
        that claims this collection (new / continue modes)."""
        from gui.game_helpers import save_collection_url_to_profile
        from Utils.profile_state import (
            write_collection_revision, write_collection_optional_skipped)
        try:
            url = f"https://www.nexusmods.com/{domain}/collections/{slug}"
            if revision_number is not None:
                url += f"/revisions/{revision_number}"
            save_collection_url_to_profile(profile_dir, url)
        except Exception as exc:
            self._append_log(f"[collection] could not save URL: {exc}")
        try:
            if revision_number is not None:
                write_collection_revision(profile_dir, revision_number)
            if skipped:
                write_collection_optional_skipped(profile_dir, skipped)
        except Exception:
            pass

    def _build_collection_callbacks(self):
        """Build a CollectionInstallCallbacks whose every field is a single
        Signal.emit — NO callback touches a widget (all UI mutation happens in the
        connected UI-thread slots). Matches the thread-safety rule that cost a
        segfault before."""
        from Utils.collection_install import CollectionInstallCallbacks
        return CollectionInstallCallbacks(
            on_status=lambda m: self._col_status.emit(m),
            on_progress=lambda v: self._col_progress.emit(v),
            on_agg_download=lambda c, t, s: self._col_agg.emit(int(c), int(t), float(s)),
            on_dl_mod_start=lambda f, n, s: self._col_dl.emit("start", (f, n, s)),
            on_dl_mod_update=lambda f, c, t: self._col_dl.emit("update", (f, c, t)),
            on_dl_mod_finish=lambda f: self._col_dl.emit("finish", f),
            on_extract_queue=lambda f, n: self._col_extract.emit("queue", (f, n)),
            on_extract_add=lambda f, n: self._col_extract.emit("add", (f, n)),
            on_extract_remove=lambda f: self._col_extract.emit("remove", f),
            on_row_installed=lambda f: self._col_row.emit(int(f)),
            on_log=lambda m: self._op_log.emit(str(m)),
            on_done=lambda i, s, t, p: self._col_finished.emit("done", (i, s, t, p)),
            on_paused=lambda i, p: self._col_finished.emit("paused", (i, p)),
            on_cancelled=lambda pd: self._col_finished.emit("cancelled", {"profile_dir": str(pd)}),
            resolve_fomod=self._make_col_fomod_cb(),
            resolve_bain=self._make_col_bain_cb(),
        )

    # ---- collection progress slots (UI thread) ---------------------------
    def _on_col_status(self, text):
        if self._col_install_overlay is not None:
            self._col_install_overlay.set_status(text)

    def _on_col_progress(self, value):
        pass   # overlay drives from the aggregate bytes; per-mod count unused here

    def _on_col_agg(self, cur, tot, mbps):
        if self._col_install_overlay is not None:
            self._col_install_overlay.set_agg(cur, tot, mbps)

    def _on_col_dl(self, verb, payload):
        ov = self._col_install_overlay
        if ov is None:
            return
        if verb == "start":
            ov.dl_start(*payload)
        elif verb == "update":
            ov.dl_update(*payload)
        elif verb == "finish":
            ov.dl_finish(payload)

    def _on_col_extract(self, verb, payload):
        ov = self._col_install_overlay
        if ov is None:
            return
        if verb == "queue":
            ov.extract_queue(*payload)
        elif verb == "add":
            ov.extract_add(*payload)
        elif verb == "remove":
            ov.extract_remove(payload)

    def _on_col_row(self, file_id):
        if self._col_install_overlay is not None:
            self._col_install_overlay.row_installed(file_id)

    # ---- deferred FOMOD / BAIN blocking handshake ------------------------
    def _make_col_fomod_cb(self):
        """resolve_fomod for the orchestrator: runs on the WORKER thread, asks the
        UI to open the FOMOD wizard, and BLOCKS until the user Finishes/Cancels."""
        import threading

        def _cb(config, base, name, installed, active, loose, saved=None):
            holder = {"result": None}
            ev = threading.Event()
            self._col_fomod.emit({"config": config, "base": base, "name": name,
                                  "installed": installed, "active": active,
                                  "loose": loose, "saved": saved,
                                  "holder": holder, "event": ev})
            ev.wait()
            return holder["result"]

        return _cb

    def _on_col_fomod_ui(self, payload):
        """UI thread: open the FOMOD wizard as a tab; unblock the worker on
        finish/cancel/close. Only ONE deferred wizard is ever open (the deferred
        loop is sequential) so there's no nesting."""
        from gui_qt.fomod_wizard_view import FomodWizardView
        holder, ev = payload["holder"], payload["event"]
        done = {"v": False}
        # Hide the install overlay while the wizard is up — it covers the tab
        # and would otherwise block the deferred FOMOD picker (no work is running
        # while we wait for the user, so nothing to display).
        self._hide_col_overlay()

        def _finish_ev(result):
            if done["v"]:
                return
            done["v"] = True
            holder["result"] = result
            self._tabs.close_tab("col_fomod_wizard")
            self._show_col_overlay()
            ev.set()

        _sel_path = None
        _game_name = getattr(getattr(self._gs, "game", None), "name", "")
        if _game_name:
            from Utils.config_paths import get_fomod_selections_path
            _sel_path = get_fomod_selections_path(_game_name, payload["name"])
        view = FomodWizardView(payload["config"], payload["base"], payload["name"],
                               on_finish=lambda sel: _finish_ev(sel),
                               on_cancel=lambda: _finish_ev(None),
                               saved_selections=payload.get("saved"),
                               selections_path=_sel_path,
                               installed_files=payload.get("installed"),
                               active_files=payload.get("active"),
                               loose_files=payload.get("loose"))
        # Closing the tab (or a stop request) counts as cancel so we never hang.
        view.destroyed.connect(lambda *_: _finish_ev(None))
        self._tabs.open_tab(view, f"Install: {payload['name']}",
                            key="col_fomod_wizard")

    def _make_col_bain_cb(self):
        import threading

        def _cb(subpkgs, root, name):
            holder = {"result": None}
            ev = threading.Event()
            self._col_bain.emit({"subpkgs": subpkgs, "root": root, "name": name,
                                 "holder": holder, "event": ev})
            ev.wait()
            return holder["result"]

        return _cb

    def _on_col_bain_ui(self, payload):
        from gui_qt.bain_picker_view import BainPickerView
        holder, ev = payload["holder"], payload["event"]
        done = {"v": False}
        # Hide the install overlay while the BAIN picker is up (see FOMOD above).
        self._hide_col_overlay()

        def _finish_ev(result):
            if done["v"]:
                return
            done["v"] = True
            holder["result"] = result
            self._tabs.close_tab("col_bain_picker")
            self._show_col_overlay()
            ev.set()

        view = BainPickerView(payload["subpkgs"], payload["root"], payload["name"],
                              on_done=lambda r: _finish_ev(r))
        view.destroyed.connect(lambda *_: _finish_ev(None))
        self._tabs.open_tab(view, f"Install: {payload['name']}",
                            key="col_bain_picker")

    def _hide_col_overlay(self):
        """Hide the collection install overlay (e.g. while a deferred FOMOD/BAIN
        wizard tab is up so it isn't covered)."""
        ov = self._col_install_overlay
        if ov is not None:
            try:
                ov.hide()
            except Exception:
                pass

    def _show_col_overlay(self):
        """Re-show the collection install overlay after a deferred wizard closes
        (no-op if the install already finished + dismissed the overlay)."""
        ov = self._col_install_overlay
        if ov is not None:
            try:
                ov.show()
                ov.raise_()
            except Exception:
                pass

    # ---- pause / cancel --------------------------------------------------
    def _on_col_pause_clicked(self):
        ctl = self._col_install_control
        if ctl is not None:
            ctl.pause.set()
            ctl.stop.set()

    def _on_col_cancel_clicked(self):
        from gui_qt.confirm_overlay import ConfirmOverlay

        def _done(confirmed):
            if not confirmed:
                return
            ctl = self._col_install_control
            if ctl is not None:
                ctl.cancel.set()
                ctl.pause.set()
                ctl.stop.set()
            if self._col_install_overlay is not None:
                self._col_install_overlay.set_status("Cancelling…")

        ConfirmOverlay.show_over(
            self, "Cancel install?",
            "This will stop the install and delete the collection profile.",
            _done, confirm_label="Cancel Install", cancel_label="Keep Going")

    # ---- completion ------------------------------------------------------
    def _on_col_finished(self, kind, payload):
        """UI thread: handle the premium gate result AND the terminal states."""
        if kind == "_premium":
            if not payload.get("ok"):
                self._col_install_running = False
                self._notify("Automatic collection install requires Nexus Premium "
                             "(manual install coming later).", "warning")
                return
            # Premium confirmed — route by intent.
            intent = payload.get("intent", "install")
            if intent == "update":
                self._run_collection_update(payload)
            elif intent == "resume":
                self._resume_collection_install(payload)
            else:
                # Ask the user how to install (New/Append/Continue) BEFORE
                # creating any profile.
                self._choose_collection_mode(payload)
            return

        # Terminal states.
        self._col_install_running = False
        self._col_install_control = None
        ov = self._col_install_overlay

        if kind == "cancelled":
            import threading
            pd = payload.get("profile_dir") if isinstance(payload, dict) else None
            if ov is not None:
                ov.set_status("Cancelling…")
            game = self._gs.game

            def _cleanup_worker():
                from Utils.collection_install import cleanup_cancelled_install
                from Utils.ui_config import load_clear_archive_after_install
                try:
                    cleanup_cancelled_install(
                        game, Path(pd) if pd else None,
                        clear_cache=bool(load_clear_archive_after_install()),
                        log_fn=lambda m: self._op_log.emit(str(m)))
                except Exception as exc:
                    self._op_log.emit(f"[collection] cancel cleanup failed: {exc}")
                self._col_finished.emit("_cancel_cleaned", None)

            threading.Thread(target=_cleanup_worker, daemon=True,
                             name="col-cancel-cleanup").start()
            return

        if kind == "_cancel_cleaned":
            if ov is not None:
                ov.dismiss()
                self._col_install_overlay = None
            # Switch back to the default profile + reload.
            try:
                profs = self._gs.profiles()
                if profs:
                    self._on_profile_changed(profs[0])
            except Exception:
                pass
            self._notify("Collection install cancelled.", "info")
            return

        if kind == "paused":
            installed, profile_name = payload
            if ov is not None:
                ov.finish(f"Paused — {installed} installed.")
                QTimer.singleShot(1500, self._dismiss_col_overlay)
            self._select_installed_collection_profile(profile_name)
            # Register the paused slug + refresh any open detail view so its
            # button flips to "Resume" (Tk parity — _PAUSED_INSTALLS).
            slug = getattr(self, "_col_install_slug", "") or ""
            if slug:
                try:
                    from gui_qt.collection_detail_view import _PAUSED_COLLECTIONS
                    _PAUSED_COLLECTIONS.add(slug)
                except Exception:
                    pass
            self._refresh_open_collection_buttons()
            self._notify(f"Install paused — {installed} mod(s) installed.", "info")
            return

        # done
        installed, skipped_n, total, profile_name = payload
        # Local-manifest import: extract bundled mods + profile files from the
        # .amethyst over the freshly-installed profile, then reload. Handled on a
        # worker (unzipping can be sizeable) → reload marshaled back via a Signal.
        bundle_zip = getattr(self, "_col_bundle_zip", "") or ""
        self._col_bundle_zip = ""
        if bundle_zip and Path(bundle_zip).is_file():
            self._finish_import_bundle(bundle_zip, profile_name, ov,
                                       installed, total, skipped_n)
            return
        if ov is not None:
            ov.finish(f"Done — {installed}/{total} installed.")
            QTimer.singleShot(1500, self._dismiss_col_overlay)
        self._select_installed_collection_profile(profile_name)
        msg = f"Collection installed — {installed}/{total} mod(s)"
        self._notify(msg + (f" ({skipped_n} skipped)" if skipped_n else ""), "success")

    # ---- Import profile: local-bundle extraction -------------------------
    def _finish_import_bundle(self, bundle_zip, profile_name, ov,
                              installed, total, skipped_n):
        """Extract a local .amethyst's bundled mods + profile files into the
        just-installed profile on a worker, then reload on the UI thread."""
        import threading
        game = self._gs.game
        if game is None:
            self._select_installed_collection_profile(profile_name)
            return
        profile_dir = game.get_profile_root() / "profiles" / profile_name
        if ov is not None:
            ov.set_status("Restoring bundled mods + profile files…")

        def _worker():
            try:
                # Resolve the imported profile's own mods/overwrite dirs (it's a
                # profile_specific_mods profile → <profile_dir>/mods + /overwrite).
                # Set it active first so get_effective_*_path resolves to it.
                prev = getattr(game, "_active_profile_dir", None)
                try:
                    game.set_active_profile_dir(profile_dir)
                    game.load_paths()
                    mods_dir = game.get_effective_mod_staging_path()
                    overwrite_dir = game.get_effective_overwrite_path()
                finally:
                    game.set_active_profile_dir(prev)
                    game.load_paths()
                from Utils import profile_export
                profile_export.install_local_bundle(
                    bundle_zip, profile_dir, mods_dir, overwrite_dir,
                    log_fn=lambda m: self._op_log.emit(str(m)))
            except Exception as exc:
                self._op_log.emit(f"[import] bundle extraction failed: {exc}")
            self._col_import_done.emit(
                (profile_name, int(installed), int(total), int(skipped_n)))

        threading.Thread(target=_worker, daemon=True, name="import-bundle").start()

    def _on_import_bundle_done(self, payload):
        profile_name, installed, total, skipped_n = payload
        ov = self._col_install_overlay
        if ov is not None:
            ov.finish(f"Imported — {installed}/{total} installed.")
            QTimer.singleShot(1500, self._dismiss_col_overlay)
        # Rebuild the mod index for the imported bundle mods, then reload.
        self._select_installed_collection_profile(profile_name, rescan_index=True)
        msg = f"Profile imported — {installed}/{total} mod(s)"
        self._notify(msg + (f" ({skipped_n} skipped)" if skipped_n else ""),
                     "success")

    def _dismiss_col_overlay(self):
        if self._col_install_overlay is not None:
            self._col_install_overlay.dismiss()
            self._col_install_overlay = None

    def _select_installed_collection_profile(self, profile_name, rescan_index=False):
        """Switch the profile selector to the freshly-installed collection profile
        and reload the panels + Open-Current. *rescan_index* forces a full mod-index
        rebuild (needed after an import extracts bundle mods straight to disk)."""
        try:
            profs = self._gs.profiles()
            self._profile_selector.set_items(profs, current=profile_name)
            self._gs.set_profile(profile_name)
            self._profile_selector.set_current(profile_name)
        except Exception as exc:
            self._append_log(f"[collection] profile switch failed: {exc}")
        self._reload_modlist(rescan_index=rescan_index)
        self._reload_plugins()
        try:
            self._update_deployed_profile_highlight()
        except Exception:
            pass
        cv = getattr(self, "_collections_view", None)
        if cv is not None:
            cv.refresh_open_current()

    # ---- Collections ▸ Reset load order ----------------------------------
    def _reset_collection_load_order(self):
        """Re-apply the active collection profile's intended load order from its
        manifest. No-op (with a toast) unless the active profile is a collection
        profile. Runs the file rewrites on a worker → toast + modlist reload."""
        if self._reset_running:
            self._notify("A load-order reset is already running.", "warning")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return
        pdir = self._gs.profile_dir()
        from gui.game_helpers import get_collection_url_from_profile
        url = get_collection_url_from_profile(pdir) if pdir is not None else None
        if not url:
            self._notify("The active profile isn't a collection profile.",
                         "warning")
            return

        self._reset_running = True
        self._notify("Resetting collection load order…", "info")
        domain = getattr(game, "nexus_game_domain", "") or ""
        game_name = getattr(game, "name", "") or ""
        # slug from the stored URL: …/collections/<slug>[/revisions/N]
        slug = ""
        try:
            after = url.split("/collections/", 1)[1]
            slug = after.split("/", 1)[0]
        except Exception:
            slug = ""

        import threading, json

        def worker():
            res = {"error": "unknown"}
            try:
                from Utils.collection_manifest import load_collection_manifest
                from Utils.collection_reset import reset_collection_load_order
                # Prefer the profile's already-saved collection.json (offline).
                manifest = {}
                saved = pdir / "collection.json"
                if saved.is_file():
                    try:
                        manifest = json.loads(saved.read_text(encoding="utf-8"))
                    except Exception:
                        manifest = {}
                if not manifest and slug:
                    api = self._ensure_nexus_api()
                    if api is not None:
                        (_n, _s, _c, _mods, dl_path,
                         revs) = api.get_collection_detail(slug, domain)
                        rev = None
                        try:
                            pub = [int(r.get("revisionNumber") or 0)
                                   for r in (revs or [])
                                   if (r.get("revisionStatus") or "").lower()
                                   == "published"]
                            rev = max(pub) if pub else None
                        except Exception:
                            rev = None
                        manifest = load_collection_manifest(
                            api, game_name, slug, rev, dl_path,
                            log_fn=lambda m: self._op_log.emit(str(m)))
                if not manifest:
                    res = {"error": "no_manifest"}
                else:
                    res = reset_collection_load_order(
                        pdir, manifest,
                        log_fn=lambda m: self._op_log.emit(str(m)))
            except Exception as exc:
                self._op_log.emit(f"Reset load order failed: {exc}")
                res = {"error": str(exc)}
            self._reset_done.emit(res)

        threading.Thread(target=worker, daemon=True,
                         name="collection-reset").start()

    def _on_reset_done(self, res):
        self._reset_running = False
        if not isinstance(res, dict) or res.get("error"):
            reason = (res or {}).get("error", "unknown") if isinstance(res, dict) else "unknown"
            self._notify(f"Load order reset failed: {reason}", "warning")
            return
        self._notify(
            f"Load order reset — {res.get('ordered', 0)} mods ordered"
            + (f", {res['unordered']} at top."
               if res.get("unordered") else "."), "info")
        self._reload_modlist()

    # ---- Nexus login (header menu ▸ Login to Nexus) ------------------------
    # Thin wiring over the toolkit-neutral OAuth/NXM backend in src/Nexus/,
    # mirroring the Tk gui/nexus_settings_dialog.py. The OAuth client fires its
    # callbacks from a background thread, so they only emit `_oauth_event`
    # (queued → UI thread) and never touch widgets directly.

    def _nexus_login_sso(self):
        """Start the browser OAuth flow. Keeps the client on self so the
        'Paste login code' fallback can complete the same session."""
        from Nexus.nexus_oauth import NexusOAuthClient, CLIENT_ID
        if not CLIENT_ID:
            self._notify("Nexus login is unavailable in this build.", "warning")
            return
        if self._oauth_client is not None and self._oauth_client.is_running:
            self._notify("A Nexus login is already in progress.", "info")
            return
        self._oauth_client = NexusOAuthClient(
            on_token=lambda t: self._oauth_event.emit("token", t),
            on_error=lambda m: self._oauth_event.emit("error", m),
            on_status=lambda m: self._oauth_event.emit("status", m),
        )
        self._oauth_client.start()
        self._notify("Opening browser to log in to Nexus Mods…", "info")

    def _nexus_paste_code(self):
        """Fallback when the localhost redirect was blocked: paste the Base64
        code from the Nexus 'Having issues?' page into the live login session."""
        if self._oauth_client is None or not self._oauth_client.is_running:
            self._notify("Start 'Login via SSO' first, then paste the code.",
                         "warning")
            return
        from PySide6.QtWidgets import QInputDialog
        blob, ok = QInputDialog.getText(
            self, "Paste Nexus login code",
            "Paste the code from the Nexus 'Having issues?' page:")
        if not ok or not blob.strip():
            return
        ok2, msg = self._oauth_client.submit_manual_code(blob)
        self._notify(msg, "info" if ok2 else "warning")

    def _nexus_clear_credentials(self):
        """Forget the saved OAuth tokens + legacy API key."""
        from Nexus.nexus_oauth import clear_oauth_tokens
        from Nexus.nexus_api import clear_api_key
        clear_api_key()
        clear_oauth_tokens()
        self._nexus_api = None
        if hasattr(self, "_nexus_footer"):
            self._nexus_footer.set_username(None)
        self._notify("Nexus credentials cleared.", "warning")
        self._append_log("[nexus] credentials cleared")

    def _nxm_menu_label(self) -> str:
        """Label for the NXM toggle, reflecting the handler's state at build
        time (the menu is built once at startup)."""
        from Nexus.nxm_handler import NxmHandler
        try:
            registered = NxmHandler.is_registered()
        except Exception:
            registered = False
        return "Unregister NXM handler" if registered else "Register NXM handler"

    def _nexus_toggle_nxm(self):
        """Register or unregister the nxm:// protocol handler (for Nexus
        'Download with Manager' links). Toasts the resulting state."""
        from Nexus.nxm_handler import NxmHandler
        try:
            if NxmHandler.is_registered():
                NxmHandler.unregister()
                self._notify("NXM handler unregistered.", "warning")
                self._append_log("[nexus] NXM handler unregistered")
            elif NxmHandler.register():
                self._notify("NXM handler registered.", "info")
                self._append_log("[nexus] NXM handler registered")
            else:
                self._notify("Failed to register — xdg-mime not found?", "error")
        except Exception as exc:
            self._notify(f"NXM handler error: {exc}", "error")

    def _on_oauth_event(self, kind: str, payload):
        """OAuth client callbacks marshalled onto the UI thread."""
        if kind == "status":
            self._append_log(f"[nexus] {payload}")
        elif kind == "error":
            self._oauth_client = None
            self._notify(f"Nexus login failed: {payload}", "error")
        elif kind == "token":
            # Tokens are already persisted by the client before this fires.
            self._oauth_client = None
            self._nexus_api = None
            self._ensure_nexus_api()   # rebuild api + kick the validate() worker
            self._notify("Logged in to Nexus Mods.", "info")
            self._append_log("[nexus] OAuth login complete")

    # ---- Check for updates -------------------------------------------------
    # Reuses the toolkit-neutral Nexus.nexus_update_checker.check_for_updates,
    # which writes has_update / missing_requirements back to each mod's meta.ini
    # (save_results=True). The modlist re-reads those on reload and paints the
    # update + missing-requirement badges, so the handler just runs the check on
    # a worker thread and reloads.

    def _on_check_updates(self, names=None):
        """Check Nexus for mod updates + missing requirements. *names* limits the
        check to a set of mod folder names (right-click subset); None = all."""
        if self._updates_running:
            self._notify("An update check is already running.", "info")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return
        domain = getattr(game, "nexus_game_domain", "") or ""
        if not domain:
            self._notify(f"'{game.name}' has no Nexus Mods page.", "warning")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO.",
                         "warning")
            return
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify("No mod staging folder for this profile.", "warning")
            return

        # Normalise the subset to a set (or None for "all").
        subset = set(names) if names else None
        self._updates_running = True
        btn = getattr(self, "_check_updates_btn", None)
        if btn is not None:
            btn.setEnabled(False)
            btn.setText("Checking…")
        n = len(subset) if subset else "all"
        self._notify(f"Checking Nexus for updates ({n})…", "info")

        import threading
        from Nexus.nexus_update_checker import check_for_updates

        def _worker():
            try:
                result = check_for_updates(
                    api, staging, game_domain=domain, save_results=True,
                    enabled_only=subset,
                    progress_cb=lambda m: self._op_log.emit(f"[nexus] {m}"),
                )
            except Exception as exc:
                self._append_log(f"[nexus] update check failed: {exc}")
                self._updates_ready.emit(None)
                return
            self._updates_ready.emit(result)

        threading.Thread(target=_worker, daemon=True, name="check-updates").start()

    def _on_updates_ready(self, result):
        """Worker finished — re-enable the button, summarise, refresh the rows."""
        self._updates_running = False
        btn = getattr(self, "_check_updates_btn", None)
        if btn is not None:
            btn.setEnabled(True)
            btn.setText("Check Updates")
        if result is None:
            self._notify("Update check failed — see the log.", "error")
            return
        updates, missing = result
        if not updates and not missing:
            self._notify("Nexus: all mods are up to date.", "info")
        else:
            parts = []
            if updates:
                parts.append(f"{len(updates)} update"
                             f"{'s' if len(updates) != 1 else ''}")
            if missing:
                parts.append(f"{len(missing)} missing requirement"
                             f"{'s' if len(missing) != 1 else ''}")
            self._notify("Nexus: " + ", ".join(parts) + ".", "warning")
        # Re-read meta.ini (now updated on disk) → repaint flags + refresh filters.
        self._reload_modlist()

    # ---- Quick Update -----------------------------------------------------

    def _quick_update_mods(self, mod_names):
        """Auto-install the latest name-matched version for each update-flagged
        mod (Tk parity, gui/modlist_nexus_actions._quick_update_mods). Mods whose
        latest file isn't a name match are skipped — the user updates those via
        Change Version. Three phases, all off the UI thread: resolve the target
        file_ids (parallel) → download them (parallel, premium-gated) → install
        the whole batch via _install_paths with the folder name forced (silent
        Replace-All)."""
        if getattr(self, "_quick_updating", False):
            self._notify("A Quick Update is already running.", "info")
            return
        if getattr(self, "_install_running", False):
            self._notify("An install is already in progress.", "warning")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO.",
                         "warning")
            return
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify("No mod staging folder for this profile.", "warning")
            return
        targets = list(mod_names or [])
        if not targets:
            self._notify("No mods with a pending update to quick-update.", "info")
            return

        self._quick_updating = True
        domain = getattr(game, "nexus_game_domain", "") or ""
        self._notify(f"Quick Update — checking {len(targets)} mod(s)…", "info")
        self._append_log(f"[nexus] Quick Update — checking {len(targets)} mod(s)…")

        import threading

        def _resolve_worker():
            import concurrent.futures as _cf
            from Utils.quick_update import resolve_quick_update_target
            queue = []
            skipped = []   # (mod_name, reason)

            def _one(nm):
                return resolve_quick_update_target(api, staging, nm, domain)

            with _cf.ThreadPoolExecutor(max_workers=4) as pool:
                for nm, (status, payload) in zip(targets, pool.map(_one, targets)):
                    if status == "queued":
                        queue.append(payload)
                    else:
                        skipped.append((nm, payload))
                        self._op_log.emit(f"[nexus] {nm} — {payload}, skipped.")
            self._qu_resolved.emit(queue, skipped)

        threading.Thread(target=_resolve_worker, daemon=True,
                         name="quick-update-resolve").start()

    def _on_qu_resolved(self, queue, skipped):
        """UI thread: resolve finished. Nothing to update → summarise. Otherwise
        gate on premium and download every resolved file in parallel."""
        if not queue:
            self._qu_finish(0, [], skipped)
            return
        api = self._ensure_nexus_api()
        game = self._gs.game
        is_premium = False
        try:
            is_premium = bool(api.validate().is_premium)
        except Exception:
            pass
        if not is_premium:
            self._append_log("[nexus] Premium required for Quick Update direct "
                             "downloads.")
            self._qu_finish(
                0, [(it[0], "Premium required for direct download") for it in queue],
                skipped)
            return

        self._append_log(f"[nexus] Quick Update — downloading {len(queue)} mod(s)…")
        self._notify(f"Quick Update — downloading {len(queue)} mod(s)…", "info")

        from Utils.ui_config import load_collection_settings
        try:
            dl_workers = max(1, int(load_collection_settings().get("max_concurrent", 3)))
        except Exception:
            dl_workers = 3

        import threading

        def _download_all():
            import concurrent.futures as _cf
            from Nexus.nexus_download import NexusDownloader
            from Nexus.nexus_meta import build_meta_from_download
            from Utils.config_paths import get_download_cache_dir_for_game
            dest = get_download_cache_dir_for_game(getattr(game, "name", "") or "")
            downloader = NexusDownloader(api, download_dir=dest)
            dl_items = []          # (mod_name, archive_path, prebuilt_meta)
            failed = []            # (mod_name, reason)
            lock = threading.Lock()

            def _one(item):
                mod_name, game_domain, meta, file_id, file_info = item
                try:
                    try:
                        mod_info = api.get_mod(game_domain, meta.mod_id)
                    except Exception as exc:
                        with lock:
                            failed.append((mod_name, f"could not fetch mod info ({exc})"))
                        return
                    size = 0
                    if file_info is not None:
                        size = (getattr(file_info, "size_in_bytes", None)
                                or (getattr(file_info, "size_kb", 0) or 0) * 1024 or 0)
                    result = downloader.download_file(
                        game_domain=game_domain, mod_id=meta.mod_id,
                        file_id=file_id, dest_dir=dest,
                        known_file_name=getattr(file_info, "file_name", "") or "",
                        expected_size_bytes=int(size),
                        progress_cb=lambda d, t: None)
                    if not (result.success and result.file_path):
                        with lock:
                            failed.append((mod_name,
                                           f"download failed — {result.error}"))
                        return
                    try:
                        prebuilt = build_meta_from_download(
                            game_domain=game_domain, mod_id=meta.mod_id,
                            file_id=file_id, archive_name=result.file_name,
                            mod_info=mod_info, file_info=file_info)
                        prebuilt.has_update = False
                    except Exception as exc:
                        self._op_log.emit(
                            f"[nexus] Warning — could not build metadata: {exc}")
                        prebuilt = None
                    with lock:
                        dl_items.append((mod_name, str(result.file_path), prebuilt))
                except Exception as exc:
                    with lock:
                        failed.append((mod_name, f"download error ({exc})"))

            with _cf.ThreadPoolExecutor(max_workers=dl_workers) as pool:
                list(pool.map(_one, queue))
            self._qu_skipped = skipped   # carried to _qu_finish via the installer
            self._qu_downloaded.emit(dl_items, failed)

        threading.Thread(target=_download_all, daemon=True,
                         name="quick-update-dl").start()

    def _on_qu_downloaded(self, dl_items, failed):
        """UI thread: every download finished. Install the batch via _install_paths
        with the folder name forced per archive (silent Replace-All), then finish."""
        skipped = getattr(self, "_qu_skipped", [])
        if not dl_items:
            self._qu_finish(0, failed, skipped)
            return
        paths = [p for _n, p, _m in dl_items]
        metas = {p: m for _n, p, m in dl_items if m is not None}
        preferred = {p: n for n, p, _m in dl_items}
        expected = len(dl_items)

        def _done(ok, total, names):
            # ok/total here are the archive install results; combine with the
            # download failures + resolve skips for the batch summary.
            more_failed = list(failed)
            if ok < expected:
                more_failed.append(
                    (f"{expected - ok} mod(s)", "install failed — see log"))
            self._qu_finish(ok, more_failed, skipped)

        self._install_paths(paths, metas=metas, preferred_names=preferred,
                            on_all_done=_done)

    def _qu_finish(self, updated, failed, skipped):
        """Log the batch summary + toast; warn about mods that couldn't be
        quick-updated. Re-checks flags happen via _install_paths' _reload_modlist;
        the resolve/download-only paths (nothing installed) reload here."""
        self._quick_updating = False
        self._qu_skipped = []
        if updated == 0:
            # No install ran → refresh flags ourselves (installed path already did).
            self._reload_modlist()
        self._append_log(
            f"[nexus] Quick Update — {updated} updated, "
            f"{len(skipped)} skipped (no name match), {len(failed)} failed.")
        for name, reason in failed:
            self._append_log(f"[nexus] Quick Update — {name}: {reason}")
        if updated:
            self._notify(f"Quick Update: updated {updated} mod(s)", "success")
        problems = len(skipped) + len(failed)
        if not problems:
            if not updated:
                self._notify("Quick Update: nothing to update.", "info")
            return
        parts = []
        if skipped:
            parts.append(f"{len(skipped)} had no name-matched file")
        if failed:
            parts.append(f"{len(failed)} failed to download or install")
        self._notify(
            f"Quick Update: {problems} mod(s) couldn't be updated — "
            + " and ".join(parts) + ". See the log; use Change Version manually.",
            "warning")

    # ---- Restore backup (plugins-panel-scoped overlay) --------------------

    def _open_restore_backup_tab(self):
        """Open the Restore backup list for the current profile as a tab that
        takes over the whole plugins panel. Triggered by the footer button."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return
        pname = self._gs.profile
        if not pname:
            self._notify("No profile selected.", "warning")
            return
        if self._tabs.has_key("restore_backup"):
            self._tabs.focus_key("restore_backup")
            return
        profile_dir = game.get_profile_root() / "profiles" / pname
        from gui_qt.backup_restore_view import BackupRestoreView
        view = BackupRestoreView(
            profile_dir, pname,
            on_restored=self._on_backup_restored,
            on_close=self._close_restore_backup_tab,
            log_fn=self._append_log)
        self._tabs.open_scoped_tab(
            view, "Restore backup", self._plugins_panel_stack,
            key="restore_backup")

    def _on_backup_restored(self):
        """A backup was restored — refresh both panels (backups cover modlist
        AND plugins)."""
        self._reload_modlist()
        self._reload_plugins()

    def _close_restore_backup_tab(self):
        if self._tabs.has_key("restore_backup"):
            self._tabs.close_tab("restore_backup")

    # ---- Change Version (plugins-panel-scoped overlay) --------------------

    def _open_change_version_tab(self, mod_name: str):
        """Open the Change Version picker for *mod_name* as a tab that takes over
        the whole plugins panel. Triggered by the update-flag click + the
        right-click 'Change Version' item."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return
        domain = getattr(game, "nexus_game_domain", "") or ""
        if not domain:
            self._notify(f"'{game.name}' has no Nexus Mods page.", "warning")
            return
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify("No mod staging folder for this profile.", "warning")
            return
        from Nexus.nexus_meta import read_meta
        meta = read_meta(staging / mod_name / "meta.ini")
        if int(getattr(meta, "mod_id", 0) or 0) <= 0:
            self._notify(f"'{mod_name}' isn't a Nexus mod.", "warning")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO.",
                         "warning")
            return

        # Reuse one overlay: rebuild it for the new mod if already open.
        if self._tabs.has_key("change_version"):
            self._tabs.close_tab("change_version")
        from gui_qt.change_version_view import ChangeVersionView
        view = ChangeVersionView(
            api, game, mod_name, meta,
            install_fn=self._install_paths,
            on_close=self._close_change_version_tab,
            log_fn=self._append_log)
        self._change_version_view = view
        view.destroyed.connect(
            lambda *_: setattr(self, "_change_version_view", None))
        self._tabs.open_scoped_tab(
            view, "Change Version", self._plugins_panel_stack,
            key="change_version")

    def _close_change_version_tab(self):
        """Close the Change Version overlay + refresh modlist flags (an Ignore-
        Update toggle or an install may have changed meta.ini)."""
        if self._tabs.has_key("change_version"):
            self._tabs.close_tab("change_version")
        self._reload_modlist()

    # ---- Separator settings (plugins-panel-scoped overlay) ----------------

    def _open_sep_settings_tab(self, sep_name, current_color, current_deploy):
        """Open the Separator Settings picker (colour + deploy override) as a tab
        over the plugins panel. Triggered by the right-click 'Separator settings…'
        item. *sep_name* is the internal `..._separator` name (the storage key)."""
        from gui_qt.separator_settings_view import SeparatorSettingsView
        if self._tabs.has_key("sep_settings"):
            self._tabs.close_tab("sep_settings")
        view = SeparatorSettingsView(
            sep_name, current_color, current_deploy,
            on_save=lambda color, deploy: self._save_sep_settings(
                sep_name, color, deploy),
            on_close=self._close_sep_settings_tab)
        self._tabs.open_scoped_tab(
            view, "Separator Settings", self._plugins_panel_stack,
            key="sep_settings")

    def _save_sep_settings(self, sep_name, color, deploy):
        """Persist a separator's colour + deploy override, repaint the modlist,
        and rebuild the filemap (a deploy-path change reroutes its mods)."""
        profile_dir = self._gs.profile_dir()
        # Colour — model drives the repaint; profile_state persists it.
        self._modlist_model.set_sep_color(sep_name, color)
        if profile_dir is not None:
            try:
                from Utils.profile_state import (
                    read_separator_colors, write_separator_colors,
                    read_separator_deploy_paths, write_separator_deploy_paths)
                colors = read_separator_colors(profile_dir)
                if color:
                    colors[sep_name] = color
                else:
                    colors.pop(sep_name, None)
                write_separator_colors(profile_dir, colors)

                paths = read_separator_deploy_paths(profile_dir)
                if deploy:
                    paths[sep_name] = deploy
                else:
                    paths.pop(sep_name, None)
                write_separator_deploy_paths(profile_dir, paths)
            except Exception as exc:
                print(f"[gui_qt] separator settings save failed: {exc}",
                      flush=True)
        self._modlist_view.viewport().update()
        # Deploy paths feed the filemap → rebuild so the routing takes effect.
        self._rebuild_conflicts_async()

    def _close_sep_settings_tab(self):
        if self._tabs.has_key("sep_settings"):
            self._tabs.close_tab("sep_settings")

    def _on_separator_renamed(self, old_name, new_name):
        """Migrate a renamed separator's stored colour + deploy override from the
        old internal name to the new one, then persist + repaint."""
        profile_dir = self._gs.profile_dir()
        # Model colour dict (keyed by internal name) follows the rename.
        c = self._modlist_model.sep_color(old_name)
        if c is not None:
            self._modlist_model.set_sep_color(old_name, None)
            self._modlist_model.set_sep_color(new_name, c)
        if profile_dir is not None:
            try:
                from Utils.profile_state import (
                    read_separator_colors, write_separator_colors,
                    read_separator_deploy_paths, write_separator_deploy_paths)
                colors = read_separator_colors(profile_dir)
                if old_name in colors:
                    colors[new_name] = colors.pop(old_name)
                    write_separator_colors(profile_dir, colors)
                paths = read_separator_deploy_paths(profile_dir)
                if old_name in paths:
                    paths[new_name] = paths.pop(old_name)
                    write_separator_deploy_paths(profile_dir, paths)
            except Exception as exc:
                print(f"[gui_qt] separator rename migration failed: {exc}",
                      flush=True)
        self._modlist_view.viewport().update()

    def _on_separators_removed(self, names):
        """Drop stored colour + deploy override for removed separators."""
        profile_dir = self._gs.profile_dir()
        for nm in names:
            self._modlist_model.set_sep_color(nm, None)
        if profile_dir is not None:
            try:
                from Utils.profile_state import (
                    read_separator_colors, write_separator_colors,
                    read_separator_deploy_paths, write_separator_deploy_paths)
                colors = read_separator_colors(profile_dir)
                paths = read_separator_deploy_paths(profile_dir)
                changed_c = changed_p = False
                for nm in names:
                    changed_c |= colors.pop(nm, None) is not None
                    changed_p |= paths.pop(nm, None) is not None
                if changed_c:
                    write_separator_colors(profile_dir, colors)
                if changed_p:
                    write_separator_deploy_paths(profile_dir, paths)
            except Exception as exc:
                print(f"[gui_qt] separator removal cleanup failed: {exc}",
                      flush=True)

    def _open_missing_reqs_tab(self, target):
        """Open the Missing Requirements panel over the plugins panel. *target* is
        a single mod name (str) or a set of names (multi-select). Triggered by the
        ⚠ flag click + the right-click 'Missing Requirements' item."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return
        domain = getattr(game, "nexus_game_domain", "") or ""
        if not domain:
            self._notify(f"'{game.name}' has no Nexus Mods page.", "warning")
            return
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify("No mod staging folder for this profile.", "warning")
            return
        names = [target] if isinstance(target, str) else list(target or ())
        from Nexus.nexus_meta import read_meta
        from gui_qt.modlist_data import _parse_missing_req_names  # noqa: F401
        specs = []
        for name in names:
            meta = read_meta(staging / name / "meta.ini")
            raw = getattr(meta, "missing_requirements", "") or ""
            ids: set[int] = set()
            for part in raw.split(";"):
                part = part.strip()
                if not part:
                    continue
                head = part.split(":", 1)[0].strip()
                try:
                    ids.add(int(head))
                except ValueError:
                    pass
            if not ids:
                continue
            specs.append({"mod_name": name,
                          "mod_id": int(getattr(meta, "mod_id", 0) or 0),
                          "domain": getattr(meta, "game_domain", "") or domain,
                          "missing_ids": ids})
        if not specs:
            self._notify("No missing requirements.", "info")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO.",
                         "warning")
            return

        from Utils.profile_state import (
            read_ignored_missing_requirements, write_ignored_missing_requirements)
        pdir = self._gs.profile_dir()

        def _save_ignored(s):
            if pdir is not None:
                write_ignored_missing_requirements(pdir, set(s))

        ignored = (read_ignored_missing_requirements(pdir)
                   if pdir is not None else set())

        if self._tabs.has_key("missing_reqs"):
            self._tabs.close_tab("missing_reqs")
        from gui_qt.missing_reqs_view import MissingReqsView
        view = MissingReqsView(
            api, game, specs, ignored, _save_ignored,
            on_close=self._close_missing_reqs_tab, log_fn=self._append_log,
            install_fn=self._install_nexus_mod_by_id)
        self._missing_reqs_view = view
        view.destroyed.connect(
            lambda *_: setattr(self, "_missing_reqs_view", None))
        self._tabs.open_scoped_tab(
            view, "Missing Requirements", self._plugins_panel_stack,
            key="missing_reqs")

    def _close_missing_reqs_tab(self):
        """Close the Missing Requirements panel + refresh flags (an Ignore toggle
        may have changed which mods are flagged)."""
        if self._tabs.has_key("missing_reqs"):
            self._tabs.close_tab("missing_reqs")
        self._reload_modlist()

    # ---- Show Conflicts (full detachable tab) ------------------------------
    def _open_show_conflicts_tab(self, mod_name: str):
        """Open the conflict-detail view for *mod_name* as a full tab. The
        file-level data is computed on a worker thread inside the view (from
        filemap.txt + modindex.bin + bsa_index.bin — the app's ConflictData is
        only mod-level)."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify("No mod staging folder for this profile.", "warning")
            return
        cd = getattr(self, "_conflict_data", None)
        beaten = set()
        if cd is not None:
            beaten = (set(cd.overrides.get(mod_name, set()))
                      | set(cd.bsa_overrides.get(mod_name, set())))
        strip_prefixes = (getattr(game, "mod_folder_strip_prefixes", set())
                          | getattr(game, "mod_folder_strip_prefixes_post", set()))
        plugin_order = [r.name for r in getattr(self._plugin_model, "_rows", [])
                        if getattr(r, "enabled", False)]
        plugin_exts = frozenset(x.lower() for x in
                                (getattr(game, "plugin_extensions", []) or ()))
        ctx = {
            "staging_root": staging,
            "profile_dir": self._gs.profile_dir(),
            "filemap_path": staging.parent / "filemap.txt",
            "modindex_path": staging.parent / "modindex.bin",
            "bsa_index_path": staging.parent / "bsa_index.bin",
            "strip_prefixes": strip_prefixes,
            "beaten_mods": beaten,
            "archive_exts": getattr(game, "archive_extensions", frozenset()),
            "plugin_order": plugin_order,
            "plugin_exts": plugin_exts,
        }
        # Reuse one tab: rebuild it for the new mod if already open.
        if self._tabs.has_key("show_conflicts"):
            self._tabs.close_tab("show_conflicts")
        from gui_qt.show_conflicts_view import ShowConflictsView
        view = ShowConflictsView(
            mod_name, ctx,
            on_close=lambda: self._tabs.close_tab("show_conflicts"),
            log_fn=self._append_log)
        self._tabs.open_tab(view, f"Conflicts: {mod_name}", key="show_conflicts")

    def _on_modlist_endorse(self, names, endorse: bool):
        """Endorse / abstain the given mods on Nexus (right-click). Runs on a
        worker thread via the shared API, updates each mod's meta.ini `endorsed`
        flag, then refreshes the modlist so the ★ flag icon updates. Mirrors Tk
        _vote_selected_mods."""
        import threading
        names = [n for n in (names or []) if n]
        if not names:
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify("Log in first: Nexus ▸ Login to Nexus.", "warning")
            return
        game = self._gs.game
        domain = getattr(game, "nexus_game_domain", "") or ""
        staging = self._gs.staging_dir()
        if staging is None or not domain:
            return
        verb = "Endorsing" if endorse else "Abstaining from"
        self._notify(f"{verb} {len(names)} mod(s)…", "info")

        def _worker():
            from Nexus.nexus_meta import read_meta, write_meta
            ok = 0
            for nm in names:
                meta_path = staging / nm / "meta.ini"
                if not meta_path.is_file():
                    continue
                try:
                    meta = read_meta(meta_path)
                    if not meta.mod_id:
                        continue
                    if endorse:
                        api.endorse_mod(domain, meta.mod_id, meta.version or "")
                    else:
                        api.abstain_mod(domain, meta.mod_id, meta.version or "")
                    meta.endorsed = endorse
                    write_meta(meta_path, meta)
                    ok += 1
                except Exception as exc:
                    self._op_log.emit(f"Nexus: {'endorse' if endorse else 'abstain'} "
                                      f"failed for '{nm}': {exc}")
            self._endorse_done.emit({"ok": ok, "endorse": endorse,
                                     "names": list(names)})

        threading.Thread(target=_worker, daemon=True, name="endorse").start()

    def _on_endorse_done(self, payload):
        """UI thread: report the endorse/abstain result + refresh the ★ flag."""
        ok = payload.get("ok", 0)
        verb = "Endorsed" if payload.get("endorse") else "Abstained from"
        if ok:
            self._notify(f"{verb} {ok} mod(s).", "success")
            self._refresh_modlist_flags(payload.get("names"))
        else:
            self._notify("No mods were updated (already in that state or no "
                         "Nexus id).", "info")

    def _copy_mods_to_profile(self, names, enabled_map, target_profile, move):
        """Copy (or move) the given mods' staging folders into *target_profile*.
        Resolves collisions (single → Replace/Rename/Cancel overlay; multi → one
        Replace-or-skip prompt), copies on a worker thread, and — for a move —
        removes the sources here afterwards. Port of Tk _copy_mod(s)_to_profile."""
        from pathlib import Path
        from Utils import mod_copy
        game = self._gs.game
        src_staging = self._gs.staging_dir()
        src_profile_dir = self._gs.profile_dir()
        if game is None or src_staging is None or src_profile_dir is None:
            return
        try:
            target_profile_dir = game.get_profile_root() / "profiles" / target_profile
            target_staging = mod_copy.resolve_target_staging(game, target_profile_dir)
        except Exception as exc:
            self._notify(f"Could not resolve target profile: {exc}", "error")
            return
        names = [n for n in names if n]
        if not names:
            return
        existing = [n for n in names
                    if mod_copy.mod_exists_in_profile(target_staging, n)]

        # plan[name] = dest_name (rename) or None (copy as-is / replace after wipe)
        def _launch(plan, replace_set):
            self._run_copy_to_profile(
                names, enabled_map, plan, replace_set, move,
                src_staging, src_profile_dir, target_staging, target_profile_dir,
                target_profile, game)

        if not existing:
            _launch({n: None for n in names}, set())
            return

        if len(names) == 1 and existing:
            # Single mod already there → Replace / Rename / Cancel.
            from gui_qt.mod_exists_overlay import ModExistsOverlay
            nm = names[0]

            def _resolved(action, _nm=nm):
                if not action or action == "cancel":
                    self._notify("Copy cancelled.", "info")
                    return
                if action == "replace":
                    _launch({_nm: None}, {_nm})
                elif action.startswith("rename:"):
                    new = action.split(":", 1)[1].strip()
                    if new:
                        _launch({_nm: new}, set())

            ModExistsOverlay.show_over(self, nm, False, _resolved)
        else:
            # Several already there → one Replace-or-skip prompt (Tk parity).
            from gui_qt.confirm_overlay import ConfirmOverlay

            def _resolved(replace):
                if replace:
                    _launch({n: None for n in names}, set(existing))
                else:
                    # Skip existing: copy only the non-colliding ones.
                    keep = [n for n in names if n not in existing]
                    if not keep:
                        self._notify("All selected mods already exist there.", "info")
                        return
                    _launch({n: None for n in keep}, set())

            ConfirmOverlay.show_over(
                self, "Copy to profile",
                f"{len(existing)} of {len(names)} mod(s) already exist in "
                f"'{target_profile}'. Replace them? (Cancel skips those.)",
                _resolved, confirm_label="Replace", cancel_label="Skip",
                danger=False)

    def _run_copy_to_profile(self, names, enabled_map, plan, replace_set, move,
                             src_staging, src_profile_dir, target_staging,
                             target_profile_dir, target_profile, game):
        """Worker: copy each planned mod, then (move) remove the sources."""
        import threading
        from pathlib import Path
        from Utils import mod_copy
        # Serialize: a second copy/move while one runs would write the same
        # target folders concurrently (install has the same guard).
        if getattr(self, "_copy_running", False):
            self._notify("A copy/move is already in progress.", "info")
            return
        self._copy_running = True
        self._notify(f"{'Moving' if move else 'Copying'} {len(plan)} mod(s) to "
                     f"'{target_profile}'…", "info")

        def _worker():
            copied = []
            for nm, dest_name in plan.items():
                try:
                    if nm in replace_set:
                        import shutil
                        shutil.rmtree(target_staging / (dest_name or nm),
                                      ignore_errors=True)
                    out = mod_copy.copy_mod_to_profile(
                        Path(src_staging), Path(src_profile_dir),
                        Path(target_staging), Path(target_profile_dir),
                        nm, enabled_map.get(nm, True), dest_name=dest_name)
                    if out:
                        copied.append(nm)
                except Exception as exc:
                    self._op_log.emit(f"Copy to profile failed for '{nm}': {exc}")
            removed = False
            if move and copied:
                try:
                    from Utils.mod_remove import remove_mods
                    remove_mods(game, Path(src_profile_dir), copied,
                                log_fn=lambda m: self._op_log.emit(str(m)))
                    removed = True
                except Exception as exc:
                    self._op_log.emit(f"Move: could not remove sources: {exc}")
            self._copy_done.emit({"copied": len(copied), "total": len(plan),
                                  "move": move, "removed": removed,
                                  "target": target_profile,
                                  # names to drop from THIS profile's modlist
                                  # (remove_mods deliberately leaves modlist.txt
                                  # to the caller — mirror Tk _finish_copy_popup).
                                  "removed_names": list(copied) if removed else []})

        threading.Thread(target=_worker, daemon=True, name="copy-to-profile").start()

    def _on_copy_done(self, payload):
        """UI thread: report the copy/move result. For a move, drop the moved
        mods' rows from this profile's modlist (remove_mods left modlist.txt to
        us — mirrors Tk _finish_copy_popup calling _remove_selected_mods), which
        persists modlist.txt via the model, then reload."""
        self._copy_running = False
        c = payload.get("copied", 0)
        verb = "Moved" if payload.get("move") else "Copied"
        self._notify(f"{verb} {c}/{payload.get('total', 0)} mod(s) to "
                     f"'{payload.get('target', '')}'.",
                     "success" if c else "info")
        removed_names = set(payload.get("removed_names") or [])
        if removed_names:
            model = self._modlist_model
            # Remove by name, highest row first (indices shift as we delete).
            rows = [r for r in range(model.rowCount())
                    if (e := model.entry(r)) is not None
                    and not e.is_separator and e.name in removed_names]
            for r in sorted(rows, reverse=True):
                model.remove_row(r)          # drops the row + saves modlist.txt
        if payload.get("removed"):
            self._reload_modlist()

    # ---- install a Nexus mod by id (used by Missing Requirements cards) ----
    # Mirrors the Nexus browser's install flow: premium check → fetch files →
    # pick MAIN (or file chooser if several) → download → hand to _install_paths.
    # All worker stages hop back to the UI thread via Signals (NOT QThread).

    def _install_nexus_mod_by_id(self, mod_id: int, domain: str, name: str):
        if self._req_installing:
            self._notify("An install is already in progress.", "info")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify("Log in to Nexus first.", "warning")
            return
        mod_id = int(mod_id or 0)
        if mod_id <= 0:
            self._notify("That requirement has no Nexus mod page.", "warning")
            return
        self._req_installing = True
        ctx = {"mod_id": mod_id, "domain": domain, "name": name}
        self._append_log(f"[nexus] preparing install for {name}…")
        import threading

        def worker():
            files = None
            try:
                user = api.validate()
                if not bool(getattr(user, "is_premium", False)):
                    # Non-premium: open the files page (site 'Download with Manager').
                    from Utils.xdg import open_url
                    open_url(f"https://www.nexusmods.com/{domain}/mods/{mod_id}"
                             f"?tab=files", log_fn=self._append_log)
                    self._append_log("[nexus] premium required for direct "
                                     "download — opened the files page.")
                    self._req_install_files.emit(ctx, None)
                    return
                resp = api.get_mod_files(domain, mod_id)
                files = list(resp.files)
            except Exception as exc:
                self._append_log(f"[nexus] install prep failed: {exc}")
            self._req_install_files.emit(ctx, files)

        threading.Thread(target=worker, daemon=True, name="req-install-files").start()

    def _on_req_install_files(self, ctx, files):
        """UI thread: pick which file to install (file chooser if >1 MAIN)."""
        if files is None:           # non-premium / error path already handled
            self._req_installing = False
            return
        mains = [f for f in files if f.category_name == "MAIN"] or list(files)
        if not mains:
            self._notify("No downloadable files for that mod.", "warning")
            self._req_installing = False
            return
        mains.sort(key=lambda f: getattr(f, "uploaded_timestamp", 0), reverse=True)
        if len(mains) > 1:
            from gui_qt.nexus_file_chooser import NexusFileChooser

            def _picked(chosen):
                if chosen is None:
                    self._req_installing = False
                    return
                self._start_req_download(ctx, chosen)

            NexusFileChooser.show_over(self, ctx["name"], mains, _picked)
        else:
            self._start_req_download(ctx, mains[0])

    def _start_req_download(self, ctx, f):
        domain, mod_id, name = ctx["domain"], ctx["mod_id"], ctx["name"]
        self._append_log(f"[nexus] downloading {f.file_name or name}…")

        class _Info:
            pass
        info = _Info()
        info.mod_id = mod_id
        info.domain_name = domain
        info.name = name
        import threading

        def worker():
            archive = meta = None
            try:
                from Nexus.nexus_download import NexusDownloader
                from Utils.config_paths import get_download_cache_dir_for_game
                from Nexus.nexus_meta import build_meta_from_download
                dest = get_download_cache_dir_for_game(
                    getattr(self._gs.game, "name", "") or "")
                size = (f.size_in_bytes or 0) or (f.size_kb * 1024)
                result = NexusDownloader(
                    self._nexus_api, download_dir=dest).download_file(
                    game_domain=domain, mod_id=mod_id, file_id=f.file_id,
                    dest_dir=dest, known_file_name=f.file_name,
                    expected_size_bytes=size, progress_cb=lambda d, t: None)
                if result.success and result.file_path is not None:
                    archive = str(result.file_path)
                    try:
                        meta = build_meta_from_download(
                            game_domain=domain, mod_id=mod_id, file_id=f.file_id,
                            archive_name=result.file_name, mod_info=info,
                            file_info=f)
                    except Exception:
                        meta = None
                else:
                    self._append_log(f"[nexus] download failed: "
                                     f"{result.error or 'unknown error'}")
            except Exception as exc:
                self._append_log(f"[nexus] download error: {exc}")
            self._req_install_dl.emit(archive, meta)

        threading.Thread(target=worker, daemon=True, name="req-install-dl").start()

    def _on_req_install_dl(self, archive, meta):
        self._req_installing = False
        if not archive:
            return
        self._append_log(f"[nexus] downloaded → {archive}; installing…")
        self._install_paths([archive], {archive: meta} if meta is not None else None)

    def _on_modlist_flag_clicked(self, row: int, flag: int):
        """A flag icon in the modlist Flags column was clicked → its action
        (Tk parity, gui/modlist_panel ~3960): update→Change Version,
        modio-update→open mod.io page, missing→Missing Requirements,
        note→note editor. (Bundle options deferred — no Qt bundle UI yet.)"""
        from gui_qt.modlist_data import (
            FLAG_UPDATE, FLAG_MISSING_REQS, FLAG_NOTE, FLAG_MODIO_UPDATE)
        e = self._modlist_model.entry(row)
        if e is None or e.is_separator:
            return
        if flag == FLAG_UPDATE:
            self._open_change_version_tab(e.name)
        elif flag == FLAG_MODIO_UPDATE:
            from gui_qt.modlist_menu import _open_on_modio
            _open_on_modio(self._modlist_view, e.name)
        elif flag == FLAG_MISSING_REQS:
            self._open_missing_reqs_tab(e.name)
        elif flag == FLAG_NOTE:
            from gui_qt.modlist_menu import _open_note_editor
            _open_note_editor(self._modlist_view, [e.name])

    def _on_add_game_select(self, name: str):
        """A configured game was picked in the Add-Game view → switch to it and
        close the tab."""
        self._tabs.close_tab("add_game")
        if name in self._gs.game_names:
            self._on_game_changed(name)
            self._game_selector.set_current(name)
        self._append_log(f"[game] selected {name}")

    def _on_add_game_add(self, name: str):
        """An unconfigured game was picked → open the configure-game tab."""
        from gui.game_helpers import _GAMES
        game = _GAMES.get(name)
        if game is None:
            self._append_log(f"[game] {name} not found in registry")
            return
        self._tabs.close_tab("add_game")
        self._open_configure_game_tab(game)

    def _open_configure_game_tab(self, game):
        """Open the (live) Configure-Game view as a detachable tab."""
        from gui_qt.configure_game_view import ConfigureGameView

        def _done(saved: bool, removed: bool):
            self._tabs.close_tab("configure_game")
            if saved or removed:
                # Refresh the game registry + selector; switch to the game if it
                # is now configured, else fall back to the current/ first game.
                from gui.game_helpers import _load_games
                names = _load_games()
                self._gs.game_names = names
                self._game_selector.set_items(names, current=self._gs.game_name)
                if saved and game.name in names:
                    self._on_game_changed(game.name)
                    self._game_selector.set_current(game.name)
                elif removed:
                    self._append_log(f"[game] removed instance: {game.name}")

        page = ConfigureGameView(game, on_done=_done)
        verb = "Reconfigure" if game.is_configured() else "Add"
        self._tabs.open_tab(page, f"{verb} game", key="configure_game")

    def _on_profile_action(self, which):
        if which == "add":
            if self._gs.game is None:
                self._append_log("[profile] no game selected.")
                return
            self._new_profile_bar.open_for()
        elif which == "settings":
            self._open_profile_settings_tab()
        elif which == "export":
            self._open_export_profile_tab()
        elif which == "import":
            self._import_profile()
        else:
            self._append_log(f"[profile] {which} (not wired yet)")

    def _open_export_profile_tab(self):
        """Open the Export Profile panel scoped over the MODLIST panel (like Profile
        Settings): per-mod source/version/optional config + Export to a .amethyst."""
        if self._gs.game_name is None:
            self._notify("No game selected.", "warning")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return
        if self._tabs.has_key("export_profile"):
            self._tabs.focus_key("export_profile")
            return
        api = self._ensure_nexus_api()   # optional — version/size fetch needs it
        from gui_qt.export_profile_view import ExportProfileView
        view = ExportProfileView(self, game, api, log_fn=self._append_log)
        self._export_profile_view = view
        self._tabs.open_scoped_tab(
            view, "Export Profile", self._modlist_panel_stack,
            key="export_profile")

    def _import_profile(self):
        """Import a .amethyst / manifest: parse it, then reuse the collection detail
        + install pipeline (via CollectionDetailView with a local manifest) to build
        a new profile from it. Requires a configured game matching the manifest."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO.",
                         "warning")
            return
        # Native picker via the XDG portal (portal_filechooser). The callback fires
        # on a WORKER thread — QTimer.singleShot(0, …) from there never fires (no
        # event loop on that thread), so marshal to the GUI thread with a Signal
        # (auto-queued to the receiver's thread).
        from Utils.portal_filechooser import pick_file
        pick_file(
            "Import profile",
            lambda p: self._import_file_picked.emit(p),
            filters=[("Amethyst Manifest (*.amethyst *.zip *.json)",
                      ["*.amethyst", "*.zip", "*.json"]),
                     ("All files", ["*"])])

    def _on_import_file_picked(self, picked):
        """GUI thread: continue the import once a file was chosen in the portal."""
        if not picked:
            return
        path = str(picked)
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO.",
                         "warning")
            return
        from Utils import profile_export
        try:
            manifest = profile_export.read_manifest(path)
        except Exception as exc:
            self._notify(f"Could not read manifest: {exc}", "error")
            return
        if not isinstance(manifest, dict) or not manifest.get("mods"):
            self._notify("That file doesn't look like an Amethyst manifest.",
                         "warning")
            return
        # Game-domain guard: v1 requires the selected game to match the manifest.
        man_domain = ((manifest.get("info") or {}).get("domainName") or "").strip()
        game_domain = (getattr(game, "nexus_game_domain", "") or "").strip()
        if man_domain and game_domain and man_domain.lower() != game_domain.lower():
            self._notify(
                f"This profile targets '{man_domain}', but the selected game is "
                f"'{game_domain}'. Switch games first, then import.", "warning")
            return

        # Build a bare NexusCollection + open the detail tab populated from the
        # manifest; the bundle zip (if a .amethyst) restores mods/profile after.
        from Nexus.nexus_api import NexusCollection
        name = (manifest.get("info") or {}).get("name") or Path(path).stem
        collection = NexusCollection(
            id=0, slug=f"import_{Path(path).stem}", name=name,
            game_domain=man_domain or game_domain)
        bundle_zip = path if Path(path).suffix.lower() in (".amethyst", ".zip") else ""
        key = f"import_profile_{Path(path).stem}"
        if self._tabs.has_key(key):
            self._tabs.focus_key(key)
            return
        from gui_qt.collection_detail_view import CollectionDetailView
        view = CollectionDetailView(
            api, collection, game, log_fn=self._append_log,
            local_manifest=manifest, bundle_zip=bundle_zip)
        view.set_install_handler(
            lambda chosen, skipped, intent="install": self._install_collection(
                collection, view, chosen, skipped, intent))
        self._tabs.open_tab(view, f"Import: {name}", key=key)

    def _open_profile_settings_tab(self):
        """Open the Profile Settings panel scoped over the MODLIST panel (like the
        Settings gear / image preview): profile rows with lock / rename / open /
        remove, while the plugins panel + the rest of the UI stay live."""
        if self._gs.game_name is None:
            self._notify("No game selected.", "warning")
            return
        if self._tabs.has_key("profile_settings"):
            self._tabs.focus_key("profile_settings")
            return
        from gui_qt.profile_settings_view import ProfileSettingsView
        view = ProfileSettingsView(
            self,
            game_name=self._gs.game_name,
            current_profile=self._gs.profile,
            on_profile_renamed=self._on_profile_renamed,
            on_profile_removed=self._on_profile_removed,
            on_profiles_changed=self._on_profiles_lock_changed,
            log_fn=self._append_log,
        )
        self._profile_settings_view = view
        self._tabs.open_scoped_tab(
            view, "Profile Settings", self._modlist_panel_stack,
            key="profile_settings")

    # -- Profile Settings callbacks (view → app: refresh selector + reload) --
    def _on_profiles_lock_changed(self):
        """A profile's lock toggled — the active profile is unchanged, so just
        refresh the selector list (Remove-eligibility, etc.)."""
        self._profile_selector.set_items(self._gs.profiles(),
                                         current=self._gs.profile)

    def _on_profile_renamed(self, old: str, new: str):
        profs = self._gs.profiles()
        if self._gs.profile == old:
            # The active profile was renamed → switch GameState + reload.
            self._gs.set_profile(new)
            self._profile_selector.set_items(profs, current=new)
            self._reload_modlist()
            self._reload_plugins()
        else:
            self._profile_selector.set_items(profs, current=self._gs.profile)
        self._update_deployed_profile_highlight()

    def _on_profile_removed(self, name: str):
        profs = self._gs.profiles()
        if self._gs.profile == name:
            # The active profile was removed → fall back like the view did.
            target = "default" if "default" in profs else (
                profs[0] if profs else "default")
            self._gs.set_profile(target)
            self._profile_selector.set_items(profs, current=target)
            self._reload_modlist()
            self._reload_plugins()
        else:
            self._profile_selector.set_items(profs, current=self._gs.profile)
        self._update_deployed_profile_highlight()

    def _on_new_profile_create(self, name: str, profile_specific_mods: bool):
        """Create a new profile for the active game and switch to it. Mirrors the
        Tk top_bar._on_add_profile flow: reject an existing name, create via the
        neutral _create_profile, repopulate the selector, select + reload."""
        from gui.game_helpers import _create_profile, _profiles_for_game
        game_name = self._gs.game_name
        if not game_name:
            return
        existing = _profiles_for_game(game_name)
        if name in existing:
            self._notify(f"Profile '{name}' already exists.", "error")
            # Re-open so the user can pick another name (fields reset).
            self._new_profile_bar.open_for()
            return
        try:
            _create_profile(game_name, name,
                            profile_specific_mods=profile_specific_mods)
        except Exception as exc:
            self._append_log(f"[profile] create failed: {exc}")
            self._notify(f"Could not create profile: {exc}", "error")
            return
        # Success — make sure the bar is closed (the widget hides itself before
        # calling us, but close explicitly so the flow is robust to any caller).
        self._new_profile_bar.close()
        self._append_log(f"[profile] created '{name}'"
                         + (" (profile-specific mods)" if profile_specific_mods
                            else ""))
        # Refresh the profile selector, select the new profile, and load it.
        profs = self._gs.profiles()
        self._profile_selector.set_items(profs, current=name)
        self._gs.set_profile(name)
        self._profile_selector.set_current(name)
        self._reload_modlist()
        self._reload_plugins()
        self._update_deployed_profile_highlight()
        self._notify(f"Profile '{name}' created", "info")

    def _update_deployed_profile_highlight(self):
        """Green-highlight the deployed profile in the profile dropdown. Reads the
        same backend state the Tk app writes (game deploy-state JSON via
        get_deploy_active / get_last_deployed_profile)."""
        game = self._gs.game
        deployed = None
        try:
            if game is not None and game.get_deploy_active():
                deployed = game.get_last_deployed_profile()
        except Exception:
            deployed = None
        if hasattr(self, "_profile_selector"):
            self._profile_selector.set_highlighted_item(deployed)

    def _on_play_action(self, which):
        self._append_log(f"[play] {which} (not wired yet)")

    # ------------------------------------------------------------- deploy/restore
    def _ensure_feedback(self):
        """Lazily create the progress popup + notifier (host = central widget)."""
        if self._notifier is None:
            from gui_qt.notifications import ProgressPopup, NotificationManager
            host = self.centralWidget() or self
            self._progress_popup = ProgressPopup(host)
            self._notifier = NotificationManager(host)

    def _notify(self, text: str, state: str = "info"):
        self._ensure_feedback()
        self._notifier.notify(text, state)

    def _set_deploy_buttons_enabled(self, enabled: bool):
        for b in (getattr(self, "_deploy_btn", None), getattr(self, "_restore_btn", None)):
            if b is not None:
                b.setEnabled(enabled)

    def _on_deploy(self):
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return
        if not hasattr(game, "deploy"):
            self._notify(f"'{game.name}' does not support deployment.", "warning")
            return
        # Serialize: coalesce a request that arrives mid-deploy into one re-run.
        if self._deploy_running:
            self._deploy_rerun_pending = True
            return
        self._deploy_running = True
        self._op_is_restore = False
        self._op_title = "Deploying"
        self._set_deploy_buttons_enabled(False)
        self._ensure_feedback()
        self._notify(f"Deploying {game.name}…", "info")
        profile = self._gs.profile
        rf_enabled = True   # Root_Folder toggle lives in the modlist; default on

        import threading

        def worker():
            from Utils.deploy_pipeline import run_deploy_pipeline
            ok = False
            warns = []
            try:
                ok = run_deploy_pipeline(
                    game, profile,
                    log_fn=lambda m: self._op_log.emit(str(m)),
                    progress_fn=lambda d, t, p=None: self._op_progress.emit(d, t, p),
                    root_folder_enabled=rf_enabled,
                    confirm_cet=None,
                    do_backup=True,
                )
            except Exception as exc:
                self._op_log.emit(f"Deploy error: {exc}")
            finally:
                try:
                    warns = list(game.pop_deploy_warnings())
                except Exception:
                    warns = []
                self._op_done.emit("deploy", bool(ok), warns)

        threading.Thread(target=worker, daemon=True).start()

    def closeEvent(self, event):
        """On close, optionally restore every deployed game to vanilla (the
        'Restore on close' setting). Synchronous (the app is exiting) — mirrors
        the Tk gui.py shutdown path."""
        try:
            from Utils.ui_config import load_restore_on_close
            if load_restore_on_close():
                self._restore_all_on_close()
        except Exception as exc:
            print(f"[gui_qt] restore-on-close error: {exc}", flush=True)
        super().closeEvent(event)

    def _restore_all_on_close(self):
        """Restore every configured game that has an active deployment back to
        vanilla. Ported from gui.py `_restore_all_on_close`."""
        from gui_qt.game_state import _GAMES
        from Utils.deploy import restore_root_folder

        games = [g for g in _GAMES.values()
                 if g.is_configured() and g.get_deploy_active()
                 and getattr(g, "restore_on_close_eligible", True)]
        if not games:
            return
        log_fn = lambda m: print(f"[restore-on-close] {m}", flush=True)
        log_fn(f"restoring {len(games)} game(s)...")
        for game in games:
            try:
                game_root = game.get_game_path()
                last_deployed = game.get_last_deployed_profile()
                original_profile_dir = getattr(game, "_active_profile_dir", None)
                if last_deployed:
                    game.set_active_profile_dir(
                        game.get_profile_root() / "profiles" / last_deployed)
                    # Reload so the last-deployed profile's path overrides drive
                    # the restore (it may target a different game folder).
                    game.load_paths()
                    game_root = game.get_game_path()
                try:
                    if hasattr(game, "restore"):
                        game.restore(log_fn=log_fn)
                    root_folder_dir = game.get_effective_root_folder_path()
                    if root_folder_dir.is_dir() and game_root:
                        restore_root_folder(root_folder_dir, game_root, log_fn=log_fn)
                    game.clear_deploy_active()
                finally:
                    if original_profile_dir is not None:
                        game.set_active_profile_dir(original_profile_dir)
                        game.load_paths()
            except Exception as e:
                log_fn(f"error for {game.name}: {e}")

    def _on_restore(self):
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return
        if self._deploy_running:
            self._notify("A deploy is in progress — try again shortly.", "warning")
            return
        self._deploy_running = True
        self._op_is_restore = True
        self._op_title = "Restoring"
        self._set_deploy_buttons_enabled(False)
        self._ensure_feedback()
        self._notify(f"Restoring {game.name}…", "info")
        profile = self._gs.profile

        import threading
        from Utils.deploy import restore_root_folder

        def worker():
            ok = True
            try:
                from Utils.deploy_pipeline import check_paths_mounted
                err = check_paths_mounted(game)
                if err:
                    self._op_log.emit(f"Restore aborted: {err}")
                    ok = False
                else:
                    last = game.get_last_deployed_profile()
                    if last:
                        game.set_active_profile_dir(
                            game.get_profile_root() / "profiles" / last)
                        game.load_paths()
                    game_root = game.get_game_path()
                    if hasattr(game, "restore"):
                        game.restore(
                            log_fn=lambda m: self._op_log.emit(str(m)),
                            progress_fn=lambda d, t, p=None: self._op_progress.emit(d, t, p))
                    rf = game.get_effective_root_folder_path()
                    if rf.is_dir() and game_root:
                        restore_root_folder(
                            rf, game_root,
                            log_fn=lambda m: self._op_log.emit(str(m)),
                            data_deploy_dirs=(game.root_restore_protect_dirs()
                                              if hasattr(game, "root_restore_protect_dirs") else None))
            except Exception as exc:
                ok = False
                self._op_log.emit(f"Restore error: {exc}")
            finally:
                # Always restore the active profile dir to the selected profile.
                try:
                    game.set_active_profile_dir(
                        game.get_profile_root() / "profiles" / profile)
                    game.load_paths()
                    if ok and hasattr(game, "clear_deploy_active"):
                        game.clear_deploy_active()
                except Exception:
                    pass
                self._op_done.emit("restore", ok, [])

        threading.Thread(target=worker, daemon=True).start()

    def _on_op_progress(self, done: int, total: int, phase):
        if self._progress_popup is not None:
            title = getattr(self, "_op_title", "Working")
            self._progress_popup.set_progress(done, total, phase, title=title)

    def _on_op_done(self, kind: str, success: bool, warnings):
        self._deploy_running = False
        self._set_deploy_buttons_enabled(True)
        if self._progress_popup is not None:
            QTimer.singleShot(1200, self._progress_popup.clear)
        # Refresh the modlist/conflicts + deployed-profile highlight after the op.
        self._reload_modlist()
        self._update_deployed_profile_highlight()
        verb = "Deployed" if kind == "deploy" else "Restored"
        if success:
            self._notify(f"{self._gs.game.name if self._gs.game else 'Game'} {verb}",
                         "success")
        else:
            self._notify(f"{verb.rstrip('ed')} failed — see log.", "error")
        for w in (warnings or []):
            self._notify(w, "warning")
        # Coalesced re-deploy if mod state changed mid-deploy.
        if kind == "deploy" and self._deploy_rerun_pending:
            self._deploy_rerun_pending = False
            QTimer.singleShot(0, self._on_deploy)

    # ----------------------------------------------------------------- install
    def _on_install_mod(self):
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return
        if self._install_running:
            self._notify("An install is already in progress.", "warning")
            return
        from Utils.portal_filechooser import pick_files

        def _on_picked(paths):
            if paths:
                self._install_paths([str(p) for p in paths])

        pick_files("Select mod archive(s)",
                   lambda ps: QTimer.singleShot(0, lambda: _on_picked(ps)))

    # ---- Proton tools ------------------------------------------------------
    def _proton_game(self):
        """Return the active configured game, or None (after notifying)."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return None
        return game

    def _open_folder_path(self, path, descr):
        """Open *path* in the file manager, or log why it can't be opened.
        Ported from gui/dialogs ProtonTools._open_folder."""
        from pathlib import Path
        if path is None:
            self._notify(f"{descr} is not configured for this game.", "warning")
            return
        path = Path(path)
        if not path.is_dir():
            self._notify(f"{descr} not found ({path}).", "warning")
            return
        from Utils.xdg import xdg_open
        try:
            xdg_open(str(path))
        except Exception as e:
            self._append_log(f"Open {descr} error: {e}")

    def _open_game_dir(self, which: str):
        """Open one of the game's on-disk folders (game selector ▸ Open ▸ …).
        Mirrors gui/dialogs ProtonTools folder openers."""
        from pathlib import Path
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return
        if which == "game":
            self._open_folder_path(game.get_game_path(), "game folder")
        elif which == "prefix":
            self._open_folder_path(game.get_prefix_path(), "prefix folder")
        elif which == "mygames":
            getter = getattr(game, "_mygames_path", None)
            path = getter() if callable(getter) else None
            if path is None:
                prefix = game.get_prefix_path()
                if prefix is not None:
                    path = prefix / "drive_c/users/steamuser/Documents/My Games"
            self._open_folder_path(path, "My Games folder")
        elif which == "appdata":
            prefix = game.get_prefix_path()
            if prefix is None:
                self._open_folder_path(None, "AppData folder")
                return
            sub = getattr(game, "_APPDATA_SUBPATH", None)
            if sub is not None:
                self._open_folder_path(prefix / sub, "AppData folder")
            else:
                self._open_folder_path(
                    prefix / "drive_c/users/steamuser/AppData/Local",
                    "AppData folder")
        elif which == "staging":
            getter = getattr(game, "get_effective_mod_staging_path", None) \
                or getattr(game, "get_mod_staging_path", None)
            path = getter() if callable(getter) else None
            self._open_folder_path(path, "staging folder")
        elif which == "profile":
            path = getattr(game, "_active_profile_dir", None)
            if path is None:
                try:
                    path = game.get_profile_root() / "profiles"
                except Exception:
                    path = None
            self._open_folder_path(path, "profile folder")
        elif which == "config":
            try:
                from Utils.config_paths import get_config_dir
                path = get_config_dir()
            except Exception:
                path = None
            self._open_folder_path(path, ".config folder")

    def _proton_winecfg(self):
        game = self._proton_game()
        if game is None:
            return
        from Utils.proton_tools import launch_wine_tool
        launch_wine_tool(game, "winecfg", log_fn=self._append_log)

    def _proton_regedit(self):
        game = self._proton_game()
        if game is None:
            return
        from Utils.proton_tools import launch_wine_tool
        launch_wine_tool(game, "regedit", log_fn=self._append_log)

    def _proton_dll_overrides(self):
        """Open the Wine DLL overrides manager scoped over the MODLIST panel
        (like the Settings gear): a per-DLL load-order picker for this game's
        prefix. The plugins panel + rest of the UI stay live."""
        game = self._proton_game()
        if game is None:
            return
        if self._tabs.has_key("dll_overrides"):
            self._tabs.focus_key("dll_overrides")
            return
        from gui_qt.dll_overrides_view import DllOverridesView
        view = DllOverridesView(self, game, log_fn=self._append_log)
        self._dll_overrides_view = view
        self._tabs.open_scoped_tab(
            view, "Wine DLL overrides", self._modlist_panel_stack,
            key="dll_overrides")

    def _proton_winetricks(self):
        game = self._proton_game()
        if game is None:
            return
        import threading
        from Utils.proton_tools import launch_winetricks
        self._notify("Launching winetricks…", "info")
        threading.Thread(
            target=lambda: launch_winetricks(
                game, log_fn=lambda m: self._op_log.emit(str(m))),
            daemon=True).start()

    def _proton_run_exe(self):
        game = self._proton_game()
        if game is None:
            return
        from Utils.portal_filechooser import pick_exe_file
        from Utils.proton_tools import launch_exe_in_prefix

        def _picked(exe_path):
            if exe_path is None:
                return
            launch_exe_in_prefix(game, exe_path, log_fn=self._append_log)

        pick_exe_file("Select EXE to run in this prefix", _picked)

    def _proton_install_vcredist(self):
        from Utils.proton_tools import install_vcredist
        self._run_proton_installer(
            "Installing VC++ Redistributable",
            lambda plog: install_vcredist(self._gs.game, log_fn=plog))

    def _proton_install_d3dcompiler(self):
        from Utils.proton_tools import install_d3dcompiler_47
        self._run_proton_installer(
            "Installing d3dcompiler_47",
            lambda plog: install_d3dcompiler_47(self._gs.game, log_fn=plog))

    def _proton_install_dotnet(self, version: str):
        from Utils.proton_tools import install_dotnet
        self._run_proton_installer(
            f"Installing .NET {version}",
            lambda plog: install_dotnet(self._gs.game, version, log_fn=plog))

    def _run_proton_installer(self, title: str, worker_fn):
        """Run a blocking Proton installer (*worker_fn(log_fn) -> bool*) on a
        worker thread, showing the indeterminate progress popup + a toast on
        completion. Serialized: refuses a second installer while one runs."""
        game = self._proton_game()
        if game is None:
            return
        if self._proton_busy:
            self._notify("A Proton installer is already running.", "warning")
            return
        self._proton_busy = True
        self._op_title = title
        self._ensure_feedback()
        self._notify(f"{title}…", "info")
        self._op_progress.emit(0, 0, title)   # indeterminate (busy) bar

        import threading

        def _run():
            ok = False
            try:
                ok = bool(worker_fn(lambda m: self._op_log.emit(f"Proton Tools: {m}")))
            except Exception as exc:
                self._op_log.emit(f"Proton Tools error: {exc}")
            self._proton_done.emit(title, ok)

        threading.Thread(target=_run, daemon=True).start()

    def _on_proton_done(self, title: str, success: bool):
        self._proton_busy = False
        if self._progress_popup is not None:
            self._progress_popup.clear()
        if success:
            self._notify(f"{title} — done.", "success")
        else:
            self._notify(f"{title} — failed (see log).", "error")

    def _install_paths(self, paths: list[str], metas: dict | None = None,
                       previous_mod_name: str | None = None,
                       preferred_names: dict | None = None,
                       on_all_done=None):
        """Queue + install a list of archive paths (shared by the Install Mod
        button and the Downloads tab). FOMODs pause for the wizard mid-queue.
        *metas* optionally maps an archive path → a prebuilt NexusModMeta (the
        Nexus browser supplies the real mod_id/file_id so meta.ini is correct).
        *previous_mod_name* — when set (Change Version updating an existing mod),
        and a single install lands under a DIFFERENT folder name, offer to remove
        that previous version (it inherits its modlist slot).
        *preferred_names* — optional archive-path → forced mod-folder name. Forces
        the install into that folder and SILENTLY replaces it (no Mod-Already-Exists
        dialog). Used by Quick Update, where the name match is already confirmed.
        *on_all_done* — optional no-arg callback fired once the whole batch finishes
        (after the summary), so a caller can chain post-install work (Quick Update
        re-checks flags + reports its own summary)."""
        if not paths:
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return
        if getattr(self, "_install_running", False):
            self._notify("An install is already in progress.", "warning")
            return
        profile_dir = self._gs.profile_dir()
        if profile_dir is None:
            self._notify("No active profile.", "warning")
            return
        self._install_running = True
        self._op_is_restore = False
        self._op_title = "Installing"
        if hasattr(self, "_install_btn"):
            self._install_btn.setEnabled(False)
        self._ensure_feedback()
        # Process archives ONE AT A TIME so a FOMOD can pause for the wizard.
        self._install_queue = list(paths)
        self._install_total = len(paths)
        self._install_ok = []
        self._install_game = game
        self._install_profile_dir = profile_dir
        self._install_metas = dict(metas or {})
        self._install_prev_name = previous_mod_name
        self._install_preferred = dict(preferred_names or {})
        self._install_all_done_cb = on_all_done
        self._notify(f"Installing {len(paths)} mod(s)…" if len(paths) > 1
                     else f"Installing {Path(paths[0]).name}…", "info")
        self._install_next()

    def _make_need_prefix_cb(self):
        """Return an on_need_prefix(required, file_list, mod_name) callback for the
        install worker. It runs on the WORKER thread, so it asks the UI thread to
        show the Set-Prefix overlay (via _need_prefix) and BLOCKS on an Event
        until the user responds — mirroring the Tk non-main-thread prefix dialog.
        Returns the prefix str ("" = as-is), or None (cancel)."""
        import threading

        def _cb(required, file_list, mod_name):
            holder = {"result": None}
            ev = threading.Event()
            self._need_prefix.emit({
                "required": required, "file_list": file_list,
                "mod_name": mod_name, "holder": holder, "event": ev})
            ev.wait()
            return holder["result"]

        return _cb

    def _on_need_prefix_ui(self, payload):
        """UI thread: show the Set-Prefix overlay; unblock the worker when done.
        The progress popup is hidden while the user decides (no work running)."""
        if self._progress_popup is not None:
            self._progress_popup.clear()
        from gui_qt.set_prefix_overlay import SetPrefixOverlay

        def _done(result):
            payload["holder"]["result"] = result
            payload["event"].set()

        SetPrefixOverlay.show_over(
            self, payload["mod_name"], payload["required"],
            payload["file_list"], _done)

    def _make_exists_cb(self):
        """Return an on_exists(mod_name, conflict) callback for finish_install.
        Runs on the WORKER thread → shows the Mod-Already-Exists overlay on the
        UI thread and BLOCKS until the user picks (replace / rename:<n> / cancel),
        mirroring _make_need_prefix_cb."""
        import threading

        def _cb(mod_name, conflict=False):
            holder = {"result": "cancel"}
            ev = threading.Event()
            self._mod_exists.emit({
                "mod_name": mod_name, "conflict": bool(conflict),
                "holder": holder, "event": ev})
            ev.wait()
            return holder["result"]

        return _cb

    def _on_mod_exists_ui(self, payload):
        """UI thread: show the Mod-Already-Exists overlay; unblock the worker."""
        if self._progress_popup is not None:
            self._progress_popup.clear()
        from gui_qt.mod_exists_overlay import ModExistsOverlay

        def _done(result):
            payload["holder"]["result"] = result or "cancel"
            payload["event"].set()

        ModExistsOverlay.show_over(
            self, payload["mod_name"], payload["conflict"], _done)

    def _install_next(self):
        """Pop the next queued archive: prepare it on a worker; the prepared
        result comes back on _prepared_ready (UI thread)."""
        if not self._install_queue:
            self._install_done.emit(len(self._install_ok), self._install_total,
                                    self._install_ok)
            return
        path = self._install_queue.pop(0)
        idx = self._install_total - len(self._install_queue)
        self._op_log.emit(f"Installing ({idx}/{self._install_total}): {Path(path).name}")

        import threading

        meta = getattr(self, "_install_metas", {}).get(path)
        forced_name = getattr(self, "_install_preferred", {}).get(path, "")

        def worker():
            from Utils.mod_install import prepare_archive
            try:
                prepared = prepare_archive(
                    path, self._install_game, self._install_profile_dir,
                    log_fn=lambda m: self._op_log.emit(str(m)),
                    progress_fn=lambda d, t, ph=None: self._op_progress.emit(d, t, ph),
                    prebuilt_meta=meta,
                    preferred_name=forced_name,
                    on_need_prefix=self._make_need_prefix_cb())
            except Exception as exc:
                self._op_log.emit(f"Prepare error ({Path(path).name}): {exc}")
                prepared = None
            self._prepared_ready.emit(prepared)

        threading.Thread(target=worker, daemon=True).start()

    def _on_prepared_ready(self, prepared):
        if prepared is None:
            self._one_install_done.emit(None)
            return
        if prepared.is_fomod():
            # Open the wizard tab; finish on the user's selections (or cancel).
            # Hide the progress popup while the wizard is up (no work running).
            if self._progress_popup is not None:
                self._progress_popup.clear()
            from gui_qt.fomod_wizard_view import FomodWizardView

            self._fomod_done = False   # set True on finish/cancel to avoid double-fire

            def _finish(selections):
                if self._fomod_done:
                    return
                self._fomod_done = True
                self._tabs.close_tab("fomod_wizard")
                self._run_finish_install(prepared, selections)

            def _cancel():
                if self._fomod_done:
                    return
                self._fomod_done = True
                self._tabs.close_tab("fomod_wizard")
                self._op_log.emit(f"FOMOD install cancelled: {prepared.mod_name}")
                prepared.cleanup()
                self._notify(f"Install cancelled: {prepared.mod_name}", "info")
                self._one_install_done.emit(None)

            _sel_path = None
            _game_name = getattr(prepared.game, "name", "")
            if _game_name:
                from Utils.config_paths import get_fomod_selections_path
                _sel_path = get_fomod_selections_path(_game_name,
                                                      prepared.mod_name)
            _inst, _act, _loose = prepared.fomod_context
            view = FomodWizardView(prepared.fomod_config, prepared.fomod_base,
                                   prepared.mod_name, on_finish=_finish,
                                   on_cancel=_cancel,
                                   saved_selections=prepared.saved_fomod_selections,
                                   selections_path=_sel_path,
                                   installed_files=_inst, active_files=_act,
                                   loose_files=_loose)
            # Closing the tab (× / detached-window close) cancels the install.
            view.destroyed.connect(lambda *_: _cancel())
            self._tabs.open_tab(view, f"Install: {prepared.mod_name}",
                                key="fomod_wizard")
        elif prepared.is_bain():
            # BAIN package: open the sub-package picker tab; finish on the user's
            # selection (or cancel). Mirrors the FOMOD handshake above.
            if self._progress_popup is not None:
                self._progress_popup.clear()
            from gui_qt.bain_picker_view import BainPickerView

            self._bain_done = False   # guard against double-fire

            def _bfinish(result):
                if self._bain_done:
                    return
                self._bain_done = True
                self._tabs.close_tab("bain_picker")
                if result is None:
                    self._op_log.emit(
                        f"BAIN install cancelled: {prepared.mod_name}")
                    prepared.cleanup()
                    self._notify(f"Install cancelled: {prepared.mod_name}", "info")
                    self._one_install_done.emit(None)
                    return
                self._run_finish_install(prepared, None, bain_selections=result)

            view = BainPickerView(
                prepared.bain_subpkgs, prepared.bain_root, prepared.mod_name,
                on_done=_bfinish,
                readme_text=getattr(prepared, "readme_text", None),
                saved_selections=getattr(prepared, "saved_bain_selections", None))
            # Closing the tab (× / detached-window close) cancels the install.
            view.destroyed.connect(lambda *_: _bfinish(None))
            self._tabs.open_tab(view, f"Install: {prepared.mod_name}",
                                key="bain_picker")
        else:
            self._run_finish_install(prepared, None)

    def _run_finish_install(self, prepared, selections, bain_selections=None):
        import threading

        # Quick Update (forced folder name) auto-confirms Replace-All — pass
        # on_exists=None so finish_install silently replaces the existing folder
        # instead of raising the Mod-Already-Exists overlay (Tk parity).
        _forced = str(getattr(prepared, "archive", "")) in \
            getattr(self, "_install_preferred", {})
        exists_cb = None if _forced else self._make_exists_cb()

        def worker():
            from Utils.mod_install import finish_install
            try:
                name = finish_install(
                    prepared, selections,
                    log_fn=lambda m: self._op_log.emit(str(m)),
                    progress_fn=lambda d, t, ph=None: self._op_progress.emit(d, t, ph),
                    on_exists=exists_cb,
                    bain_selections=bain_selections)
            except Exception as exc:
                self._op_log.emit(f"Install error ({prepared.mod_name}): {exc}")
                name = None
            if name:
                self._maybe_clear_archive(prepared)
            self._one_install_done.emit(name)

        threading.Thread(target=worker, daemon=True).start()

    def _maybe_clear_archive(self, prepared):
        """Delete the source archive after a successful install, honouring the
        'Clear archive after install' / 'Keep FOMOD archives' settings (Tk
        parity). Runs on the install worker thread; failures are non-fatal."""
        try:
            from Utils.ui_config import (
                load_clear_archive_after_install, load_keep_fomod_archives)
            if not load_clear_archive_after_install():
                return
            if prepared.is_fomod() and load_keep_fomod_archives():
                return
            archive = getattr(prepared, "archive", None)
            if archive is None:
                return
            from Nexus.nexus_download import delete_archive_and_sidecar
            from pathlib import Path as _P
            delete_archive_and_sidecar(_P(archive))
            self._op_log.emit(f"Removed archive: {_P(archive).name}")
        except Exception as exc:
            self._op_log.emit(f"Archive cleanup skipped: {exc}")

    def _on_one_install_done(self, name):
        if name:
            # Optional post-install rename prompt (Tk parity). Modal, before the
            # next queued install — keeps one dialog at a time.
            name = self._maybe_prompt_rename(name)
            self._install_ok.append(name)
            # Change Version landed a different-named version → offer to remove
            # the previous version (Tk parity). One-shot per queue.
            prev = getattr(self, "_install_prev_name", None)
            if prev and name != prev:
                self._install_prev_name = None   # don't re-prompt for later items
                self._maybe_prompt_remove_previous(prev, name)
        self._install_next()   # continue the queue

    def _maybe_prompt_remove_previous(self, old_name: str, new_name: str):
        """Show the borderless 'Remove previous version?' overlay if both the old
        and new mods exist. Remove → the new mod inherits the old one's modlist
        position + enabled state, then the old mod is removed."""
        staging = self._gs.staging_dir()
        if staging is None:
            return
        if not (staging / old_name).is_dir() or not (staging / new_name).is_dir():
            return
        from gui_qt.remove_previous_overlay import RemovePreviousOverlay

        def _done(result):
            if result == "remove":
                self._remove_previous_version(old_name, new_name)

        RemovePreviousOverlay.show_over(self, old_name, new_name, _done)

    def _remove_previous_version(self, old_name: str, new_name: str):
        """New mod inherits old's modlist slot + enabled state; old is removed."""
        try:
            from Utils.modlist import read_modlist, write_modlist
            from Utils.mod_remove import remove_mods
            pdir = self._gs.profile_dir()
            game = self._gs.game
            if pdir is None or game is None:
                return
            ml = pdir / "modlist.txt"
            entries = read_modlist(ml)
            old_e = next((e for e in entries if e.name == old_name), None)
            new_e = next((e for e in entries if e.name == new_name), None)
            if new_e is not None:
                # New inherits old's enabled state + position, and the OLD entry
                # is dropped here (remove_mods intentionally does NOT touch
                # modlist.txt — it leaves the row for the caller to remove).
                if old_e is not None:
                    new_e.enabled = old_e.enabled
                # Pull the new entry out first, THEN locate old's slot in the
                # remaining list, drop old, and insert new there (so new lands
                # exactly where old was).
                rest = [e for e in entries if e.name != new_name]
                idx = next((i for i, e in enumerate(rest)
                            if e.name == old_name), 0)
                rest = [e for e in rest if e.name != old_name]
                rest.insert(min(idx, len(rest)), new_e)
                write_modlist(ml, rest)
            # Delete the old mod's files/plugins/index (NOT its modlist row —
            # already dropped above).
            remove_mods(game, pdir, [old_name],
                        log_fn=lambda m: self._append_log(f"[remove] {m}"))
        except Exception as exc:
            self._append_log(f"[install] remove-previous failed: {exc}")
        self._reload_modlist()
        self._rebuild_conflicts_async()

    def _maybe_prompt_rename(self, name: str) -> str:
        """If 'Rename mod after install' is on, prompt for a new name and rename
        the mod (staging folder + index + modlist entry). Returns the final name
        (unchanged if the user cancels or the rename fails)."""
        try:
            from Utils.ui_config import load_rename_mod_after_install
            if not load_rename_mod_after_install():
                return name
        except Exception:
            return name
        from PySide6.QtWidgets import QInputDialog
        new, ok = QInputDialog.getText(
            self, "Rename mod", "New name for the installed mod:", text=name)
        if not ok or not new.strip():
            return name
        renamed = self._rename_mod_on_disk(name, new.strip())
        return renamed or name

    def _rename_mod_on_disk(self, old_name: str, new_name: str) -> str | None:
        """Rename a mod: staging folder → new, modindex entry, modlist entry,
        then reload. Returns the sanitised new name on success, else None.
        Mirrors the Tk modlist_panel.rename_mod_by_name operation."""
        from gui.mod_name_utils import sanitize_mod_folder_name
        new_name = sanitize_mod_folder_name(new_name)
        if not old_name or not new_name or old_name == new_name:
            return None
        staging = self._gs.staging_dir()
        if staging is None:
            return None
        old_folder = staging / old_name
        new_folder = staging / new_name
        if new_folder.exists():
            self._notify(f"A mod named '{new_name}' already exists.", "warning")
            return None
        try:
            if old_folder.is_dir():
                old_folder.rename(new_folder)
        except OSError as exc:
            self._notify(f"Rename failed: {exc}", "warning")
            return None
        # Keep the persistent mod index in sync (avoids a full rescan).
        try:
            from Utils.filemap import rename_in_mod_index
            from Utils.ui_config import load_normalize_folder_case
            idx_path = staging.parent / "modindex.bin"
            rename_in_mod_index(idx_path, old_name, new_name,
                                normalize_folder_case=load_normalize_folder_case())
        except Exception:
            pass
        # Re-key the name-keyed per-mod state (strip prefixes, disabled plugins,
        # excluded files, notes) — Tk parity: _migrate_mod_name_state.
        try:
            from Utils.mod_rename import migrate_mod_state
            migrate_mod_state(self._gs.profile_dir(), old_name, new_name,
                              log_fn=self._append_log)
        except Exception as exc:
            print(f"[gui_qt] rename state migration failed: {exc}", flush=True)
        # Update the modlist entry by name, persist, and reload everything.
        m = self._modlist_model
        for r in range(m.rowCount()):
            e = m.entry(r)
            if not e.is_separator and e.name == old_name:
                e.name = new_name
                break
        m.save()
        self._reload_modlist()
        self._notify(f"Renamed to '{new_name}'.", "info")
        return new_name

    def _on_install_done(self, ok: int, total: int, names):
        self._install_running = False
        if hasattr(self, "_install_btn"):
            self._install_btn.setEnabled(True)
        if self._progress_popup is not None:
            QTimer.singleShot(1200, self._progress_popup.clear)
        self._reload_modlist()
        self._reload_plugins()
        # Re-flag Reinstall in the Downloads tab now that meta.ini changed.
        if hasattr(self, "_downloads_view"):
            self._downloads_view.mark_dirty()
        # A batch owner (Quick Update) reports its own summary; skip the generic
        # install toast and hand control back to it.
        cb = getattr(self, "_install_all_done_cb", None)
        self._install_all_done_cb = None
        if cb is not None:
            cb(ok, total, names)
            return
        if ok == total and ok > 0:
            if ok == 1:
                self._notify(f"Installed {names[0]}", "success")
            else:
                self._notify(f"Installed {ok} mods", "success")
        elif ok > 0:
            self._notify(f"Installed {ok} of {total} mods — see log for failures.",
                         "warning")
        else:
            self._notify("Install failed — see log.", "error")

    def _build_modlist(self) -> QWidget:
        self._modlist_model = ModListModel([])
        self._modlist_model.enabled_changed.connect(self._on_mods_enabled_changed)
        self._modlist_model.save_failed.connect(
            lambda msg: self._notify(msg, "error"))
        self._modlist_view = ModListView(self._modlist_model)
        # Column-sort rebuilds reorder rows in place (layoutChanged); the
        # search/filter hidden sets are display-row-indexed and must be
        # recomputed. Connected AFTER the view (its own layoutChanged hooks
        # must clear the applied-hidden cache + spanning first).
        self._modlist_model.layoutChanged.connect(
            self._on_modlist_layout_changed)
        return self._modlist_view

    def _on_modlist_layout_changed(self, *_a):
        self._apply_modlist_search()
        self._apply_modlist_filters()

    def _on_mods_enabled_changed(self, changes):
        """Mods were toggled (checkbox / Enable-Disable all / context menu) —
        mirror the change into plugins.txt + loadorder.txt (Tk parity:
        _sync_plugins_for_toggle) and refresh the Plugins tab."""
        from Utils.plugin_sync import sync_plugins_for_mods
        try:
            wrote = sync_plugins_for_mods(
                self._gs.game, self._gs.profile_dir(), self._gs.staging_dir(),
                list(changes or []), log_fn=self._append_log)
        except Exception as exc:
            print(f"[gui_qt] plugin sync failed: {exc}", flush=True)
            return
        if wrote:
            self._reload_plugins()

    def _build_modlist_area(self) -> QWidget:
        """Modlist column: the list + its tool footer (buttons + search) stacked
        vertically, with a collapsible filter side panel docked on the left (Tk
        parity — the filter panel pushes the column right, not an overlay)."""
        # The list + footer share one vertical column. A hidden 'new profile'
        # bar sits at the very top (row 0, Tk parity) and appears when the user
        # picks 'Add new profile…'.
        col = QWidget()
        cv = QVBoxLayout(col)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        from gui_qt.new_profile_bar import NewProfileBar
        self._new_profile_bar = NewProfileBar(
            on_create=self._on_new_profile_create,
            on_cancel=lambda: None)
        cv.addWidget(self._new_profile_bar)
        cv.addWidget(self._build_modlist(), 1)
        cv.addWidget(self._modlist_footer())

        area = QWidget()
        h = QHBoxLayout(area)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self._modlist_filter_panel = self._build_modlist_filter_panel()
        self._modlist_filter_panel.setVisible(False)
        h.addWidget(self._modlist_filter_panel)
        # The Mod Files filter panel shares this window-left slot so it opens in
        # the same place as the modlist filters (just a different filter set).
        self._mod_files_filter_panel = self._build_mod_files_filter_panel()
        self._mod_files_filter_panel.setVisible(False)
        h.addWidget(self._mod_files_filter_panel)
        # The Data filter panel shares the same window-left slot.
        self._data_filter_panel = self._build_data_filter_panel()
        self._data_filter_panel.setVisible(False)
        h.addWidget(self._data_filter_panel)
        # The Downloads filter panel shares the slot too.
        self._downloads_filter_panel = self._build_downloads_filter_panel()
        self._downloads_filter_panel.setVisible(False)
        h.addWidget(self._downloads_filter_panel)
        # The Text Files filter panel shares the slot too.
        self._text_files_filter_panel = self._build_text_files_filter_panel()
        self._text_files_filter_panel.setVisible(False)
        h.addWidget(self._text_files_filter_panel)
        # The Plugins filter panel shares the slot too.
        self._plugin_filter_panel = self._build_plugin_filter_panel()
        self._plugin_filter_panel.setVisible(False)
        h.addWidget(self._plugin_filter_panel)
        h.addWidget(col, 1)
        return area

    def _build_text_files_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        panel = FilterSidePanel(self._text_files_view.filter_spec(), title="Filters")
        panel.changed.connect(self._on_text_files_filter_changed)
        panel.close_requested.connect(self._toggle_text_files_filters)
        self._text_files_view.filetypes_changed.connect(
            self._sync_text_files_filter_list)
        return panel

    def _toggle_text_files_filters(self):
        panel = self._text_files_filter_panel
        show = not panel.isVisible()
        if show:
            self._modlist_filter_panel.setVisible(False)
            self._mod_files_filter_panel.setVisible(False)
            self._data_filter_panel.setVisible(False)
            self._downloads_filter_panel.setVisible(False)
            self._plugin_filter_panel.setVisible(False)
            panel.setVisible(True)
            self._sync_text_files_filter_list()
        else:
            panel.setVisible(False)

    def _on_text_files_filter_changed(self, state: dict):
        self._text_files_view.apply_filter_state(state)
        active = self._text_files_filter_panel.any_active()
        b = getattr(self, "_tf_filters_btn", None)
        if b is not None:
            b.setProperty("active", active)
            b.style().unpolish(b); b.style().polish(b)

    def _build_plugin_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        from gui_qt.modlist_filter import PLUGIN_STATUS_FILTERS
        items = [(key, label, True) for key, label in PLUGIN_STATUS_FILTERS]
        spec = [{"title": "By status", "type": "checks", "items": items}]
        panel = FilterSidePanel(spec, title="Filters")
        panel.changed.connect(self._on_plugin_filter_changed)
        panel.close_requested.connect(self._toggle_plugin_filters)
        self._plugin_filter_state: dict = {}
        return panel

    def _toggle_plugin_filters(self):
        panel = self._plugin_filter_panel
        show = not panel.isVisible()
        if show:
            self._modlist_filter_panel.setVisible(False)
            self._mod_files_filter_panel.setVisible(False)
            self._data_filter_panel.setVisible(False)
            self._downloads_filter_panel.setVisible(False)
            self._text_files_filter_panel.setVisible(False)
            self._plugin_filter_panel.setVisible(False)
            panel.setVisible(True)
            self._apply_plugin_filters()
        else:
            panel.setVisible(False)

    def _on_plugin_filter_changed(self, state: dict):
        self._plugin_filter_state = state
        self._apply_plugin_filters()
        active = self._plugin_filter_panel.any_active()
        b = getattr(self, "_plugin_filters_btn", None)
        if b is not None:
            b.setProperty("active", active)
            b.style().unpolish(b); b.style().polish(b)

    def _apply_plugin_filters(self):
        """Push the filter-hidden row set to the plugin view (composes with the
        plugin search via the view's search/filter union)."""
        if not hasattr(self, "_plugin_view"):
            return
        from gui_qt.modlist_filter import plugin_filter_hidden_rows
        state = getattr(self, "_plugin_filter_state", None) or {}
        hide = plugin_filter_hidden_rows(self._plugin_model._rows, state)
        self._plugin_view.set_filter_hidden(hide)

    def _sync_text_files_filter_list(self):
        if not self._text_files_filter_panel.isVisible():
            return
        self._text_files_filter_panel.set_dynamic_items(
            "filetypes", self._text_files_view.filetype_items())

    def _build_downloads_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        panel = FilterSidePanel(self._downloads_view.filter_spec(), title="Filters")
        panel.changed.connect(self._on_downloads_filter_changed)
        panel.close_requested.connect(self._toggle_downloads_filters)
        self._downloads_view.filetypes_changed.connect(
            self._sync_downloads_filter_list)
        return panel

    def _toggle_downloads_filters(self):
        panel = self._downloads_filter_panel
        show = not panel.isVisible()
        if show:
            self._modlist_filter_panel.setVisible(False)
            self._mod_files_filter_panel.setVisible(False)
            self._data_filter_panel.setVisible(False)
            self._text_files_filter_panel.setVisible(False)
            self._plugin_filter_panel.setVisible(False)
            panel.setVisible(True)
            self._sync_downloads_filter_list()
        else:
            panel.setVisible(False)

    def _on_downloads_filter_changed(self, state: dict):
        self._downloads_view.apply_filter_state(state)
        active = self._downloads_filter_panel.any_active()
        b = getattr(self, "_dl_filters_btn", None)
        if b is not None:
            b.setProperty("active", active)
            b.style().unpolish(b); b.style().polish(b)

    def _sync_downloads_filter_list(self):
        if not self._downloads_filter_panel.isVisible():
            return
        self._downloads_filter_panel.set_dynamic_items(
            "filetypes", self._downloads_view.filetype_items())
        self._downloads_filter_panel.set_dynamic_items(
            "locations", self._downloads_view.location_items())

    def _build_data_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        from gui_qt.data_view import DataView
        panel = FilterSidePanel(DataView.filter_spec(), title="Filters")
        panel.changed.connect(self._on_data_filter_changed)
        panel.close_requested.connect(self._toggle_data_filters)
        self._data_view.filetypes_changed.connect(self._sync_data_filter_list)
        return panel

    def _toggle_data_filters(self):
        """Open/close the Data filter panel in the window-left slot (does NOT hide
        the plugins column — the Data tree lives there)."""
        panel = self._data_filter_panel
        show = not panel.isVisible()
        if show:
            self._modlist_filter_panel.setVisible(False)
            self._mod_files_filter_panel.setVisible(False)
            self._downloads_filter_panel.setVisible(False)
            self._text_files_filter_panel.setVisible(False)
            self._plugin_filter_panel.setVisible(False)
            panel.setVisible(True)
            self._sync_data_filter_list()
        else:
            panel.setVisible(False)

    def _on_data_filter_changed(self, state: dict):
        self._data_view.apply_filter_state(state)
        active = self._data_filter_panel.any_active()
        b = getattr(self, "_data_filters_btn", None)
        if b is not None:
            b.setProperty("active", active)
            b.style().unpolish(b); b.style().polish(b)

    def _sync_data_filter_list(self):
        if not self._data_filter_panel.isVisible():
            return
        self._data_filter_panel.set_dynamic_items(
            "filetypes", self._data_view.filetype_items())

    def _build_mod_files_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        from gui_qt.mod_files_view import ModFilesView
        panel = FilterSidePanel(ModFilesView.filter_spec(), title="Filters")
        panel.changed.connect(self._on_mod_files_filter_changed)
        panel.close_requested.connect(self._toggle_mod_files_filters)
        # Keep the panel's file-type list + Pack/Unpack enablement in sync.
        self._mod_files_view.filetypes_changed.connect(
            self._sync_mod_files_filter_list)
        self._mod_files_view.mod_changed.connect(
            lambda _n: self._update_mf_footer_buttons())
        return panel

    def _toggle_mod_files_filters(self):
        """Open/close the Mod Files filter panel in the window-left slot. Unlike
        the modlist filter, this does NOT hide the plugins column — the Mod Files
        tree lives there, so hiding it would hide what you're filtering."""
        panel = self._mod_files_filter_panel
        show = not panel.isVisible()
        if show:
            self._modlist_filter_panel.setVisible(False)  # share the slot
            self._data_filter_panel.setVisible(False)
            self._downloads_filter_panel.setVisible(False)
            self._text_files_filter_panel.setVisible(False)
            self._plugin_filter_panel.setVisible(False)
            panel.setVisible(True)
            self._sync_mod_files_filter_list()
        else:
            panel.setVisible(False)

    def _on_mod_files_filter_changed(self, state: dict):
        self._mod_files_view.apply_filter_state(state)
        active = self._mod_files_filter_panel.any_active()
        b = getattr(self, "_mf_filters_btn", None)
        if b is not None:
            b.setProperty("active", active)
            b.style().unpolish(b); b.style().polish(b)

    def _sync_mod_files_filter_list(self):
        if not self._mod_files_filter_panel.isVisible():
            return
        self._mod_files_filter_panel.set_dynamic_items(
            "filetypes", self._mod_files_view.filetype_items())

    def _update_mf_footer_buttons(self):
        ok = self._mod_files_view.has_mod()
        for attr in ("_mf_pack_btn", "_mf_unpack_btn"):
            b = getattr(self, attr, None)
            if b is not None:
                b.setEnabled(ok)

    def _on_mf_expand_clicked(self):
        expanded = self._mod_files_view._toggle_expand_all()
        self._mf_expand_btn.setText("⊟ Collapse all" if expanded
                                    else "⊞ Expand all")

    def _build_modlist_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        from gui_qt.modlist_filter import STATUS_FILTERS

        # Filters whose backing data the Qt side doesn't build yet — shown but
        # disabled (greyed) so the panel is complete and they light up later.
        # (FOMOD/BAIN come from meta.is_fomod/is_bain; conflicts, plugins, BSA,
        #  PBR, updates, categories, file types are all wired.)
        _UNWIRED = {
            "filter_missing_reqs", "filter_has_disabled_plugins",
            "filter_has_notes",
        }
        items = [(key, label, key not in _UNWIRED)
                 for key, label in STATUS_FILTERS]
        spec = [
            {"title": "By status", "type": "checks", "items": items},
            {"title": "By category", "type": "dynamic", "id": "categories"},
            {"title": "By file type", "type": "dynamic", "id": "filetypes"},
        ]
        panel = FilterSidePanel(spec, title="Filters")
        panel.changed.connect(self._on_modlist_filter_changed)
        panel.close_requested.connect(self._toggle_modlist_filters)
        self._modlist_filter_state: dict = {}
        self._modlist_filter_data = None
        return panel

    def _toggle_modlist_filters(self):
        """Show/hide the modlist filter side panel (the Filters footer button).

        Mirrors Tk: opening the modlist filter auto-hides the plugins panel so
        the filter takes its space; closing restores the plugins panel only if
        it was visible when we opened."""
        panel = getattr(self, "_modlist_filter_panel", None)
        if panel is None:
            return
        show = not panel.isVisible()
        right = getattr(self, "_right_col", None)
        if show:
            # The Mod Files / Data filters share this slot — close them first.
            mfp = getattr(self, "_mod_files_filter_panel", None)
            if mfp is not None and mfp.isVisible():
                self._toggle_mod_files_filters()
            dfp = getattr(self, "_data_filter_panel", None)
            if dfp is not None and dfp.isVisible():
                self._toggle_data_filters()
            dlfp = getattr(self, "_downloads_filter_panel", None)
            if dlfp is not None and dlfp.isVisible():
                self._toggle_downloads_filters()
            tffp = getattr(self, "_text_files_filter_panel", None)
            if tffp is not None and tffp.isVisible():
                self._toggle_text_files_filters()
            self._filter_plugins_was_visible = bool(
                right is not None and right.isVisible())
            panel.setVisible(True)
            if right is not None and self._filter_plugins_was_visible:
                right.setVisible(False)
            self._rebuild_filter_data()
        else:
            panel.setVisible(False)
            if right is not None and getattr(
                    self, "_filter_plugins_was_visible", False):
                right.setVisible(True)
            self._filter_plugins_was_visible = False

    # ---- footer button handlers -----------------------------------------
    def _on_toggle_collapse_all(self):
        """Expand all / Collapse all separators (toggles based on current state)."""
        m = self._modlist_model
        if not m.collapsible_separator_names():
            return
        collapse = not m.any_collapsed()   # if any expanded → collapse all
        self._modlist_view.set_all_collapsed(collapse)
        self._refresh_footer_toggle_labels()

    def _on_toggle_enable_all(self):
        """Enable all / Disable all toggleable mods."""
        m = self._modlist_model
        enable = not m.all_mods_enabled()
        m.set_all_enabled(enable)
        self._refresh_footer_toggle_labels()
        self._notify("All mods enabled" if enable else "All mods disabled",
                     "info")

    def _refresh_footer_toggle_labels(self):
        """Keep the Expand/Collapse all + Enable/Disable all button text in sync
        with the list state (Tk parity)."""
        m = self._modlist_model
        eb = getattr(self, "_expand_all_btn", None)
        if eb is not None:
            has_seps = bool(m.collapsible_separator_names())
            eb.setText("Expand all" if (not has_seps or m.any_collapsed())
                       else "Collapse all")
        nb = getattr(self, "_enable_all_btn", None)
        if nb is not None:
            nb.setText("Disable all" if m.all_mods_enabled() else "Enable all")

    def _on_refresh_modlist(self):
        """Refresh: re-sync the mods folder, reload the modlist + plugins, and
        force a full index rescan (picks up files added/removed inside mods)."""
        from Utils.modlist import sync_modlist_with_mods_folder
        ml = self._gs.modlist_path()
        staging = self._gs.staging_dir()
        if ml is not None and staging is not None:
            try:
                sync_modlist_with_mods_folder(ml, staging)
            except Exception as exc:
                print(f"[gui_qt] modlist sync failed: {exc}", flush=True)
        self._reload_modlist(rescan_index=True)
        self._reload_plugins()
        self._refresh_footer_toggle_labels()
        self._notify("Modlist refreshed", "info")

    # ---- search boxes ----------------------------------------------------
    def _on_modlist_search(self, text: str):
        """Modlist search: hide rows whose mod name (or owning separator block)
        doesn't contain the query. Debounced to coalesce fast typing."""
        self._modlist_search_text = text
        t = getattr(self, "_modlist_search_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(120)
            t.timeout.connect(self._apply_modlist_search)
            self._modlist_search_timer = t
        t.start()

    def _apply_modlist_search(self):
        from gui_qt.modlist_filter import search_hidden_rows
        text = getattr(self, "_modlist_search_text", "")
        entries = self._modlist_model._entries
        active = bool((text or "").strip())
        self._modlist_view.set_search_hidden(
            search_hidden_rows(entries, text), active=active)

    def _on_plugin_search(self, text: str):
        """Plugins search: hide plugins whose name (or owning mod name) doesn't
        contain the query."""
        self._plugin_search_text = text
        t = getattr(self, "_plugin_search_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(120)
            t.timeout.connect(self._apply_plugin_search)
            self._plugin_search_timer = t
        t.start()

    def _apply_plugin_search(self):
        from gui_qt.modlist_filter import plugin_search_hidden_rows
        text = getattr(self, "_plugin_search_text", "")
        rows = self._plugin_model._rows
        owner = (self._conflict_data.plugin_owner
                 if getattr(self, "_conflict_data", None) else {})
        self._plugin_view.set_search_hidden(
            plugin_search_hidden_rows(rows, text, owner))

    def _on_modlist_filter_changed(self, state: dict):
        self._modlist_filter_state = state
        self._apply_modlist_filters()
        self._update_filters_btn_active()

    def _apply_modlist_filters(self):
        from gui_qt.modlist_filter import compute_hidden_rows
        state = getattr(self, "_modlist_filter_state", {}) or {}
        data = getattr(self, "_modlist_filter_data", None)
        if data is None:
            self._modlist_view.set_filter_hidden(set())
            return
        entries = self._modlist_model._entries
        hide = compute_hidden_rows(entries, state, data)
        self._modlist_view.set_filter_hidden(hide)

    def _update_filters_btn_active(self):
        """Tint the Filters footer button when any filter is active (Tk parity)."""
        btn = getattr(self, "_modlist_filters_btn", None)
        panel = getattr(self, "_modlist_filter_panel", None)
        if btn is None or panel is None:
            return
        btn.setProperty("active", panel.any_active())
        btn.style().unpolish(btn)
        btn.style().polish(btn)

    def _rebuild_filter_data(self):
        """(Re)build FilterData: the disk-derived parts (modindex read + BSA /
        plugin folder walks) run on a worker; _on_filter_data_ready assembles
        the FilterData and updates the panel on the UI thread. Runs after every
        conflict rebuild, so the scans must not block the UI."""
        panel = getattr(self, "_modlist_filter_panel", None)
        if panel is None:
            return
        import threading
        staging = self._gs.staging_dir()
        staging_parent = staging.parent if staging is not None else None
        plugin_exts = getattr(self._gs.game, "plugin_extensions", None)
        self._filter_data_gen += 1
        gen = self._filter_data_gen

        def worker():
            payload = None
            if staging_parent is not None:
                from gui_qt.modlist_filter import (
                    build_index_data, build_mods_with_bsa, build_mods_with_plugins,
                )
                try:
                    counts, mod_ft, pbr = build_index_data(staging_parent)
                    payload = {
                        "filetype_counts": counts,
                        "mod_filetypes": mod_ft,
                        "mods_with_pbr": pbr,
                        "mods_with_bsa": build_mods_with_bsa(staging_parent),
                        "mods_with_plugins": build_mods_with_plugins(
                            staging_parent, plugin_exts),
                    }
                except Exception as exc:
                    print(f"[gui_qt] filter data scan failed: {exc}", flush=True)
            self._filter_data_ready.emit(gen, payload)

        threading.Thread(target=worker, daemon=True,
                         name="filter-data").start()

    def _on_filter_data_ready(self, gen: int, payload):
        """UI thread: merge the worker's disk-derived sets with the in-memory
        meta/conflict state into FilterData, repopulate the panel, reapply."""
        if gen != self._filter_data_gen:
            return   # superseded by a newer rebuild
        panel = getattr(self, "_modlist_filter_panel", None)
        if panel is None:
            return
        from gui_qt.modlist_filter import FilterData
        cd = getattr(self, "_conflict_data", None)

        data = FilterData()
        if cd is not None:
            data.conflict_codes = self._loose_backend_codes(cd)
            data.bsa_conflict_codes = self._bsa_backend_codes(cd)
        data.mods_with_updates = set(getattr(self, "_mod_updates", set()))
        data.missing_reqs = set(getattr(self, "_mod_missing_reqs", set()))
        data.ignored_missing_reqs = set(getattr(self, "_ignored_missing_reqs", frozenset()))
        data.category_names = dict(getattr(self, "_mod_categories", {}))
        data.fomod_mods = set(getattr(self, "_mod_fomod", set()))
        data.bain_mods = set(getattr(self, "_mod_bain", set()))
        data.modified_mf_mods = self._build_modified_mf_mods()
        # Overlay the "modified in Mod Files" eye flag in the modlist Flags column.
        self._modlist_model.set_modified_mf(data.modified_mf_mods)
        if payload:
            data.filetype_counts = payload["filetype_counts"]
            data.mod_filetypes = payload["mod_filetypes"]
            data.mods_with_pbr = payload["mods_with_pbr"]
            data.mods_with_bsa = payload["mods_with_bsa"]
            data.mods_with_plugins = payload["mods_with_plugins"]
        self._modlist_filter_data = data

        # Repopulate dynamic lists.
        cats = sorted({(c or "") for c in data.category_names.values()} | {""},
                      key=lambda c: ("(Uncategorized)" if c == "" else c).lower())
        panel.set_dynamic_items("categories", [
            (c, "(Uncategorized)" if c == "" else c, None) for c in cats])
        fts = sorted(data.filetype_counts.items(), key=lambda kv: kv[0])
        panel.set_dynamic_items("filetypes", [
            (ext, ext, count) for ext, count in fts])

        # Relabel / re-enable game-specific filters, then reapply.
        self._refresh_filter_game_specific()
        self._apply_modlist_filters()

    def _build_modified_mf_mods(self) -> set:
        """Mods with Mod Files tab modifications — any excluded file OR a strip
        prefix (Tk `_mod_is_modified_in_mf`)."""
        pdir = self._gs.profile_dir()
        if pdir is None:
            return set()
        out: set[str] = set()
        try:
            from Utils.profile_state import (
                read_excluded_mod_files, read_mod_strip_prefixes)
            for mod, keys in (read_excluded_mod_files(pdir, None) or {}).items():
                if keys:
                    out.add(mod)
            for mod, prefixes in (read_mod_strip_prefixes(pdir, None) or {}).items():
                if any(p for p in prefixes):
                    out.add(mod)
        except Exception:
            pass
        return out

    def _refresh_filter_game_specific(self):
        """Relabel BSA→BA2 + enable/disable PGPatcher per the active game."""
        panel = getattr(self, "_modlist_filter_panel", None)
        g = self._gs.game
        if panel is None:
            return
        archive_exts = getattr(g, "archive_extensions", None) if g else None
        if archive_exts and ".ba2" in archive_exts:
            panel.set_check_label("filter_has_bsa", "Mods with BA2 archives")
        else:
            panel.set_check_label("filter_has_bsa", "Mods with BSA archives")
        # PGPatcher (PBR) is Skyrim SE only.
        is_sse = bool(g and getattr(g, "nexus_game_domain", "")
                      == "skyrimspecialedition")
        panel.set_check_enabled("filter_has_pbr", is_sse)

    def _loose_backend_codes(self, cd) -> dict:
        """Map the display loose_codes back to backend CONFLICT_* codes the
        engine filters on (DISP 1/-1/2/3 → WINS/LOSES/PARTIAL/FULL)."""
        from Utils.filemap import (
            CONFLICT_WINS, CONFLICT_LOSES, CONFLICT_PARTIAL, CONFLICT_FULL)
        m = {1: CONFLICT_WINS, -1: CONFLICT_LOSES,
             2: CONFLICT_PARTIAL, 3: CONFLICT_FULL}
        return {n: m[c] for n, c in (cd.loose_codes or {}).items() if c in m}

    def _bsa_backend_codes(self, cd) -> dict:
        """BSA codes from ConflictData are 1/-1/2 (win/lose/mixed). Map to
        backend codes for the engine (mixed→PARTIAL)."""
        from Utils.filemap import (
            CONFLICT_WINS, CONFLICT_LOSES, CONFLICT_PARTIAL)
        m = {1: CONFLICT_WINS, -1: CONFLICT_LOSES, 2: CONFLICT_PARTIAL}
        return {n: m[c] for n, c in (cd.bsa_codes or {}).items() if c in m}

    def _reload_modlist(self, rescan_index: bool = False):
        """Load the active game/profile's modlist + metadata into the model.
        rescan_index=True forces the conflict rebuild to rescan the index from
        disk (Refresh button)."""
        from Utils.modlist import read_modlist
        from gui_qt.modlist_data import read_meta_for_entries

        ml_path = self._gs.modlist_path()
        staging = self._gs.staging_dir()
        entries = read_modlist(ml_path) if (ml_path and ml_path.is_file()) else []

        versions = installed = flags = {}
        self._mod_categories: dict[str, str] = {}
        self._mod_updates: set[str] = set()
        self._mod_fomod: set[str] = set()
        self._mod_bain: set[str] = set()
        self._mod_missing_reqs: set[str] = set()
        self._ignored_missing_reqs: frozenset[str] = frozenset()
        if entries and staging is not None:
            pdir = self._gs.profile_dir()
            if pdir is not None:
                try:
                    from Utils.profile_state import read_ignored_missing_requirements
                    self._ignored_missing_reqs = frozenset(
                        read_ignored_missing_requirements(pdir))
                except Exception:
                    self._ignored_missing_reqs = frozenset()
            (versions, installed, flags,
             self._mod_categories, self._mod_updates,
             self._mod_fomod, self._mod_bain,
             self._mod_missing_reqs) = read_meta_for_entries(
                entries, staging, self._ignored_missing_reqs,
                profile_dir=self._gs.profile_dir(),
                is_bg3=(getattr(self._gs.game, "game_id", "") == "baldurs_gate_3"))

        # Meta dicts BEFORE set_entries — set_entries re-derives the display
        # order, and an active column sort (version/installed/category) must
        # see the fresh data.
        self._modlist_model._versions = versions
        self._modlist_model._installed = installed
        self._modlist_model._categories = self._mod_categories
        self._modlist_model.set_entries(entries)
        self._modlist_model.set_flags(flags)
        self._modlist_model.set_notes(self._read_mod_notes())
        self._modlist_model.set_conflicts({}, {})   # clear stale; recomputed async
        # Persist edits back to this modlist; rebuild conflicts after each save.
        self._modlist_model.modlist_path = ml_path
        self._modlist_model.on_saved = self._rebuild_conflicts_async
        self._modlist_view.staging_dir = staging
        self._modlist_view.profile_dir = self._gs.profile_dir()
        self._modlist_view.game = self._gs.game
        # Right-click "Check Updates" reaches the window through this callback.
        self._modlist_view.on_check_updates = self._on_check_updates
        # Change Version: right-click item + clicking the update flag icon.
        self._modlist_view.on_change_version = self._open_change_version_tab
        self._modlist_view.on_flag_clicked = self._on_modlist_flag_clicked
        # Missing Requirements: right-click item + clicking the ⚠ flag icon.
        self._modlist_view.on_missing_reqs = self._open_missing_reqs_tab
        # Quick Update: right-click on update-flagged mods (premium direct DL).
        self._modlist_view.on_quick_update = self._quick_update_mods
        # Show Conflicts: right-click item.
        self._modlist_view.on_show_conflicts = self._open_show_conflicts_tab
        # Root-Folder toggle wrote meta.ini → refresh the root flag immediately,
        # then rescan those mods + rebuild filemap (the index caches strip-applied
        # vs verbatim paths, so it's now stale; the rebuild also refreshes the
        # filemap-derived root-rule/pre-RTX overlays).
        self._modlist_view.on_root_folder_changed = \
            lambda names: (self._refresh_modlist_flags(names),
                           self._rebuild_conflicts_async(rescan_index=True))
        # Endorse/Abstain: needs the shared Nexus API + a flag refresh.
        self._modlist_view.on_endorse = self._on_modlist_endorse
        # A saved note may change the note flag — light flag-only refresh.
        self._modlist_view.on_notes_changed = self._refresh_modlist_flags
        # Copy/Move to profile: worker copy + collision overlay + (move) remove.
        self._modlist_view.on_copy_to_profile = self._copy_mods_to_profile
        # Rename (context menu): folder + modindex + per-mod state migration.
        self._modlist_view.on_rename_mod = self._rename_mod_on_disk
        # Separator settings (colour + deploy override): open the scoped tab;
        # rename/remove migrate/drop the stored colour + deploy entries.
        self._modlist_view.on_separator_settings = self._open_sep_settings_tab
        self._modlist_view.on_separator_renamed = self._on_separator_renamed
        self._modlist_view.on_separators_removed = self._on_separators_removed
        # Enabling the Size column scans mod folder sizes on demand (Tk parity:
        # only walk the disk when Size is actually shown).
        self._modlist_view.on_sizes_requested = self._apply_modlist_sizes
        self._modlist_model._sizes = {}
        if not self._modlist_view.isColumnHidden(COL_SIZE):
            self._apply_modlist_sizes()
        self._modlist_view.load_separator_state()
        # Point the Mod Files tab at this game/profile (index next to filemap).
        if hasattr(self, "_mod_files_view"):
            idx = (staging.parent / "modindex.bin") if staging is not None else None
            self._mod_files_view.configure(
                self._gs.game, self._gs.profile_dir(), idx)
            self._mod_files_view.show_mod(None)
        # Point the Data tab at this game/profile (filemap.txt + modindex.bin).
        if hasattr(self, "_data_view"):
            fm = (staging.parent / "filemap.txt") if staging is not None else None
            idx2 = (staging.parent / "modindex.bin") if staging is not None else None
            self._data_view.configure(
                self._gs.game, self._gs.profile_dir(), fm, idx2)
        # Point the Downloads tab at this game (game-name getter for cache dir).
        if hasattr(self, "_downloads_view"):
            self._downloads_view.configure(
                self._gs.game, lambda: self._gs.game_name)
        # Point the Text Files tab at this game/profile.
        if hasattr(self, "_text_files_view"):
            fm3 = (staging.parent / "filemap.txt") if staging is not None else None
            self._text_files_view.configure(
                self._gs.game, self._gs.profile_dir(), fm3, staging)
        self._refresh_footer_toggle_labels()
        # Re-apply an active search against the fresh row indices.
        self._apply_modlist_search()
        self._refresh_modlist_stats()
        print(f"[gui_qt] modlist: {ml_path} ({len(entries)} entries)")

        # The Downloads tab reflects the active profile's STAGING folder (which
        # changes with the profile), so refresh it on every reload — even for an
        # empty profile, where the conflict rebuild below is skipped.
        if hasattr(self, "_data_view"):
            self._data_view.mark_dirty()
        if hasattr(self, "_downloads_view"):
            self._downloads_view.mark_dirty()
        if hasattr(self, "_text_files_view"):
            self._text_files_view.mark_dirty()
        # The Nexus browser (if open) shows Install/Reinstall per the active
        # profile's installed mods — refresh on every modlist reload (covers
        # profile/game change AND post-install).
        nv = getattr(self, "_nexus_view", None)
        if nv is not None:
            nv._game = self._gs.game
            nv.refresh_installed()

        if entries:
            self._rebuild_conflicts_async(rescan_index=rescan_index)

    def _apply_modlist_sizes(self):
        """Scan mod folder sizes and push them to the model. Called on reload
        when the Size column is visible, and when the user enables Size from the
        column menu (the disk walk is skipped while Size stays hidden)."""
        staging = self._gs.staging_dir()
        if staging is None:
            return
        from gui_qt.modlist_data import compute_sizes
        entries = self._modlist_model.natural_entries()
        sizes, size_bytes = compute_sizes(entries, staging)
        self._modlist_model.set_sizes(sizes, size_bytes)

    def _refresh_modlist_stats(self):
        """Update the modlist footer stat pills: enabled / disabled mod counts.
        Both come straight from the model, so this is instant."""
        bar = getattr(self, "_modlist_stats", None)
        if bar is None:
            return
        entries = [self._modlist_model.entry(r)
                   for r in range(self._modlist_model.rowCount())]
        enabled = sum(1 for e in entries if not e.is_separator and e.enabled)
        disabled = sum(1 for e in entries if not e.is_separator and not e.enabled)
        bar.set_stats([("Enabled", enabled), ("Disabled", disabled)])

    def _refresh_modlist_flags(self, names=None):
        """Re-read the meta/profile-derived flags and push them into the model
        WITHOUT the full modlist reset (keeps selection + scroll). Call after any
        action that changes a flag (endorse, root toggle, note edit). When
        *names* is given, only those mods' metas are re-read and merged into the
        existing state (endorsing one mod shouldn't re-read 500 meta files).
        The filemap-derived overlays (pre-RTX / root-rule) refresh via the
        conflict-ready path instead."""
        from gui_qt.modlist_data import read_meta_for_entries
        staging = self._gs.staging_dir()
        if staging is None:
            return
        subset = set(names) if names else None
        entries = [e for r in range(self._modlist_model.rowCount())
                   if (e := self._modlist_model.entry(r)) is not None
                   and (subset is None or e.name in subset)]
        try:
            (_v, _i, flags, categories, updates, fomod, bain,
             missing_reqs) = read_meta_for_entries(
                entries, staging, self._ignored_missing_reqs,
                profile_dir=self._gs.profile_dir(),
                is_bg3=(getattr(self._gs.game, "game_id", "") == "baldurs_gate_3"))
        except Exception:
            return
        if subset is None:
            (self._mod_categories, self._mod_updates, self._mod_fomod,
             self._mod_bain, self._mod_missing_reqs) = (
                categories, updates, fomod, bain, missing_reqs)
        else:
            # Merge: clear the requested names first (a cleared flag won't
            # appear in the subset result), then overlay the fresh values.
            flags = {**{n: b for n, b in self._modlist_model._flags.items()
                        if n not in subset}, **flags}
            self._mod_categories = {**{n: c for n, c in self._mod_categories.items()
                                       if n not in subset}, **categories}
            for cur, fresh in ((self._mod_updates, updates),
                               (self._mod_fomod, fomod),
                               (self._mod_bain, bain),
                               (self._mod_missing_reqs, missing_reqs)):
                cur -= subset
                cur |= fresh
        self._modlist_model.set_flags(flags)
        # Re-point the model at the (possibly rebuilt) categories dict and
        # re-sort if the Category column drives the current sort.
        self._modlist_model._categories = self._mod_categories
        self._modlist_model._resort_if_key("category")
        self._modlist_model.set_notes(self._read_mod_notes())

    def _read_mod_notes(self) -> dict:
        """Per-mod note text for the active profile (Note flag tooltip). {} if
        none / no profile."""
        pdir = self._gs.profile_dir()
        if pdir is None:
            return {}
        try:
            from Utils.profile_state import read_mod_notes
            return read_mod_notes(pdir)
        except Exception:
            return {}

    def _refresh_plugin_stats(self):
        """Update the plugins footer stat pills: total / ESL / non-ESL. All the
        data is in-memory on the plugin model, so this is instant."""
        bar = getattr(self, "_plugin_stats", None)
        if bar is None:
            return
        from gui_qt.modlist_data import compute_plugin_stats
        s = compute_plugin_stats(self._plugin_model._rows)
        bar.set_stats([("Plugins", s["total"]),
                       ("ESL", s["esl"]), ("Non-ESL", s["non_esl"])])

    def _reload_plugins(self):
        """Load the active game/profile's plugins into the Plugins tab."""
        from gui_qt.plugin_state import load_plugins
        rows = load_plugins(self._gs.game, self._gs.profile)
        self._plugin_model.set_rows(rows, game=self._gs.game,
                                    profile=self._gs.profile,
                                    profile_dir=self._gs.profile_dir())
        # Context menu reads these off the view (ESL path resolution + refresh).
        self._plugin_view.game = self._gs.game
        self._plugin_view.profile_dir = self._gs.profile_dir()
        self._plugin_view.on_plugins_changed = self._reload_plugins
        self._apply_plugin_search()
        # Persistent red marker-strip ticks for plugins with missing masters
        # (Tk parity) — recomputed from the freshly-loaded PF_MISSING flags.
        self._plugin_view.refresh_missing_marker()
        # Resolved plugin paths (name.lower → on-disk path) power the master-
        # highlight on plugin selection; clear any stale master ticks.
        from gui_qt.plugin_state import resolve_plugin_paths_for_game
        self._plugin_paths = (resolve_plugin_paths_for_game(self._gs.game)
                              if self._gs.game is not None else {})
        self._plugin_view.set_master_highlight(set())
        # Row indices/flags changed — re-apply any active plugin filter.
        if getattr(self, "_plugin_filter_panel", None) is not None:
            self._apply_plugin_filters()
        self._refresh_plugin_stats()
        self._refresh_framework_banner()
        print(f"[gui_qt] plugins: {len(rows)} entries")

    # ---- Sort Plugins (LOOT) ----------------------------------------------
    def _on_sort_plugins(self):
        """LOOT-sort the load order on a worker thread (reuses the Tk backend
        LOOT/loot_sorter.sort_plugins). Result applied on the UI thread."""
        from LOOT.loot_sorter import is_available
        if not is_available():
            self._notify("LOOT library not available — cannot sort.", "warning")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return
        if not getattr(game, "loot_sort_enabled", False):
            self._notify("LOOT sorting isn't supported for this game.", "warning")
            return
        rows = list(self._plugin_model._rows)
        if not rows:
            self._notify("No plugins to sort.", "warning")
            return
        if self._sort_running:
            self._notify("A sort is already running.", "info")
            return

        # Locked plugins stay put; LOOT sorts the rest. (Qt model already carries
        # vanilla rows pinned at the top, so — unlike Tk — we don't inject them.)
        locked_indices = {i: r for i, r in enumerate(rows)
                          if self._plugin_model.is_locked(i)}
        unlocked = [r for i, r in enumerate(rows) if i not in locked_indices]
        plugin_names = [r.name for r in unlocked]
        enabled_set = {r.name for r in unlocked if r.enabled}

        pdir = self._gs.profile_dir()
        userlist_path = None
        try:
            prof_ul = (pdir / "userlist.yaml") if pdir else None
            if prof_ul and prof_ul.is_file():
                userlist_path = prof_ul
            else:
                from Utils.config_paths import get_loot_game_dir
                g_ul = get_loot_game_dir(game.game_id) / "userlist.yaml"
                userlist_path = g_ul if g_ul.is_file() else None
        except Exception:
            userlist_path = None

        include_vanilla = bool(getattr(game, "plugins_include_vanilla", False))
        # Snapshot what the apply step needs (no model access mid-flight).
        self._sort_ctx = {
            "rows": rows, "locked_indices": locked_indices,
            "plugin_names": plugin_names, "include_vanilla": include_vanilla,
            "profile_dir": pdir, "game_id": game.game_id,
            "game": game, "profile": self._gs.profile,
        }

        kw = dict(
            plugin_names=plugin_names, enabled_set=enabled_set,
            game_name=getattr(game, "name", self._gs.game_name),
            game_path=game.get_game_path(),
            staging_root=game.get_effective_mod_staging_path(),
            game_type_attr=getattr(game, "loot_game_type", ""),
            game_id=game.game_id,
            masterlist_url=getattr(game, "loot_masterlist_url", ""),
            masterlist_repo=getattr(game, "loot_masterlist_repo", ""),
            game_data_dir=(game.get_vanilla_plugins_path()
                           if hasattr(game, "get_vanilla_plugins_path") else None),
            userlist_path=userlist_path,
        )

        self._sort_running = True
        self._plugin_sort_btn.setEnabled(False)
        self._notify(f"Running LOOT on {len(plugin_names)} plugins…", "info")

        import threading

        def worker():
            from LOOT.loot_sorter import sort_plugins
            try:
                result = sort_plugins(
                    log_fn=lambda m: print(f"[loot] {m}", flush=True), **kw)
            except Exception as exc:
                print(f"[loot] sort failed: {exc}", flush=True)
                self._sort_plugins_ready.emit(None)
                return
            self._sort_plugins_ready.emit(result)

        threading.Thread(target=worker, daemon=True).start()

    def _on_sort_plugins_ready(self, result):
        self._sort_running = False
        if hasattr(self, "_plugin_sort_btn"):
            self._plugin_sort_btn.setEnabled(True)
        ctx = getattr(self, "_sort_ctx", None) or {}
        self._sort_ctx = None
        if result is None:
            self._notify("LOOT sort failed — see log.", "error")
            return
        for w in getattr(result, "warnings", []) or []:
            self._append_log(f"[loot] warning: {w}")

        # Persist evaluated LOOT metadata so PF_LOOT/DIRTY/TAGS light up on reload.
        try:
            from LOOT.loot_sorter import write_loot_info
            write_loot_info(ctx.get("profile_dir"), result.plugin_info,
                            result.general_messages, game_id=ctx.get("game_id", ""))
        except Exception as exc:
            self._append_log(f"[loot] could not write loot.json: {exc}")

        from gui_qt.plugin_state import apply_loot_sort, save_plugins
        new_rows, moved = apply_loot_sort(
            ctx.get("rows", []), ctx.get("locked_indices", {}),
            list(result.sorted_names), ctx.get("include_vanilla", False))

        game, profile = ctx.get("game"), ctx.get("profile")
        if game is not None and profile:
            self._plugin_model._rows = new_rows
            try:
                save_plugins(game, profile, new_rows)
            except Exception as exc:
                self._notify(f"Failed to write load order: {exc}", "error")
                return
        # Reload (rebuilds rows + flags) and recompute BSA conflicts (a sort
        # changes plugin order → BSA winners follow plugin load order).
        self._reload_plugins()
        self._rebuild_conflicts_async()
        if moved == 0 and not ctx.get("locked_indices"):
            self._notify("Load order is already sorted.", "info")
        else:
            self._notify(f"Sorted — {moved} plugin{'s' if moved != 1 else ''} moved.",
                         "success")

    def _refresh_framework_banner(self):
        """Re-detect modding frameworks and update the Plugins-tab banner. Called
        on game/profile change + after each filemap rebuild (same as Tk)."""
        if not hasattr(self, "_framework_banner"):
            return
        from Utils.framework_detect import detect_frameworks
        staging = self._gs.staging_dir()
        filemap_path = (staging.parent / "filemap.txt") if staging is not None else None
        try:
            statuses = detect_frameworks(
                self._gs.game, filemap_path, self._gs.modlist_path(),
                rf_toggle_enabled=True)
        except Exception as exc:
            print(f"[gui_qt] framework detect error: {exc}", flush=True)
            statuses = []
        self._framework_banner.set_statuses(statuses)

    def _rebuild_conflicts_async(self, rescan_index: bool = False):
        """Build the filemap off-thread; the worker emits _conflicts_ready
        (queued → UI thread). A generation counter drops results from a
        superseded reload (user switched game before the build finished).
        rescan_index=True forces a full disk rescan (Refresh button)."""
        import threading
        gen = getattr(self, "_conflict_gen", 0) + 1
        self._conflict_gen = gen
        # Serialize the actual build: rapid triggers (e.g. a Mod Files edit while
        # a previous rescan is still running) otherwise run two rebuild_mod_index
        # writes concurrently and collide on modindex.bin.tmp. The generation
        # check still drops superseded RESULTS.
        if not hasattr(self, "_conflict_build_lock"):
            self._conflict_build_lock = threading.Lock()

        def worker():
            # log to stderr (not the widget) — we're off the UI thread.
            with self._conflict_build_lock:
                if gen != self._conflict_gen:
                    return   # a newer build was queued while we waited — skip
                data = self._gs.build_conflicts(
                    log_fn=lambda m: print(f"[filemap] {m}", flush=True),
                    rescan_index=rescan_index)
            self._conflicts_ready.emit(gen, data)

        threading.Thread(target=worker, daemon=True).start()

    def _on_conflicts_ready(self, gen: int, data):
        if gen != self._conflict_gen:
            return
        self._conflict_data = data
        self._modlist_model.set_conflicts(data.loose_codes, data.bsa_codes)
        # Filemap-derived flag overlays (info=pre-RTX, root=custom root rule).
        self._modlist_model.set_prertx_mods(getattr(data, "prertx_mods", set()))
        self._modlist_model.set_root_rule_mods(getattr(data, "root_rule_mods", set()))
        # Cross-panel highlighting needs the override + owner maps.
        self._modlist_view.set_conflict_maps(
            data.overrides, data.overridden_by,
            data.bsa_overrides, data.bsa_overridden_by)
        self._plugin_view.set_plugin_owner(data.plugin_owner)
        # Rebuild the filter data + repopulate the filter panel's dynamic lists,
        # then reapply whatever filters are currently active.
        self._rebuild_filter_data()
        # The deployed filemap changed → the Data tab is stale (rebuilds lazily).
        if hasattr(self, "_data_view"):
            self._data_view.mark_dirty()
        # A mod may have been added/removed → re-evaluate Install vs Reinstall.
        if hasattr(self, "_downloads_view"):
            self._downloads_view.mark_dirty()
        # The deployed file set changed → the Text Files list is stale.
        if hasattr(self, "_text_files_view"):
            self._text_files_view.mark_dirty()
        # A mod may have been removed/added → re-evaluate the Nexus browser's
        # Install/Reinstall buttons (remove goes through save()→conflict rebuild,
        # which is the only signal the Nexus tab gets for a removal).
        nv = getattr(self, "_nexus_view", None)
        if nv is not None:
            nv.refresh_installed()
        # The filemap (staged/deployed file set) changed → framework states may
        # have flipped (e.g. a framework mod toggled, deployed, or removed).
        self._refresh_framework_banner()
        # A mod was toggled / added / removed → the footer counts + size are
        # stale (token-guarded, so overlapping walks coalesce).
        self._refresh_modlist_stats()

    # ----------------------------------------------------------------- right
    def _build_plugins(self) -> QWidget:
        return self._plugins_placeholder()

    def _play_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(6)

        # All Play-section controls are anchored to the RIGHT: the stretch sits
        # FIRST, so extra width from resizing the plugins panel opens up on the
        # left and the controls stay locked together at the right edge.
        h.addStretch(1)

        # Game context (fixed-size).
        self._play_game_selector = SelectorButton(
            items=["Stardew Valley"],
            current="Stardew Valley",
            min_width=0,
            on_select=self._on_game_changed,
        )
        self._play_game_selector.setFixedHeight(self._BTN_H)
        h.addWidget(self._play_game_selector)

        # ▶ Play — plain fixed-size button (no dropdown).
        play = QPushButton("▶  Play")
        play.setObjectName("PlayButton")
        play.setFixedHeight(self._BTN_H)
        play.setCursor(Qt.PointingHandCursor)
        h.addWidget(play)

        # Exe / run options — a gear ICON button; its menu holds exe choices and
        # the settings / refresh / open-folder actions (moved off the toolbar).
        self._exe_selector = SelectorButton(
            items=["Default exe"],
            current="Default exe",
            icon=icon("settings.png", self._ICON_PX),
            icon_px=self._ICON_PX,
            actions=[
                ("Select executable…", lambda: self._on_play_action("select_exe")),
                ("Settings", lambda: self._on_play_action("settings")),
                ("Refresh", lambda: self._on_play_action("refresh")),
                ("Open application folder", lambda: self._on_play_action("folder")),
            ],
            on_select=lambda _l: None,
        )
        self._exe_selector.setFixedSize(self._BTN_H, self._BTN_H)
        h.addWidget(self._exe_selector)
        return bar

    def _plugins_placeholder(self) -> QWidget:
        from PySide6.QtWidgets import QStackedWidget
        from gui_qt.plugin_model import PluginModel
        from gui_qt.plugin_view import PluginView

        frame = QFrame()
        frame.setObjectName("PlaceholderPane")
        v = QVBoxLayout(frame)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        # Sub-tab strip — switches the stacked pages below.
        self._plugin_tab_names = ["Plugins", "Mod Files", "Text Files",
                                  "Data", "Downloads"]
        self._plugin_stack = QStackedWidget()

        # Page 0: the real Plugins view, with a framework-status banner above the
        # columns (one colored row per framework the game declares).
        self._plugin_model = PluginModel()
        # A plugin reorder/toggle changes BSA load order (BSAs load at their
        # plugin's position), so recompute conflicts when the order is saved —
        # reuses the same rebuild path as mod toggles. _build_bsa_conflicts
        # re-reads the freshly-written loadorder.txt.
        self._plugin_model.order_changed.connect(self._rebuild_conflicts_async)
        self._plugin_model.save_failed.connect(
            lambda msg: self._notify(msg, "error"))
        self._plugin_view = PluginView(self._plugin_model)
        from gui_qt.framework_banner import FrameworkBanner
        self._framework_banner = FrameworkBanner()
        _plugins_page = QWidget()
        _pl = QVBoxLayout(_plugins_page)
        _pl.setContentsMargins(0, 0, 0, 0)
        _pl.setSpacing(0)
        _pl.addWidget(self._framework_banner)
        _pl.addWidget(self._plugin_view, 1)
        self._plugin_stack.addWidget(_plugins_page)
        # Page 1: the real Mod Files view.
        from gui_qt.mod_files_view import ModFilesView
        self._mod_files_view = ModFilesView()
        self._mod_files_view.changed.connect(self._on_mod_files_changed)
        self._mod_files_view.on_open_image = self._open_image_preview_tab
        self._plugin_stack.addWidget(self._mod_files_view)
        # Page 2: the real Text Files view.
        from gui_qt.text_files_view import TextFilesView
        self._text_files_view = TextFilesView()
        self._text_files_view.on_open_file = self._open_text_editor_tab
        self._plugin_stack.addWidget(self._text_files_view)
        # Page 3: the real Data view.
        from gui_qt.data_view import DataView
        self._data_view = DataView()
        self._data_view.on_select_mod = self._on_data_select_mod
        self._plugin_stack.addWidget(self._data_view)
        # Page 4: the real Downloads view.
        from gui_qt.downloads_view import DownloadsView
        self._downloads_view = DownloadsView()
        self._downloads_view.on_install = self._install_paths
        self._downloads_view.selection_changed.connect(
            self._update_downloads_footer)
        self._plugin_stack.addWidget(self._downloads_view)
        self._TEXT_FILES_TAB_IDX = 2
        self._DATA_TAB_IDX = 3
        self._DOWNLOADS_TAB_IDX = 4

        tabs = QHBoxLayout()
        tabs.setSpacing(2)
        self._plugin_tab_labels = []
        for i, t in enumerate(self._plugin_tab_names):
            lbl = QLabel(t)
            lbl.setCursor(Qt.PointingHandCursor)
            lbl.mousePressEvent = lambda _e, idx=i: self._select_plugin_tab(idx)
            tabs.addWidget(lbl)
            self._plugin_tab_labels.append(lbl)
        tabs.addStretch(1)
        v.addLayout(tabs)
        v.addWidget(self._plugin_stack, 1)
        self._select_plugin_tab(0)
        return frame

    def _select_plugin_tab(self, idx: int):
        self._plugin_stack.setCurrentIndex(idx)
        # Swap the column footer to match the active sub-tab. Footer pages:
        # 0 plugins / 1 Mod Files / 2 Data / 3 Downloads / 4 Text Files.
        fstack = getattr(self, "_plugin_footer_stack", None)
        tf_idx = getattr(self, "_TEXT_FILES_TAB_IDX", 2)
        data_idx = getattr(self, "_DATA_TAB_IDX", 3)
        dl_idx = getattr(self, "_DOWNLOADS_TAB_IDX", 4)
        if fstack is not None:
            fstack.setCurrentIndex(
                1 if idx == 1 else 2 if idx == data_idx
                else 3 if idx == dl_idx else 4 if idx == tf_idx else 0)
        # Deferred build: only (re)build a tab's contents when it's shown.
        dv = getattr(self, "_data_view", None)
        if dv is not None:
            dv.set_visible_tab(idx == data_idx)
        dlv = getattr(self, "_downloads_view", None)
        if dlv is not None:
            dlv.set_visible_tab(idx == dl_idx)
            if idx == dl_idx:
                self._update_downloads_footer()
        tfv = getattr(self, "_text_files_view", None)
        if tfv is not None:
            tfv.set_visible_tab(idx == tf_idx)
        for i, lbl in enumerate(self._plugin_tab_labels):
            sel = i == idx
            lbl.setStyleSheet(
                "padding:4px 8px;" + (
                    f"color:#fff; border-bottom:2px solid {_c(self._pal,'ACCENT')};"
                    if sel else f"color:{_c(self._pal,'TEXT_DIM')};"))

    # --------------------------------------------------------------- widgets
    def _action_button(self, text: str, icon_name: str,
                       compact: bool = False) -> QToolButton:
        """Flat toolbar-style button with icon + label (mockup look).
        *compact* uses the smaller footer icon size."""
        px = self._FOOT_ICON_PX if compact else self._ICON_PX
        b = QToolButton()
        b.setText(text)
        b.setIcon(icon(icon_name, px))
        b.setIconSize(QSize(px, px))
        b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        b.setObjectName("FooterButton" if compact else "ActionButton")
        b.setCursor(Qt.PointingHandCursor)
        return b

    def _populate_menu(self, menu: "QMenu", items: "list[tuple]") -> None:
        """Fill *menu* from *items*. Each entry is None (separator) or
        (label, callback) — where callback may be a list of (label, callback)
        pairs, which becomes a submenu."""
        for entry in items:
            if entry is None:
                menu.addSeparator()
                continue
            label, cb = entry
            if isinstance(cb, list):
                sub = menu.addMenu(label)
                self._populate_menu(sub, cb)
            elif cb is not None:
                act = menu.addAction(label)
                act.triggered.connect(lambda _=False, c=cb: c())
            else:
                menu.addAction(label)   # inert label (placeholder)

    def _menu_action_button(self, text: str, icon_name: str,
                            items: "list[tuple]") -> QToolButton:
        """Like _action_button but a split button with a dropdown menu.
        *items* is a list of (label, callback|None); None inserts a separator.
        If the second element is a list of (label, callback) pairs it becomes a
        submenu instead. Highlights (button + arrow) while the menu is open via
        the `menuOpen` property, mirroring SelectorButton."""
        b = self._action_button(text, icon_name)
        b.setProperty("split", True)
        b.setPopupMode(QToolButton.MenuButtonPopup)
        menu = QMenu(b)
        self._populate_menu(menu, items)
        b.setMenu(menu)
        # Open on press (not release) so the menuOpen highlight covers the
        # whole button in one repaint — mirrors SelectorButton.
        b.pressed.connect(b.showMenu)

        def _set_open(on):
            b.setProperty("menuOpen", on)
            b.style().unpolish(b); b.style().polish(b)
        menu.aboutToShow.connect(lambda: _set_open(True))
        menu.aboutToHide.connect(lambda: _set_open(False))
        b._menu = menu
        return b

    def _text_button(self, text: str, compact: bool = False) -> QToolButton:
        """Flat text-only button (same chrome as the action buttons)."""
        b = QToolButton()
        b.setText(text)
        b.setToolButtonStyle(Qt.ToolButtonTextOnly)
        b.setObjectName("FooterButton" if compact else "ActionButton")
        b.setCursor(Qt.PointingHandCursor)
        return b

    def _color_button(self, text: str, color: str,
                      compact: bool = False) -> QPushButton:
        """Solid colored button (plugin tools, matching the Tk app)."""
        pad = "4px 10px" if compact else "6px 14px"
        fs = "12px" if compact else "14px"
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(
            f"QPushButton{{background:{color}; color:#fff; border:none;"
            f" padding:{pad}; border-radius:4px; font-size:{fs};"
            f" font-weight:600;}}"
            f"QPushButton:hover{{background:{color};}}")
        return b

    def _icon_button(self, icon_name: str, tip: str = "") -> QToolButton:
        b = QToolButton()
        b.setIcon(icon(icon_name, self._ICON_PX))
        b.setIconSize(QSize(self._ICON_PX, self._ICON_PX))
        b.setAutoRaise(True)
        b.setCursor(Qt.PointingHandCursor)
        if tip:
            b.setToolTip(tip)
        return b

    def _group_sep(self) -> QFrame:
        s = QFrame()
        s.setFrameShape(QFrame.VLine)
        s.setObjectName("GroupSep")
        s.setFixedWidth(2)
        return s

    # ------------------------------------------------------ log control bar
    def _build_log_bar(self) -> QWidget:
        """Fixed full-width control bar below the log splitter. The 'Log' button
        toggles the (drag-resizable) log text area above. Error/Warning/Clear
        controls only appear while the log is open."""
        bar = QWidget()
        bar.setObjectName("LogBar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 4, 8, 4)
        h.setSpacing(12)

        self._log_toggle = self._text_button("Log", compact=True)
        self._log_toggle.clicked.connect(self._toggle_log)
        h.addWidget(self._log_toggle)

        # These only show when the log is open.
        self._errors_lbl = QLabel("● Errors")
        self._errors_lbl.setStyleSheet(f"color:{_c(self._pal,'TEXT_ERR')};")
        h.addWidget(self._errors_lbl)
        self._warnings_lbl = QLabel("● Warnings")
        self._warnings_lbl.setStyleSheet(f"color:{_c(self._pal,'TEXT_WARN')};")
        h.addWidget(self._warnings_lbl)
        self._clear_log_btn = self._text_button("Clear Log", compact=True)
        self._clear_log_btn.clicked.connect(lambda: self._log_view.clear())
        h.addWidget(self._clear_log_btn)

        h.addStretch(1)

        # Nexus username at the far right; hover shows API rate-limit usage.
        from gui_qt.nexus_footer import NexusFooterLabel
        self._nexus_footer = NexusFooterLabel(lambda: getattr(self, "_nexus_api", None))
        h.addWidget(self._nexus_footer)

        self._log_open_widgets = [self._errors_lbl, self._warnings_lbl,
                                  self._clear_log_btn]
        for w in self._log_open_widgets:
            w.setVisible(False)
        return bar

    def _log_is_open(self) -> bool:
        return len(self._vsplit.sizes()) > 1 and self._vsplit.sizes()[1] > 0

    def _toggle_log(self):
        if self._log_is_open():
            self._vsplit.setSizes([self._vsplit.height(), 0])     # collapse
        else:
            total = self._vsplit.height()
            self._vsplit.setSizes([total - 180, 180])             # open ~180px
        self._sync_log_controls()

    def _sync_log_controls(self):
        """Error/Warning/Clear controls are visible only while the log has
        height — whether opened by the button or dragged open/closed (Tk feel)."""
        open_ = self._log_is_open()
        for w in self._log_open_widgets:
            w.setVisible(open_)

    def _append_log(self, message: str):
        """Backend log_fn target — append a line to the log text area.

        Thread-safe: worker threads pass this as their ``log_fn`` (Nexus browser,
        collection detail/reset, installs …). Qt widgets may ONLY be touched on
        the GUI thread — touching ``_log_view`` from a worker is a data race that
        can segfault — so when called off-thread we marshal through the queued
        ``_op_log`` signal instead of writing the widget directly."""
        try:
            from PySide6.QtCore import QThread
            if QThread.currentThread() is not self.thread():
                self._op_log.emit(str(message))
                return
        except Exception:
            pass
        try:
            self._log_view.appendPlainText(str(message).rstrip("\n"))
        except Exception:
            pass


def run() -> int:
    import sys
    from PySide6.QtWidgets import QApplication
    # Migrate/clean amethyst.ini BEFORE anything reads it (theme loader, GameState).
    # Wipes a pre-Qt ini (missing [meta] version=2) so everyone starts fresh.
    from Utils.ui_config import ensure_ini_version
    ensure_ini_version()
    app = QApplication(sys.argv)
    apply_theme(app)
    win = MainWindow(app)
    win.show()
    return app.exec()
