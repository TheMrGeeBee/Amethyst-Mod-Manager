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
    # Proton-tools installer worker → UI thread.
    _proton_done = Signal(str, bool)           # (title, success)
    # Nexus validate() worker → UI thread (username or None).
    _nexus_validated = Signal(object)          # (username str | None)
    # LOOT Sort Plugins worker → UI thread (SortResult | None on error).
    _sort_plugins_ready = Signal(object)
    # Nexus OAuth client (background thread) → UI thread.
    # (kind, payload): kind in {"token","error","status"}.
    _oauth_event = Signal(str, object)
    # Check-for-updates worker → UI thread ((updates, missing) | None on error).
    _updates_ready = Signal(object)
    # Install-a-Nexus-mod-by-id flow (used by Missing Requirements) → UI thread.
    _req_install_files = Signal(object, object)   # (ctx dict, files|None)
    _req_install_dl = Signal(object, object)      # (archive|None, meta|None)

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
        self._proton_busy = False
        self._proton_done.connect(self._on_proton_done)
        self._sort_running = False
        self._sort_plugins_ready.connect(self._on_sort_plugins_ready)
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
        self._vsplit.setCollapsible(0, False)
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
        # Install-a-Nexus-mod-by-id (Missing Requirements) flow.
        self._req_installing = False
        self._req_install_files.connect(self._on_req_install_files)
        self._req_install_dl.connect(self._on_req_install_dl)
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
        finally:
            self._xpanel_busy = False

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

        btns = QHBoxLayout()
        btns.setSpacing(4)
        # label -> handler ("" = no-op stub, needs a dialog/auth — wired later).
        _handlers = {
            "Expand all": self._on_toggle_collapse_all,
            "Enable all": self._on_toggle_enable_all,
            "Filters": self._toggle_modlist_filters,
            "Refresh Modlist": self._on_refresh_modlist,
            "Check Updates": self._on_check_updates,
            "Restore backup": lambda: self._append_log(
                "[modlist] Restore backup (not wired yet)"),
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
        row so its right edge aligns with the last button."""
        btns = getattr(self, "_modlist_footer_btns", None)
        search = getattr(self, "_modlist_search", None)
        if not btns or search is None:
            return
        spacing = 4
        total = sum(b.sizeHint().width() for b in btns) + spacing * (len(btns) - 1)
        search.setFixedWidth(total)

    def _plugins_footer(self) -> QWidget:
        """Colored tool buttons + search, under the plugins."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        btns = QHBoxLayout()
        btns.setSpacing(4)
        _made = {}
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

        # (Plugin count / ESL status removed — to be relocated later.)

        search = QLineEdit()
        search.setPlaceholderText("Search plugins…")
        search.setClearButtonEnabled(True)
        search.textChanged.connect(self._on_plugin_search)
        v.addWidget(search)
        self._plugins_search = search
        return bar

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
                ("Collections…", None),
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
            "settings.png", tooltip="Settings")
        self._settings_button.clicked.connect(self._open_settings_tab)
        h.addWidget(self._settings_button)

        self._left_header_widget = header
        return header

    def _icon_square_button(self, icon_name: str, tooltip: str = "") -> QToolButton:
        """A compact square icon-only button (e.g. Settings) for the toolbar."""
        b = QToolButton()
        b.setIcon(icon(icon_name, self._ICON_PX))
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
        # Reflect the new game's profiles + keep both game selectors in sync.
        profs = self._gs.profiles()
        if profs:
            self._profile_selector.set_items(profs, current=self._gs.profile)
        self._game_selector.set_current(name)
        self._play_game_selector.set_current(name)
        self._reload_modlist()
        self._reload_plugins()
        self._update_deployed_profile_highlight()

    def _on_profile_changed(self, name):
        if name == self._gs.profile:
            return
        self._gs.set_profile(name)
        self._profile_selector.set_current(name)
        self._reload_modlist()
        self._reload_plugins()
        self._update_deployed_profile_highlight()

    def _on_game_action(self, which):
        if which == "add":
            self._open_add_game_tab()
        elif which == "configure":
            game = self._gs.game
            if game is not None:
                self._open_configure_game_tab(game)
            else:
                self._append_log("[game] no active game to configure")
        else:
            self._append_log(f"[game] {which} (not wired yet)")

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
        """A flag icon in the modlist Flags column was clicked. Update flag opens
        Change Version; the warning flag opens Missing Requirements (Tk parity)."""
        from gui_qt.modlist_data import FLAG_UPDATE, FLAG_MISSING_REQS
        e = self._modlist_model.entry(row)
        if e is None or e.is_separator:
            return
        if flag == FLAG_UPDATE:
            self._open_change_version_tab(e.name)
        elif flag == FLAG_MISSING_REQS:
            self._open_missing_reqs_tab(e.name)

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
        self._append_log(f"[profile] {which} (not wired yet)")

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
        from PySide6.QtWidgets import QFileDialog
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select mod archive(s)", str(Path.home()),
            "Mod archives (*.zip *.7z *.rar *.fomod *.tar *.tar.gz *.tgz);;All files (*)")
        if not paths:
            return
        self._install_paths(paths)

    # ---- Proton tools ------------------------------------------------------
    def _proton_game(self):
        """Return the active configured game, or None (after notifying)."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify("No configured game selected.", "warning")
            return None
        return game

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

    def _install_paths(self, paths: list[str], metas: dict | None = None):
        """Queue + install a list of archive paths (shared by the Install Mod
        button and the Downloads tab). FOMODs pause for the wizard mid-queue.
        *metas* optionally maps an archive path → a prebuilt NexusModMeta (the
        Nexus browser supplies the real mod_id/file_id so meta.ini is correct)."""
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

        def worker():
            from Utils.mod_install import prepare_archive
            try:
                prepared = prepare_archive(
                    path, self._install_game, self._install_profile_dir,
                    log_fn=lambda m: self._op_log.emit(str(m)),
                    progress_fn=lambda d, t, ph=None: self._op_progress.emit(d, t, ph),
                    prebuilt_meta=meta,
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

            view = FomodWizardView(prepared.fomod_config, prepared.fomod_base,
                                   prepared.mod_name, on_finish=_finish,
                                   on_cancel=_cancel)
            # Closing the tab (× / detached-window close) cancels the install.
            view.destroyed.connect(lambda *_: _cancel())
            self._tabs.open_tab(view, f"Install: {prepared.mod_name}",
                                key="fomod_wizard")
        else:
            self._run_finish_install(prepared, None)

    def _run_finish_install(self, prepared, selections):
        import threading

        def worker():
            from Utils.mod_install import finish_install
            try:
                name = finish_install(
                    prepared, selections,
                    log_fn=lambda m: self._op_log.emit(str(m)),
                    progress_fn=lambda d, t, ph=None: self._op_progress.emit(d, t, ph))
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
        self._install_next()   # continue the queue

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
        self._modlist_view = ModListView(self._modlist_model)
        return self._modlist_view

    def _build_modlist_area(self) -> QWidget:
        """Modlist column: the list + its tool footer (buttons + search) stacked
        vertically, with a collapsible filter side panel docked on the left (Tk
        parity — the filter panel pushes the column right, not an overlay)."""
        # The list + footer share one vertical column.
        col = QWidget()
        cv = QVBoxLayout(col)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
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
        """(Re)build FilterData from the current conflicts/meta/indexes and
        repopulate the filter panel's dynamic category/filetype lists, then
        reapply active filters. Cheap — reads the persisted indexes."""
        panel = getattr(self, "_modlist_filter_panel", None)
        if panel is None:
            return
        from gui_qt.modlist_filter import (
            FilterData, build_index_data, build_mods_with_bsa,
            build_mods_with_plugins,
        )
        cd = getattr(self, "_conflict_data", None)
        staging = self._gs.staging_dir()
        staging_parent = staging.parent if staging is not None else None

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
        if staging_parent is not None:
            counts, mod_ft, pbr = build_index_data(staging_parent)
            data.filetype_counts = counts
            data.mod_filetypes = mod_ft
            data.mods_with_pbr = pbr
            data.mods_with_bsa = build_mods_with_bsa(staging_parent)
            g = self._gs.game
            data.mods_with_plugins = build_mods_with_plugins(
                staging_parent, getattr(g, "plugin_extensions", None))
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
                entries, staging, self._ignored_missing_reqs)

        self._modlist_model.set_entries(entries)
        self._modlist_model._versions = versions
        self._modlist_model._installed = installed
        self._modlist_model._categories = self._mod_categories
        self._modlist_model.set_flags(flags)
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
        entries = [self._modlist_model.entry(r)
                   for r in range(self._modlist_model.rowCount())]
        self._modlist_model.set_sizes(compute_sizes(entries, staging))

    def _reload_plugins(self):
        """Load the active game/profile's plugins into the Plugins tab."""
        from gui_qt.plugin_state import load_plugins
        rows = load_plugins(self._gs.game, self._gs.profile)
        self._plugin_model.set_rows(rows, game=self._gs.game,
                                    profile=self._gs.profile,
                                    profile_dir=self._gs.profile_dir())
        self._apply_plugin_search()
        # Row indices/flags changed — re-apply any active plugin filter.
        if getattr(self, "_plugin_filter_panel", None) is not None:
            self._apply_plugin_filters()
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
        b.clicked.connect(b.showMenu)   # text section also opens the menu

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
        """Backend log_fn target — append a line to the log text area."""
        try:
            self._log_view.appendPlainText(message.rstrip("\n"))
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
