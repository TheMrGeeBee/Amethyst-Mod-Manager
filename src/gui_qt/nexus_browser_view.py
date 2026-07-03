"""Nexus Mods browser — a full detachable tab.

Qt port of the Tk overlay (gui/nexus_browser_overlay.py + browse/trending/
tracked/endorsed_mods_panel.py + mod_card.py). Layout:

  ┌───────────────────────────────────────────────────────────┐
  │ Browse Tracked Endorsed Trending   [Sort▾][Time▾] ☐Adult …│  toolbar (blue)
  ├──────────┬────────────────────────────────────────────────┤
  │ Categories│                card grid (pink)                │
  │ (green)   │                                                │
  ├──────────┴────────────────────────────────────────────────┤
  │ [search……] Search ✕   ◂ Prev  Next ▸  page [ ] / N   status│  footer (yellow)
  └───────────────────────────────────────────────────────────┘

All Nexus data + install logic comes from the toolkit-neutral Nexus/ layer; this
file is pure Qt UI + threading. Fetches run on worker threads and marshal results
back via signals. The active GAME determines the domain (game.nexus_game_domain).
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer, Signal, QEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
    QScrollArea, QFrame, QCheckBox, QPushButton, QToolButton, QMenu,
    QSizePolicy, QSplitter,
)

from gui_qt.theme_qt import active_palette, _c
from gui_qt.safe_emit import safe_emit
from gui_qt.selector_button import SelectorButton
from gui_qt.nexus_mod_card import NexusModCard, ThumbnailLoader, CARD_W

# label → API value (verbatim from Tk browse_mods_panel.SORT_KEYS / TIME_RANGES)
SORT_KEYS = [
    ("Downloads", "downloads"),
    ("Date Published", "createdAt"),
    ("Endorsements", "endorsements"),
    ("Last Updated", "updatedAt"),
]
TIME_RANGES = [
    ("All time", None),
    ("24 hours", 1),
    ("7 days", 7),
    ("14 days", 14),
    ("28 days", 28),
    ("Year", 365),
]
SECTIONS = ["Browse", "Tracked", "Endorsed", "Trending"]
PAGE_SIZE_BROWSE = 30
PAGE_SIZE_TRENDING = 20


class NexusBrowserView(QWidget):
    """Required: *api* (authed NexusAPI), *domain* (game.nexus_game_domain),
    *game*. Optional: *install_fn(list[str])* (defaults to a no-op), *log_fn*."""

    _results_ready = Signal(object, str, object)   # (entries, status, token)
    _cats_ready = Signal(object)                    # (list[NexusCategory])
    _premium_checked = Signal(object, object)       # (entry, is_premium|None)
    _files_ready = Signal(object, object)           # (entry, list[NexusModFile])
    _download_done = Signal(object, object)         # (archive_path|None, meta|None)

    def __init__(self, api, domain, game, install_fn=None, log_fn=None,
                 parent=None):
        super().__init__(parent)
        self._api = api
        self._domain = domain or ""
        self._game = game
        self._install_fn = install_fn or (lambda paths, metas=None: None)
        self._log = log_fn or (lambda m: None)

        # state
        self._section = "Browse"
        self._page = 0
        self._sort_key = "downloads"
        self._time_days = None
        self._query = ""
        self._selected_categories: list[str] = []
        self._show_adult = self._load_show_adult()
        self._entries = []
        self._cards: list[NexusModCard] = []
        self._cols = 0
        self._fetch_token = 0           # guards against stale async results
        self._cats_loaded = False

        self._thumbs = ThumbnailLoader(self)
        self._thumbs.loaded.connect(self._on_thumb)
        self._results_ready.connect(self._on_results)
        self._cats_ready.connect(self._on_cats)
        self._premium_checked.connect(self._on_premium_checked)
        self._files_ready.connect(self._on_files_ready)
        self._download_done.connect(self._on_download_done)
        self._installing = False        # serialise one Nexus install at a time

        self._build()
        self._update_section_buttons()
        self._update_browse_controls_visibility()
        self._load_categories()
        self._reload()

    # -- construction -------------------------------------------------------
    @staticmethod
    def _filter_qss(p) -> str:
        """Match the modlist filter side-panel styling (same #Filter* QSS) so
        the categories panel's font, header and backgrounds look identical."""
        c = lambda k: _c(p, k)
        return f"""
        #FilterPanel {{ background: {c('BG_PANEL')}; }}
        #FilterHeader {{ background: {c('BG_HEADER')}; }}
        #FilterTitle {{ font-weight: bold; font-size: 14px; color: {c('TEXT_MAIN')}; }}
        #FilterRule {{ background: {c('BORDER')}; }}
        #FilterBody {{ background: {c('BG_PANEL')}; }}
        #FilterEmpty {{ color: {c('TEXT_DIM')}; font-style: italic; }}
        QScrollArea {{ background: {c('BG_PANEL')}; border: none; }}
        """

    @staticmethod
    def _load_show_adult() -> bool:
        try:
            from Utils.ui_config import load_nexus_show_adult
            return bool(load_nexus_show_adult())
        except Exception:
            return False

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        p = active_palette()

        # --- blue toolbar ---------------------------------------------------
        toolbar = QWidget()
        toolbar.setObjectName("HeaderBar")
        tb = QHBoxLayout(toolbar)
        tb.setContentsMargins(10, 6, 10, 6)
        tb.setSpacing(6)

        self._cat_toggle = QToolButton()
        self._cat_toggle.setText("☰ Categories")
        self._cat_toggle.setObjectName("ActionButton")
        self._cat_toggle.setCheckable(True)
        self._cat_toggle.setChecked(True)
        self._cat_toggle.setCursor(Qt.PointingHandCursor)
        self._cat_toggle.toggled.connect(self._toggle_categories)
        tb.addWidget(self._cat_toggle)

        self._section_btns: dict[str, QToolButton] = {}
        for name in SECTIONS:
            b = QToolButton()
            b.setText(name)
            b.setObjectName("ActionButton")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, n=name: self._set_section(n))
            tb.addWidget(b)
            self._section_btns[name] = b

        tb.addStretch(1)

        self._sort_sel = SelectorButton(
            items=[lbl for lbl, _ in SORT_KEYS], current="Downloads",
            prefix="Sort: ", min_width=150, on_select=self._on_sort_changed)
        tb.addWidget(self._sort_sel)
        self._time_sel = SelectorButton(
            items=[lbl for lbl, _ in TIME_RANGES], current="All time",
            prefix="Time: ", min_width=130, on_select=self._on_time_changed)
        tb.addWidget(self._time_sel)

        self._adult_cb = QCheckBox("Show adult")
        self._adult_cb.setChecked(self._show_adult)
        self._adult_cb.toggled.connect(self._on_adult_toggled)
        tb.addWidget(self._adult_cb)

        refresh = QToolButton()
        refresh.setText("Refresh")
        refresh.setObjectName("ActionButton")
        refresh.setCursor(Qt.PointingHandCursor)
        refresh.clicked.connect(self._reload)
        tb.addWidget(refresh)

        outer.addWidget(toolbar)

        # --- body: categories (green) + card grid (pink) -------------------
        # A QSplitter lets the user drag the divider to resize the categories
        # panel; the toolbar's "Categories" toggle hides/shows it.
        self._body_split = QSplitter(Qt.Horizontal)
        self._body_split.setChildrenCollapsible(True)
        self._body_split.setHandleWidth(6)

        # categories panel (resizable, bounded). Default width fits 3 cards in
        # the grid at the 1280 min window width. Styled to match the modlist
        # filters side-panel (same #Filter* object names + QSS).
        self._cat_panel = QWidget()
        self._cat_panel.setObjectName("FilterPanel")
        self._cat_panel.setMinimumWidth(120)
        self._cat_panel.setMaximumWidth(460)
        cv = QVBoxLayout(self._cat_panel)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        cat_header = QWidget()
        cat_header.setObjectName("FilterHeader")
        chl = QHBoxLayout(cat_header)
        chl.setContentsMargins(10, 6, 8, 6)
        cat_hdr = QLabel("Categories")
        cat_hdr.setObjectName("FilterTitle")
        chl.addWidget(cat_hdr)
        chl.addStretch(1)
        cv.addWidget(cat_header)
        cat_rule = QFrame()
        cat_rule.setObjectName("FilterRule")
        cat_rule.setFixedHeight(1)
        cv.addWidget(cat_rule)
        self._cat_scroll = QScrollArea()
        self._cat_scroll.setWidgetResizable(True)
        self._cat_scroll.setFrameShape(QFrame.NoFrame)
        self._cat_host = QWidget()
        self._cat_host.setObjectName("FilterBody")
        self._cat_layout = QVBoxLayout(self._cat_host)
        self._cat_layout.setContentsMargins(10, 8, 10, 12)
        self._cat_layout.setSpacing(3)
        self._cat_layout.setAlignment(Qt.AlignTop)
        self._cat_scroll.setWidget(self._cat_host)
        cv.addWidget(self._cat_scroll, 1)
        self._cat_checks: list[QCheckBox] = []
        self._cat_status = QLabel("Loading…")
        self._cat_status.setObjectName("FilterEmpty")
        self._cat_layout.addWidget(self._cat_status)
        self._cat_panel.setStyleSheet(self._filter_qss(p))
        self._body_split.addWidget(self._cat_panel)

        # card grid
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(16, 12, 16, 12)
        self._grid.setSpacing(12)
        self._grid.setAlignment(Qt.AlignTop)
        self._scroll.setWidget(self._grid_host)
        self._scroll.installEventFilter(self)
        self._body_split.addWidget(self._scroll)

        self._body_split.setStretchFactor(0, 0)
        self._body_split.setStretchFactor(1, 1)
        # ~260px categories: the grid then needs >=968px for 3 columns
        # ((3*CARD_W + 2*spacing + 32 margins)); at the 1280 min window the grid
        # gets ~1000px, so 3 cards fit. 300 was just over the threshold → 2.
        self._body_split.setSizes([260, 1020])
        outer.addWidget(self._body_split, 1)

        # --- yellow footer --------------------------------------------------
        footer = QWidget()
        footer.setObjectName("HeaderBar")
        ft = QHBoxLayout(footer)
        ft.setContentsMargins(10, 6, 10, 6)
        ft.setSpacing(6)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search mods…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(280)
        self._search.textChanged.connect(self._on_search_text)
        self._search.returnPressed.connect(self._do_search_now)
        ft.addWidget(self._search)
        sbtn = QToolButton()
        sbtn.setText("Search")
        sbtn.setObjectName("ActionButton")
        sbtn.setCursor(Qt.PointingHandCursor)
        sbtn.clicked.connect(self._do_search_now)
        ft.addWidget(sbtn)

        ft.addStretch(1)

        self._prev_btn = QToolButton()
        self._prev_btn.setText("◂ Prev")
        self._prev_btn.setObjectName("ActionButton")
        self._prev_btn.setCursor(Qt.PointingHandCursor)
        self._prev_btn.clicked.connect(self._prev_page)
        ft.addWidget(self._prev_btn)
        self._next_btn = QToolButton()
        self._next_btn.setText("Next ▸")
        self._next_btn.setObjectName("ActionButton")
        self._next_btn.setCursor(Qt.PointingHandCursor)
        self._next_btn.clicked.connect(self._next_page)
        ft.addWidget(self._next_btn)

        ft.addWidget(QLabel("Page"))
        self._page_edit = QLineEdit()
        self._page_edit.setFixedWidth(48)
        self._page_edit.setAlignment(Qt.AlignCenter)
        self._page_edit.returnPressed.connect(self._jump_to_page)
        ft.addWidget(self._page_edit)

        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        ft.addWidget(self._status)

        outer.addWidget(footer)

    # -- section / control state -------------------------------------------
    def _set_section(self, name: str):
        if name == self._section:
            self._section_btns[name].setChecked(True)
            return
        self._section = name
        self._page = 0
        self._query = ""
        self._search.blockSignals(True)
        self._search.clear()
        self._search.blockSignals(False)
        self._update_section_buttons()
        self._update_browse_controls_visibility()
        self._reload()

    def _update_section_buttons(self):
        for n, b in self._section_btns.items():
            b.setChecked(n == self._section)

    def _update_browse_controls_visibility(self):
        browse = self._section == "Browse"
        paged = self._section in ("Browse", "Trending")
        self._sort_sel.setVisible(browse)
        self._time_sel.setVisible(browse)
        for w in (self._prev_btn, self._next_btn, self._page_edit):
            w.setVisible(paged)

    def _toggle_categories(self, on: bool):
        if on:
            self._cat_panel.setVisible(True)
            self._body_split.setSizes(
                [getattr(self, "_cat_width", 260), max(1, self._scroll.width())])
        else:
            # Remember the current width so we can restore it, then hide.
            sizes = self._body_split.sizes()
            if sizes and sizes[0] > 0:
                self._cat_width = sizes[0]
            self._cat_panel.setVisible(False)

    # -- categories ---------------------------------------------------------
    def _load_categories(self):
        if self._cats_loaded or not self._domain:
            return

        def worker():
            try:
                cats = self._api.get_game_categories(self._domain)
            except Exception as exc:
                self._log(f"Nexus: categories error: {exc}")
                cats = []
            safe_emit(self._cats_ready, cats)

        threading.Thread(target=worker, daemon=True).start()

    def _on_cats(self, cats):
        self._cats_loaded = True
        # clear existing (checks + any indent-wrapper rows + the status label)
        while self._cat_layout.count():
            it = self._cat_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        self._cat_checks.clear()
        if not cats:
            self._cat_status = QLabel("No categories")
            self._cat_status.setStyleSheet(
                f"color:{_c(active_palette(),'TEXT_DIM')}; padding:2px;")
            self._cat_layout.addWidget(self._cat_status)
            return
        # parents first, then their children indented (Tk hierarchy).
        by_parent: dict = {}
        for c in cats:
            by_parent.setdefault(c.parent_category, []).append(c)
        tops = sorted(by_parent.get(None, []), key=lambda c: c.name.lower())
        for top in tops:
            self._add_cat_check(top.name, indent=0)
            for child in sorted(by_parent.get(top.category_id, []),
                                key=lambda c: c.name.lower()):
                self._add_cat_check(child.name, indent=1)

    def _add_cat_check(self, name: str, indent: int):
        # Use the SAME widget as the modlist filter panel (TriStateCheckBox) so
        # the rows look identical — two_state so it's plain on/off (no exclude).
        from gui_qt.tri_state_checkbox import TriStateCheckBox
        cb = TriStateCheckBox(name, two_state=True)
        cb.setToolTip(name)              # long names that clip still readable
        cb.stateChanged.connect(lambda _s: self._on_category_toggled())
        cb._cat_name = name
        if indent:
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(indent * 14, 0, 0, 0)
            rl.setSpacing(0)
            rl.addWidget(cb)
            self._cat_layout.addWidget(row)
        else:
            self._cat_layout.addWidget(cb)
        self._cat_checks.append(cb)

    def _on_category_toggled(self):
        self._selected_categories = [
            cb._cat_name for cb in self._cat_checks if cb.state()]
        self._page = 0
        self._reload()

    # -- toolbar handlers ---------------------------------------------------
    def _on_sort_changed(self, label: str):
        self._sort_key = dict(SORT_KEYS).get(label, "downloads")
        self._page = 0
        self._reload()

    def _on_time_changed(self, label: str):
        self._time_days = dict(TIME_RANGES).get(label)
        self._page = 0
        self._reload()

    def _on_adult_toggled(self, on: bool):
        self._show_adult = bool(on)
        try:
            from Utils.ui_config import save_nexus_show_adult
            save_nexus_show_adult(self._show_adult)
        except Exception:
            pass
        self._rebuild_cards()       # filter is applied at card-build time

    # -- search -------------------------------------------------------------
    def _on_search_text(self, text: str):
        t = getattr(self, "_search_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(300)
            t.timeout.connect(self._do_search_now)
            self._search_timer = t
        t.start()

    def _do_search_now(self):
        q = self._search.text().strip()
        if q == self._query:
            return
        self._query = q
        self._page = 0
        self._set_section_for_search()
        self._reload()

    def _set_section_for_search(self):
        # Searching only applies to Browse; switch there if elsewhere.
        if self._section != "Browse":
            self._section = "Browse"
            self._update_section_buttons()
            self._update_browse_controls_visibility()

    # -- pagination ---------------------------------------------------------
    def _page_size(self) -> int:
        return PAGE_SIZE_TRENDING if self._section == "Trending" else PAGE_SIZE_BROWSE

    def _prev_page(self):
        if self._page > 0:
            self._page -= 1
            self._reload()

    def _next_page(self):
        # Only allow next when the last page filled (more likely exist).
        if len(self._entries) >= self._page_size():
            self._page += 1
            self._reload()

    def _jump_to_page(self):
        txt = self._page_edit.text().strip()
        if txt.isdigit():
            self._page = max(0, int(txt) - 1)
            self._reload()

    # -- fetch --------------------------------------------------------------
    def set_game(self, game, domain):
        """Retarget this browser at a different game (game switched while the tab
        is open). Resets navigation + filters (categories differ per game) and
        re-fetches categories + the Browse grid for the new domain."""
        self._game = game
        self._domain = domain or ""
        # Reset navigation + search + filter state to the new game's defaults.
        self._section = "Browse"
        self._page = 0
        self._query = ""
        self._selected_categories = []
        self._time_days = None
        self._fetch_token += 1              # invalidate any in-flight fetch
        # Clear the search box without re-triggering a search.
        try:
            self._search.blockSignals(True)
            self._search.clear()
            self._search.blockSignals(False)
        except Exception:
            pass
        # Force categories to reload for the new game.
        self._cats_loaded = False
        while self._cat_layout.count():
            it = self._cat_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        self._cat_checks.clear()
        self._update_section_buttons()
        self._update_browse_controls_visibility()
        self._load_categories()
        self._reload()

    def _reload(self):
        if not self._domain:
            self._status.setText("No Nexus domain for this game.")
            return
        self._fetch_token += 1
        token = self._fetch_token
        self._set_loading(True)
        section = self._section
        page = self._page
        size = self._page_size()
        sort_key = self._sort_key
        time_days = self._time_days
        query = self._query
        cats = list(self._selected_categories) or None
        domain = self._domain

        def worker():
            entries = []
            status = ""
            try:
                if section == "Browse" and query:
                    if query.isdigit():
                        entries = self._api.search_mod_by_id(domain, int(query))
                    else:
                        entries = self._api.search_mods(
                            domain, query, count=size, offset=page * size,
                            category_names=cats)
                    status = f"Search '{query}': page {page + 1} ({len(entries)} result(s))"
                elif section == "Browse":
                    entries = self._api.get_top_mods(
                        domain, count=size, offset=page * size,
                        category_names=cats, created_since_days=time_days,
                        sort_key=sort_key)
                    status = f"Browse: page {page + 1}"
                elif section == "Trending":
                    entries = self._api.get_trending_mods_graphql(
                        domain, count=size, offset=page * size,
                        category_names=cats)
                    status = f"Trending (7 days): page {page + 1}"
                elif section == "Tracked":
                    entries = self._fetch_user_mods(domain, self._api.get_tracked_mods)
                    status = f"Tracked: {len(entries)} mod(s)"
                elif section == "Endorsed":
                    entries = self._fetch_user_mods(
                        domain, self._api.get_endorsements, only_status="Endorsed")
                    status = f"Endorsed: {len(entries)} mod(s)"
            except Exception as exc:
                self._log(f"Nexus: fetch error: {exc}")
                status = f"Error: {exc}"
                entries = []
            safe_emit(self._results_ready, entries, status, token)

        threading.Thread(target=worker, daemon=True).start()

    def _fetch_user_mods(self, domain, list_fn, only_status=None):
        """Tracked/Endorsed: list dicts → filter to this game → batch mod info."""
        rows = list_fn() or []
        ids = []
        for r in rows:
            if (r.get("domain_name", "") or "").lower() != domain.lower():
                continue
            if only_status and r.get("status", "") != only_status:
                continue
            mid = r.get("mod_id", 0)
            if mid:
                ids.append((domain, int(mid)))
        if not ids:
            return []
        info_map = self._api.graphql_mod_info_batch(ids)
        out = [info_map[mid] for _d, mid in ids if mid in info_map]
        return out

    def _on_results(self, entries, status, token):
        if token != self._fetch_token:
            return                       # stale
        self._entries = list(entries or [])
        self._status.setText(status)
        self._page_edit.setText(str(self._page + 1))
        self._set_loading(False)
        self._rebuild_cards()
        self._update_page_buttons()

    def _set_loading(self, on: bool):
        for w in (self._prev_btn, self._next_btn, self._sort_sel, self._time_sel,
                  self._page_edit):
            w.setEnabled(not on)
        if on:
            self._status.setText("Loading…")

    def _update_page_buttons(self):
        paged = self._section in ("Browse", "Trending")
        self._prev_btn.setEnabled(paged and self._page > 0)
        self._next_btn.setEnabled(paged and len(self._entries) >= self._page_size())

    # -- cards / grid -------------------------------------------------------
    def _visible_entries(self):
        if self._show_adult:
            return self._entries
        return [e for e in self._entries
                if not getattr(e, "contains_adult_content", False)]

    def _installed_ids(self) -> set:
        """Nexus mod IDs already installed in the active profile's staging for
        this game's domain. Recomputed each card rebuild / profile change."""
        game = self._game
        if game is None or not getattr(game, "is_configured", lambda: False)():
            return set()
        try:
            from pathlib import Path
            from Nexus.nexus_meta import scan_installed_mods
            staging = game.get_effective_mod_staging_path()
            if not staging or not Path(staging).is_dir():
                return set()
            domain = (self._domain or "").lower()
            return {
                m.mod_id for m in scan_installed_mods(Path(staging))
                if m.mod_id > 0
                and (not domain or (m.game_domain or "").lower() == domain)
            }
        except Exception:
            return set()

    def _rebuild_cards(self):
        for c in self._cards:
            c.setParent(None)
        self._cards.clear()
        installed = self._installed_ids()
        for e in self._visible_entries():
            card = NexusModCard(e, self._on_view, self._on_install,
                                on_context=self._show_card_menu,
                                is_installed=e.mod_id in installed)
            self._cards.append(card)
            self._thumbs.request(e.mod_id, getattr(e, "picture_url", "") or "")
        self._cols = 0
        self._relayout()

    def refresh_installed(self):
        """Recompute installed IDs and flip card buttons. Call on profile change
        and after an install completes — the browser tab persists across both."""
        installed = self._installed_ids()
        for card in self._cards:
            card.set_installed(card.entry.mod_id in installed)

    def _cols_for_width(self) -> int:
        vp = self._scroll.viewport().width()
        slot = CARD_W + self._grid.spacing()
        return max(1, (vp - 32) // slot)

    def _relayout(self):
        cols = self._cols_for_width()
        while self._grid.count():
            self._grid.takeAt(0)
        # Center the row group: cards live in columns 1..cols, and equal-stretch
        # spacer columns on both sides (0 and cols+1) push the block to center.
        for i, card in enumerate(self._cards):
            self._grid.addWidget(card, i // cols, 1 + (i % cols),
                                 Qt.AlignTop | Qt.AlignHCenter)
            card.show()
        for c in range(self._grid.columnCount()):
            self._grid.setColumnStretch(c, 0)
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(cols + 1, 1)
        self._cols = cols

    def _on_thumb(self, mod_id, pm):
        for card in self._cards:
            if card.entry.mod_id == mod_id:
                card.set_thumbnail(pm)

    def eventFilter(self, obj, event):
        if obj is self._scroll and event.type() == QEvent.Resize:
            if self._cols_for_width() != self._cols:
                self._relayout()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._cols_for_width() != self._cols:
            self._relayout()

    # -- card actions -------------------------------------------------------
    def _mod_url(self, entry) -> str:
        dom = getattr(entry, "domain_name", "") or self._domain
        return f"https://www.nexusmods.com/{dom}/mods/{entry.mod_id}"

    def _on_view(self, entry):
        from Utils.xdg import open_url
        open_url(self._mod_url(entry), log_fn=self._log)

    def _show_card_menu(self, entry, global_pos):
        menu = QMenu(self)
        menu.addAction("Open on Nexus", lambda: self._on_view(entry))
        menu.addAction("Install", lambda: self._on_install(entry))
        if self._section == "Tracked":
            menu.addAction("Untrack", lambda: self._user_action(
                "untrack", entry))
        elif self._section == "Endorsed":
            menu.addAction("Abstain", lambda: self._user_action(
                "abstain", entry))
        menu.exec(global_pos)

    def _user_action(self, kind: str, entry):
        domain = getattr(entry, "domain_name", "") or self._domain
        mod_id = entry.mod_id

        def worker():
            try:
                if kind == "untrack":
                    self._api.untrack_mod(domain, mod_id)
                    self._log(f"Nexus: untracked {entry.name}")
                elif kind == "abstain":
                    self._api.abstain_mod(domain, mod_id,
                                          getattr(entry, "version", ""))
                    self._log(f"Nexus: abstained {entry.name}")
            except Exception as exc:
                self._log(f"Nexus: {kind} error: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    # -- install (premium check → file pick → download → install queue) ----
    def _on_install(self, entry):
        if self._installing:
            self._log("Nexus: an install is already in progress.")
            return
        self._installing = True
        domain = getattr(entry, "domain_name", "") or self._domain
        mod_id = entry.mod_id
        name = entry.name or f"Mod {mod_id}"
        self._log(f"Nexus: preparing install for {name}…")

        def worker():
            ok = None
            try:
                user = self._api.validate()
                ok = bool(user.is_premium)
            except Exception as exc:
                self._log(f"Nexus: could not check account: {exc}")
                ok = None
            safe_emit(self._premium_checked, entry, ok)

        threading.Thread(target=worker, daemon=True).start()

    def _on_premium_checked(self, entry, is_premium):
        domain = getattr(entry, "domain_name", "") or self._domain
        mod_id = entry.mod_id
        if not is_premium:
            # Non-premium (or unknown): open the files page to use the site button.
            from Utils.xdg import open_url
            url = f"{self._mod_url(entry)}?tab=files"
            self._log("Nexus: premium required for direct download — opening "
                      "the files page (use 'Download with Mod Manager').")
            open_url(url, log_fn=self._log)
            self._installing = False
            return
        # Premium: fetch the file list on a worker → back to UI for the chooser.
        def worker():
            files = []
            try:
                resp = self._api.get_mod_files(domain, mod_id)
                files = list(resp.files)
            except Exception as exc:
                self._log(f"Nexus: file list error: {exc}")
            safe_emit(self._files_ready, entry, files)

        threading.Thread(target=worker, daemon=True).start()

    def _on_files_ready(self, entry, files):
        """UI thread: pick the file to install. >1 MAIN → in-window chooser."""
        mains = [f for f in files if f.category_name == "MAIN"] or list(files)
        if not mains:
            self._log("Nexus: no downloadable files found.")
            self._installing = False
            return
        mains.sort(key=lambda f: getattr(f, "uploaded_timestamp", 0), reverse=True)
        if len(mains) > 1:
            from gui_qt.nexus_file_chooser import NexusFileChooser

            def _picked(chosen):
                if chosen is None:
                    self._log("Nexus: install cancelled.")
                    self._installing = False
                    return
                self._start_download(entry, chosen)

            NexusFileChooser.show_over(
                self, entry.name or f"Mod {entry.mod_id}", mains, _picked)
        else:
            self._start_download(entry, mains[0])

    def _start_download(self, entry, file):
        domain = getattr(entry, "domain_name", "") or self._domain
        name = entry.name or f"Mod {entry.mod_id}"
        self._log(f"Nexus: downloading {file.file_name or name}…")

        def worker():
            archive = None
            meta = None
            try:
                from Nexus.nexus_download import NexusDownloader
                from Utils.config_paths import get_download_cache_dir_for_game
                # Download into the per-game CACHE folder (the Downloads tab
                # scans this), matching the Tk Nexus browser — NOT ~/Downloads.
                dest = get_download_cache_dir_for_game(
                    getattr(self._game, "name", "") or "")
                size = (file.size_in_bytes or 0) or (file.size_kb * 1024)
                result = NexusDownloader(self._api, download_dir=dest).download_file(
                    game_domain=domain, mod_id=entry.mod_id, file_id=file.file_id,
                    dest_dir=dest, known_file_name=file.file_name,
                    expected_size_bytes=size, progress_cb=lambda d, t: None)
                if result.success and result.file_path is not None:
                    archive = str(result.file_path)
                    # Build the meta from the KNOWN mod_id/file_id (the archive
                    # name can mis-parse), like the Tk browser — so the installed
                    # meta.ini records the right id and Reinstall detection works.
                    try:
                        from Nexus.nexus_meta import build_meta_from_download
                        meta = build_meta_from_download(
                            game_domain=domain, mod_id=entry.mod_id,
                            file_id=file.file_id, archive_name=result.file_name,
                            mod_info=entry, file_info=file)
                    except Exception:
                        meta = None
                else:
                    self._log(f"Nexus: download failed: "
                              f"{result.error or 'unknown error'}")
            except Exception as exc:
                self._log(f"Nexus: download error: {exc}")
            safe_emit(self._download_done, archive, meta)

        threading.Thread(target=worker, daemon=True).start()

    def _on_download_done(self, archive, meta):
        """UI thread: hand the downloaded archive (+ its prebuilt meta) to the
        app's install queue."""
        self._installing = False
        if not archive:
            return
        self._log(f"Nexus: downloaded → {archive}; installing…")
        self._install_fn([archive], {archive: meta} if meta is not None else None)
