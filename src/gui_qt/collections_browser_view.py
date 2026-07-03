"""Nexus Collections browser — a full detachable tab.

Like the Nexus mods browser (gui_qt/nexus_browser_view.py) but simpler: no
category side-panel — just a top toolbar, a middle card grid, and a bottom bar
(search + pagination). Cards are VIEW-ONLY this pass (open the collection's Nexus
page); the install/detail flow is a separate feature.

  ┌───────────────────────────────────────────────┐
  │ Collections            Open on Nexus   Refresh │  toolbar
  ├───────────────────────────────────────────────┤
  │                 card grid                      │
  ├───────────────────────────────────────────────┤
  │ [search……] Search   ◂ Prev  Next ▸  page  … │  footer
  └───────────────────────────────────────────────┘

Collection data comes from the neutral Nexus/ layer (get_collections /
search_collections). Fetches run on a worker thread → _results_ready signal.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer, Signal, QEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
    QScrollArea, QFrame, QToolButton, QMenu,
)

from gui_qt.theme_qt import active_palette, _c
from gui_qt.safe_emit import safe_emit
from gui_qt.nexus_mod_card import ThumbnailLoader
from gui_qt.collection_card import CollectionCard, CARD_W, IMG_W, IMG_H

PAGE_SIZE = 20            # Tk parity (collections_dialog PAGE_SIZE)


class CollectionsBrowserView(QWidget):
    """Required: *api* (authed NexusAPI), *domain* (game.nexus_game_domain),
    *game*. Optional *log_fn*."""

    _results_ready = Signal(object, str, object)   # (entries, status, token)

    def __init__(self, api, domain, game, log_fn=None, on_open_detail=None,
                 parent=None):
        super().__init__(parent)
        self._api = api
        self._domain = domain or ""
        self._game = game
        self._log = log_fn or (lambda m: None)
        # Card View opens the detail tab when provided; else falls back to the URL.
        self._on_open_detail = on_open_detail

        # state
        self._page = 0
        self._query = ""
        self._entries = []
        self._cards: list[CollectionCard] = []
        self._cols = 0
        self._fetch_token = 0           # guards against stale async results

        # Collection tiles are PORTRAIT — crop to the portrait cover size.
        self._thumbs = ThumbnailLoader(self, crop_w=IMG_W, crop_h=IMG_H)
        self._thumbs.loaded.connect(self._on_thumb)
        self._results_ready.connect(self._on_results)

        self._open_current_url = None
        self._build()
        self.refresh_open_current()
        self._reload()

    # -- construction -------------------------------------------------------
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        p = active_palette()

        # --- toolbar --------------------------------------------------------
        toolbar = QWidget()
        toolbar.setObjectName("HeaderBar")
        tb = QHBoxLayout(toolbar)
        tb.setContentsMargins(10, 6, 10, 6)
        tb.setSpacing(6)

        title = QLabel("Collections")
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        tb.addWidget(title)
        tb.addStretch(1)

        # Shown only when the active profile is a collection (see refresh_open_current).
        self._open_current_btn = QToolButton()
        self._open_current_btn.setText("Open Current")
        self._open_current_btn.setObjectName("ActionButton")
        self._open_current_btn.setCursor(Qt.PointingHandCursor)
        self._open_current_btn.clicked.connect(self._open_current)
        self._open_current_btn.setVisible(False)
        tb.addWidget(self._open_current_btn)

        open_btn = QToolButton()
        open_btn.setText("Open on Nexus")
        open_btn.setObjectName("ActionButton")
        open_btn.setCursor(Qt.PointingHandCursor)
        open_btn.clicked.connect(self._open_game_collections_on_nexus)
        tb.addWidget(open_btn)

        refresh = QToolButton()
        refresh.setText("Refresh")
        refresh.setObjectName("ActionButton")
        refresh.setCursor(Qt.PointingHandCursor)
        refresh.clicked.connect(self._reload)
        tb.addWidget(refresh)

        outer.addWidget(toolbar)

        # --- card grid (no categories panel) --------------------------------
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
        outer.addWidget(self._scroll, 1)

        # --- footer ---------------------------------------------------------
        footer = QWidget()
        footer.setObjectName("HeaderBar")
        ft = QHBoxLayout(footer)
        ft.setContentsMargins(10, 6, 10, 6)
        ft.setSpacing(6)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search collections…")
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
        self._reload()

    # -- pagination ---------------------------------------------------------
    def _page_size(self) -> int:
        return PAGE_SIZE

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
        is open). Resets paging + search and re-fetches the collection grid for
        the new domain."""
        self._game = game
        self._domain = domain or ""
        self._page = 0
        self._query = ""
        self._fetch_token += 1              # invalidate any in-flight fetch
        try:
            self._search.blockSignals(True)
            self._search.clear()
            self._search.blockSignals(False)
        except Exception:
            pass
        self.refresh_open_current()
        self._reload()

    def _reload(self):
        if not self._domain:
            self._status.setText("No Nexus domain for this game.")
            return
        self._fetch_token += 1
        token = self._fetch_token
        self._set_loading(True)
        page = self._page
        size = self._page_size()
        query = self._query
        domain = self._domain

        def worker():
            entries = []
            status = ""
            try:
                if query:
                    entries = self._api.search_collections(
                        domain, query, count=size, offset=page * size)
                    status = (f"Search '{query}': page {page + 1} "
                              f"({len(entries)} result(s))")
                else:
                    entries = self._api.get_collections(
                        domain, count=size, offset=page * size)
                    status = f"Collections: page {page + 1}"
                if not entries and page == 0:
                    status = ("No collections found."
                              if not query else f"No matches for '{query}'.")
            except Exception as exc:
                self._log(f"Nexus: collections error: {exc}")
                status = f"Error: {exc}"
                entries = []
            safe_emit(self._results_ready, entries, status, token)

        threading.Thread(target=worker, daemon=True).start()

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
        for w in (self._prev_btn, self._next_btn, self._page_edit):
            w.setEnabled(not on)
        if on:
            self._status.setText("Loading…")

    def _update_page_buttons(self):
        self._prev_btn.setEnabled(self._page > 0)
        # Search over-fetches a single capped batch (no real server paging), so
        # only paginate the unfiltered browse list.
        self._next_btn.setEnabled(
            (not self._query) and len(self._entries) >= self._page_size())

    # -- cards / grid -------------------------------------------------------
    def _rebuild_cards(self):
        for c in self._cards:
            c.setParent(None)
        self._cards.clear()
        primary_view = self._on_open_detail or self._on_view
        for e in self._entries:
            card = CollectionCard(e, primary_view,
                                  on_context=self._show_card_menu)
            self._cards.append(card)
            self._thumbs.request(e.id, getattr(e, "tile_image_url", "") or "")
        self._cols = 0
        self._relayout()

    def _cols_for_width(self) -> int:
        vp = self._scroll.viewport().width()
        slot = CARD_W + self._grid.spacing()
        return max(1, (vp - 32) // slot)

    def _relayout(self):
        cols = self._cols_for_width()
        while self._grid.count():
            self._grid.takeAt(0)
        # Center the row group: cards live in columns 1..cols, with equal-stretch
        # spacer columns on both sides (0 and cols+1).
        for i, card in enumerate(self._cards):
            self._grid.addWidget(card, i // cols, 1 + (i % cols),
                                 Qt.AlignTop | Qt.AlignHCenter)
            card.show()
        for c in range(self._grid.columnCount()):
            self._grid.setColumnStretch(c, 0)
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(cols + 1, 1)
        self._cols = cols

    def _on_thumb(self, coll_id, pm):
        for card in self._cards:
            if card.entry.id == coll_id:
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
    def _collection_url(self, entry) -> str:
        dom = getattr(entry, "game_domain", "") or self._domain
        return f"https://www.nexusmods.com/{dom}/collections/{entry.slug}"

    def _on_view(self, entry):
        from Utils.xdg import open_url
        open_url(self._collection_url(entry), log_fn=self._log)

    def _open_game_collections_on_nexus(self):
        from Utils.xdg import open_url
        open_url(f"https://www.nexusmods.com/games/{self._domain}/collections",
                 log_fn=self._log)

    # -- "Open Current" (installed collection) ------------------------------
    def refresh_open_current(self):
        """Show 'Open Current' only when the active profile is a collection.
        Call this on profile change (the app does)."""
        url = None
        try:
            from Utils.game_helpers import get_collection_url_from_profile
            pdir = getattr(self._game, "_active_profile_dir", None)
            if pdir is not None:
                url = get_collection_url_from_profile(pdir)
        except Exception:
            url = None
        self._open_current_url = url or None
        self._open_current_btn.setVisible(bool(self._open_current_url))

    def _open_current(self):
        if not self._open_current_url or self._on_open_detail is None:
            return
        from Utils.collection_manifest import parse_collection_url
        from Nexus.nexus_api import NexusCollection
        slug, url_domain, rev = parse_collection_url(self._open_current_url)
        if not slug:
            return
        col = NexusCollection(slug=slug, name=slug,
                              game_domain=url_domain or self._domain)
        self._on_open_detail(col, revision_number=rev)

    def _show_card_menu(self, entry, global_pos):
        menu = QMenu(self)
        menu.addAction("Open on Nexus", lambda: self._on_view(entry))
        menu.exec(global_pos)
