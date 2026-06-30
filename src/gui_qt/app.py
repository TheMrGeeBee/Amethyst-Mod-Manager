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
    # Deploy/restore worker → UI thread (thread-safe queued connections).
    _op_progress = Signal(int, int, object)   # (done, total, phase|None)
    _op_log = Signal(str)
    _op_done = Signal(str, bool, object)       # (kind, success, warnings-list)
    # Install worker → UI thread.
    _install_done = Signal(int, int, object)   # (ok_count, total, names-list)
    _prepared_ready = Signal(object)           # (PreparedInstall|None)
    _one_install_done = Signal(object)         # (installed name|None)

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
        self._update_deployed_profile_highlight()

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
        self._right_col = right_col

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._build_modlist_area())
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
        # label -> handler ("" = no-op stub, needs a dialog/auth — wired later).
        _handlers = {
            "Expand all": self._on_toggle_collapse_all,
            "Enable all": self._on_toggle_enable_all,
            "Filters": self._toggle_modlist_filters,
            "Refresh Modlist": self._on_refresh_modlist,
            "Check Updates": lambda: self._append_log(
                "[modlist] Check Updates (not wired yet)"),
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
        search.textChanged.connect(self._on_plugin_search)
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
        self._notify(f"Installing {len(paths)} mod(s)…" if len(paths) > 1
                     else f"Installing {Path(paths[0]).name}…", "info")
        self._install_next()

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

        def worker():
            from Utils.mod_install import prepare_archive
            try:
                prepared = prepare_archive(
                    path, self._install_game, self._install_profile_dir,
                    log_fn=lambda m: self._op_log.emit(str(m)),
                    progress_fn=lambda d, t, ph=None: self._op_progress.emit(d, t, ph))
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
            self._one_install_done.emit(name)

        threading.Thread(target=worker, daemon=True).start()

    def _on_one_install_done(self, name):
        if name:
            self._install_ok.append(name)
        self._install_next()   # continue the queue

    def _on_install_done(self, ok: int, total: int, names):
        self._install_running = False
        if hasattr(self, "_install_btn"):
            self._install_btn.setEnabled(True)
        if self._progress_popup is not None:
            QTimer.singleShot(1200, self._progress_popup.clear)
        self._reload_modlist()
        self._reload_plugins()
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
        """Modlist view with a collapsible filter side panel docked on its left
        (Tk parity — the filter panel pushes the list right, not an overlay)."""
        area = QWidget()
        h = QHBoxLayout(area)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self._modlist_filter_panel = self._build_modlist_filter_panel()
        self._modlist_filter_panel.setVisible(False)
        h.addWidget(self._modlist_filter_panel)
        h.addWidget(self._build_modlist(), 1)
        return area

    def _build_modlist_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        from gui_qt.modlist_filter import STATUS_FILTERS

        # Filters whose backing data the Qt side doesn't build yet — shown but
        # disabled (greyed) so the panel is complete and they light up later.
        # (FOMOD/BAIN come from meta.is_fomod/is_bain; conflicts, plugins, BSA,
        #  PBR, updates, categories, file types are all wired.)
        _UNWIRED = {
            "filter_missing_reqs", "filter_has_disabled_plugins",
            "filter_has_disabled_files", "filter_has_notes",
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
        data.category_names = dict(getattr(self, "_mod_categories", {}))
        data.fomod_mods = set(getattr(self, "_mod_fomod", set()))
        data.bain_mods = set(getattr(self, "_mod_bain", set()))
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
        if entries and staging is not None:
            (versions, installed, flags,
             self._mod_categories, self._mod_updates,
             self._mod_fomod, self._mod_bain) = read_meta_for_entries(
                entries, staging)

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
        self._refresh_footer_toggle_labels()
        # Re-apply an active search against the fresh row indices.
        self._apply_modlist_search()
        print(f"[gui_qt] modlist: {ml_path} ({len(entries)} entries)")

        if entries:
            self._rebuild_conflicts_async(rescan_index=rescan_index)

    def _reload_plugins(self):
        """Load the active game/profile's plugins into the Plugins tab."""
        from gui_qt.plugin_state import load_plugins
        rows = load_plugins(self._gs.game, self._gs.profile)
        self._plugin_model.set_rows(rows, game=self._gs.game,
                                    profile=self._gs.profile,
                                    profile_dir=self._gs.profile_dir())
        self._apply_plugin_search()
        print(f"[gui_qt] plugins: {len(rows)} entries")

    def _rebuild_conflicts_async(self, rescan_index: bool = False):
        """Build the filemap off-thread; the worker emits _conflicts_ready
        (queued → UI thread). A generation counter drops results from a
        superseded reload (user switched game before the build finished).
        rescan_index=True forces a full disk rescan (Refresh button)."""
        import threading
        gen = getattr(self, "_conflict_gen", 0) + 1
        self._conflict_gen = gen

        def worker():
            # log to stderr (not the widget) — we're off the UI thread.
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
