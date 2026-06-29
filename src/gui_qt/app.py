"""Qt main window — header / body / footer rows + bottom log panel.

  header row : top bar (game/profile + actions) | play bar      (fixed split)
  body row   : modlist ║ plugins                                (draggable)
  footer row : mod tools | plugin tools                         (fixed split)
  log panel  : drag-resizable log text area + control bar
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QMainWindow, QToolButton, QWidget, QSplitter,
    QLabel, QVBoxLayout, QHBoxLayout, QPlainTextEdit,
    QFrame, QLineEdit, QPushButton, QMenu,
)

from gui_qt.theme_qt import apply_theme, active_palette, _c
from gui_qt.icons import icon
from gui_qt.modlist_model import ModListModel
from gui_qt.modlist_view import ModListView
from gui_qt.selector_button import SelectorButton
from gui_qt.game_state import GameState
from gui_qt.detachable_tabs import DetachableTabWidget
from gui_qt import glue


class MainWindow(QMainWindow):
    # Carries (generation, ConflictData) from a worker thread to the UI thread
    # (queued connection — thread-safe). See _rebuild_conflicts_async.
    _conflicts_ready = Signal(int, object)

    _PLAY_BAR_W = 380       # play-bar (header right) fixed width
    _FOOTER_RIGHT_W = 400   # narrower than play bar so the 7 mod-tool buttons fit
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
        self.setWindowTitle("Amethyst Mod Manager")
        self.setMinimumSize(1280, 800)   # Steam Deck is the floor
        self.resize(1280, 800)

        # Header+body+footer go in a vertical splitter with the log text area
        # so the log is drag-resizable; the log control bar stays fixed below.
        main_content = QWidget()
        mc = QVBoxLayout(main_content)
        mc.setContentsMargins(0, 0, 0, 0)
        mc.setSpacing(0)
        mc.addWidget(self._build_header_row())
        mc.addWidget(self._build_body_row(), 1)
        mc.addWidget(self._build_footer_row())

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
        # Left: modlist. Right: a column with the Play bar on top + plugins
        # below. Splitter sits between modlist and the right column.
        right_col = QWidget()
        rc = QVBoxLayout(right_col)
        rc.setContentsMargins(0, 0, 0, 0)
        rc.setSpacing(0)
        play = self._play_bar()
        self._play_bar_widget = play
        rc.addWidget(play)
        rc.addWidget(self._build_plugins(), 1)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._build_modlist())
        split.addWidget(right_col)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 4)
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
        finally:
            self._xpanel_busy = False

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

    # ---------------------------------------------------------- footer row
    def _build_footer_row(self) -> QWidget:
        """Mod tools | plugin tools, fixed split (mirrors the header row)."""
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        left = self._modlist_footer()
        right = self._plugins_footer()
        right.setFixedWidth(self._FOOTER_RIGHT_W)
        self._plugins_footer_widget = right
        h.addWidget(left, 1)
        h.addWidget(right, 0)
        return row

    def _modlist_footer(self) -> QWidget:
        """Buttons row + search box, under the modlist."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        btns = QHBoxLayout()
        btns.setSpacing(4)
        for label in ["Expand all", "Enable all", "Check Updates", "Filters",
                      "Restore backup", "Refresh Modlist",
                      "Generate Separators"]:
            b = self._text_button(label, compact=True)
            b.setFixedHeight(self._FOOT_BTN_H)
            # Reserve the label's natural width so it never squashes at min size.
            b.setMinimumWidth(b.sizeHint().width())
            btns.addWidget(b)
        btns.addStretch(1)
        v.addLayout(btns)

        search = QLineEdit()
        search.setPlaceholderText("Search mods…")
        search.setClearButtonEnabled(True)
        v.addWidget(search)
        self._modlist_search = search
        return bar

    def _plugins_footer(self) -> QWidget:
        """Colored tool buttons + search, under the plugins."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        btns = QHBoxLayout()
        btns.setSpacing(4)
        for label, key in [
            ("Sort Plugins", "BTN_SUCCESS"),
            ("Groups", "BTN_INFO"),
            ("Plugin Rules", "BTN_INFO"),
            ("Filters", "BTN_INFO"),
        ]:
            b = self._color_button(label, _c(self._pal, key), compact=True)
            b.setFixedHeight(self._FOOT_BTN_H)
            btns.addWidget(b)
        btns.addStretch(1)
        v.addLayout(btns)

        # (Plugin count / ESL status removed — to be relocated later.)

        search = QLineEdit()
        search.setPlaceholderText("Search plugins…")
        search.setClearButtonEnabled(True)
        v.addWidget(search)
        self._plugins_search = search
        return bar

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
        for label, ico in [
            ("Install Mod", "install.png"),
            ("Deploy",      "deploy.png"),
            ("Restore",     "restore.png"),
        ]:
            b = self._action_button(label, ico)
            b.setFixedHeight(self._BTN_H)
            b.setToolTip(label)
            b._full_label = label
            self._action_buttons.append(b)
            h.addWidget(b)

        # Split menu buttons (placeholder menus — wired up in a later phase).
        for label, ico, items in [
            ("Proton", "proton.png", [
                ("Run: winecfg", None),
                ("Run: Winetricks", None),
                ("Run an .exe in this prefix…", None),
                None,
                ("Open Wine registry", None),
                ("Edit Wine DLL overrides", None),
                None,
                ("Install VC++ Redistributables", None),
                ("Install .NET…", None),
            ]),
            ("Wizard", "wizard.png", [
                ("Mod wizards…", None),
                ("Tool wizards…", None),
            ]),
            ("Nexus", "nexus.png", [
                ("Open Nexus Mods", None),
                ("Collections…", None),
                ("Check for updates", None),
            ]),
        ]:
            b = self._menu_action_button(label, ico, items)
            b.setFixedHeight(self._BTN_H)
            b.setToolTip(label)
            b._full_label = label
            self._action_buttons.append(b)
            h.addWidget(b)

        h.addStretch(1)

        # Settings — icon-only square button on the far right (placeholder menu/
        # dialog wired later).
        self._settings_button = self._icon_square_button(
            "settings.png", tooltip="Settings")
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

    def _on_profile_changed(self, name):
        if name == self._gs.profile:
            return
        self._gs.set_profile(name)
        self._profile_selector.set_current(name)
        self._reload_modlist()
        self._reload_plugins()

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

    def _on_play_action(self, which):
        self._append_log(f"[play] {which} (not wired yet)")

    def _build_modlist(self) -> QWidget:
        self._modlist_model = ModListModel([])
        self._modlist_view = ModListView(self._modlist_model)
        return self._modlist_view

    def _reload_modlist(self):
        """Load the active game/profile's modlist + metadata into the model."""
        from Utils.modlist import read_modlist
        from gui_qt.modlist_data import read_meta_for_entries

        ml_path = self._gs.modlist_path()
        staging = self._gs.staging_dir()
        entries = read_modlist(ml_path) if (ml_path and ml_path.is_file()) else []

        versions = installed = flags = {}
        if entries and staging is not None:
            versions, installed, flags = read_meta_for_entries(entries, staging)

        self._modlist_model.set_entries(entries)
        self._modlist_model._versions = versions
        self._modlist_model._installed = installed
        self._modlist_model.set_flags(flags)
        self._modlist_model.set_conflicts({}, {})   # clear stale; recomputed async
        # Persist edits back to this modlist; rebuild conflicts after each save.
        self._modlist_model.modlist_path = ml_path
        self._modlist_model.on_saved = self._rebuild_conflicts_async
        self._modlist_view.staging_dir = staging
        self._modlist_view.profile_dir = self._gs.profile_dir()
        self._modlist_view.load_separator_state()
        print(f"[gui_qt] modlist: {ml_path} ({len(entries)} entries)")

        if entries:
            self._rebuild_conflicts_async()

    def _reload_plugins(self):
        """Load the active game/profile's plugins into the Plugins tab."""
        from gui_qt.plugin_state import load_plugins
        rows = load_plugins(self._gs.game, self._gs.profile)
        self._plugin_model.set_rows(rows, game=self._gs.game,
                                    profile=self._gs.profile,
                                    profile_dir=self._gs.profile_dir())
        print(f"[gui_qt] plugins: {len(rows)} entries")

    def _rebuild_conflicts_async(self):
        """Build the filemap off-thread; the worker emits _conflicts_ready
        (queued → UI thread). A generation counter drops results from a
        superseded reload (user switched game before the build finished)."""
        import threading
        gen = getattr(self, "_conflict_gen", 0) + 1
        self._conflict_gen = gen

        def worker():
            # log to stderr (not the widget) — we're off the UI thread.
            data = self._gs.build_conflicts(
                log_fn=lambda m: print(f"[filemap] {m}", flush=True))
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

        # Page 0: the real Plugins view.
        self._plugin_model = PluginModel()
        self._plugin_view = PluginView(self._plugin_model)
        self._plugin_stack.addWidget(self._plugin_view)
        # Other pages: placeholders for now.
        for t in self._plugin_tab_names[1:]:
            ph = QLabel(f"{t}\n(coming in a later phase)")
            ph.setAlignment(Qt.AlignCenter)
            ph.setStyleSheet(f"color:{_c(self._pal,'TEXT_FAINT')};")
            self._plugin_stack.addWidget(ph)

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

    def _menu_action_button(self, text: str, icon_name: str,
                            items: "list[tuple]") -> QToolButton:
        """Like _action_button but a split button with a dropdown menu.
        *items* is a list of (label, callback|None); None inserts a separator.
        Highlights (button + arrow) while the menu is open via the `menuOpen`
        property, mirroring SelectorButton."""
        b = self._action_button(text, icon_name)
        b.setProperty("split", True)
        b.setPopupMode(QToolButton.MenuButtonPopup)
        menu = QMenu(b)
        for entry in items:
            if entry is None:
                menu.addSeparator()
                continue
            label, cb = entry
            act = menu.addAction(label)
            if cb is not None:
                act.triggered.connect(lambda _=False, c=cb: c())
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
    app = QApplication(sys.argv)
    apply_theme(app)
    win = MainWindow(app)
    win.show()
    return app.exec()
