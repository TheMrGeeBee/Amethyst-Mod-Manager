"""Qt Text Files tab — lists config/text files from mods, profile, game folder and
My Games, grouped by source. Reuses Utils.text_files for discovery + content
search. Built lazily (only scans when the sub-tab is shown — the recursive game /
My-Games scans are expensive). Clicking a file opens the scoped text editor.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTreeView, QAbstractItemView,
)

import Utils.text_files as tf
from gui_qt.text_files_model import (
    TextFilesModel, _TextNode, COL_NAME, COL_SOURCE,
)


class TextFilesView(QWidget):
    """The Text Files tab. configure() once, then mark_dirty()/refresh()."""

    filetypes_changed = Signal()
    content_status_changed = Signal(object)   # current content keyword | None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.game = None
        self.profile_dir: Path | None = None
        self.filemap_path: Path | None = None
        self.staging_root: Path | None = None
        self.on_open_file = None        # callback(full_path, rel_path)
        self._dirty = True
        self._is_visible = False
        self._all_entries: list = []
        self._search = ""
        self._inc_exts: set = set()
        self._exc_exts: set = set()
        self._inc_srcs: set = set()
        self._exc_srcs: set = set()
        self._content_matches = None    # set[(rel, mod)] | None
        self._content_keyword = None
        self._build()

    # -- context ------------------------------------------------------------
    def configure(self, game, profile_dir, filemap_path, staging_root):
        self.game = game
        self.profile_dir = profile_dir
        self.filemap_path = filemap_path
        self.staging_root = staging_root
        self._dirty = True
        # A profile/game switch invalidates any active content search.
        self._content_matches = None
        self._content_keyword = None
        self.content_status_changed.emit(None)

    def set_visible_tab(self, visible: bool):
        self._is_visible = visible
        if visible and self._dirty:
            self.refresh()

    def mark_dirty(self):
        self._dirty = True
        if self._is_visible:
            self.refresh()

    def refresh(self):
        self._dirty = False
        self._rescan()

    # -- construction -------------------------------------------------------
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self._model = TextFilesModel(self)
        self._tree = QTreeView()
        self._tree.setModel(self._model)
        self._tree.setRootIsDecorated(False)
        self._tree.setIndentation(0)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(False)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._tree.clicked.connect(self._on_clicked)
        from gui_qt.text_files_delegate import TextFilesDelegate
        self._tree.setItemDelegate(TextFilesDelegate(self._tree))
        self._tree.expanded.connect(lambda *_: self._tree.viewport().update())
        self._tree.collapsed.connect(lambda *_: self._tree.viewport().update())

        from gui_qt.modlist_header import TkStyleHeader
        col_mins = {COL_NAME: 160, COL_SOURCE: 120}
        col_defaults = {COL_SOURCE: 200}
        hdr = TkStyleHeader(self._tree, col_mins, col_defaults)
        self._tree.setHeader(hdr)
        hdr.setMinimumSectionSize(min(col_mins.values()))
        for col, wdt in col_defaults.items():
            self._tree.setColumnWidth(col, wdt)
        self._name_min = col_mins[COL_NAME]
        self._tree.viewport().installEventFilter(self)
        v.addWidget(self._tree, 1)

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self._tree.viewport() and event.type() == QEvent.Resize:
            self._fit_name_to_width()
        return super().eventFilter(obj, event)

    def _fit_name_to_width(self):
        vp = self._tree.viewport().width()
        if vp <= 0:
            return
        target = vp - self._tree.columnWidth(COL_SOURCE)
        if target >= self._name_min and target != self._tree.columnWidth(COL_NAME):
            self._tree.header().resizeSection(COL_NAME, target)

    # -- scan / filter ------------------------------------------------------
    def _rescan(self):
        self._all_entries = tf.discover_text_files(
            self.game, self.profile_dir, self.filemap_path, self.staging_root)
        # A new scan invalidates the content-match set (paths may have changed).
        if self._content_matches is not None and self._content_keyword:
            self._content_matches = tf.content_search(
                self._all_entries, self._content_keyword)
        self.filetypes_changed.emit()
        self._apply()

    def _apply(self):
        entries = self._all_entries
        if self._content_matches is not None:
            cm = self._content_matches
            entries = [e for e in entries if (e[0], e[1]) in cm]
        if self._inc_exts:
            entries = [e for e in entries
                       if Path(e[0]).suffix.lower() in self._inc_exts]
        if self._exc_exts:
            entries = [e for e in entries
                       if Path(e[0]).suffix.lower() not in self._exc_exts]
        if self._inc_srcs:
            entries = [e for e in entries
                       if tf.entry_source(e[1]) in self._inc_srcs]
        if self._exc_srcs:
            entries = [e for e in entries
                       if tf.entry_source(e[1]) not in self._exc_srcs]
        if self._search:
            q = self._search
            entries = [e for e in entries
                       if q in e[0].casefold() or q in e[1].casefold()]
        self._model.set_root(self._build_tree(entries))
        # When filtering (search/content), expand so matches are visible.
        if self._search or self._content_matches is not None:
            self._tree.expandAll()
        else:
            # Keep the top-level source nodes open; folders within stay collapsed.
            for r in range(self._model.rowCount()):
                self._tree.expand(self._model.index(r, 0))

    def _build_tree(self, entries) -> _TextNode:
        """Build a source → folder → file tree. Each source is a top-level node;
        files nest into their real folder hierarchy (collapsible — a profile can
        have thousands of files)."""
        labels = dict(tf.SOURCE_LABELS)
        root = _TextNode("", is_dir=True)
        src_nodes: dict[str, _TextNode] = {}
        # Per-source folder lookup so we don't rescan children each insert.
        folders: dict[tuple[str, str], _TextNode] = {}

        for rel, mod, full in entries:
            src = tf.entry_source(mod)
            snode = src_nodes.get(src)
            if snode is None:
                snode = _TextNode(labels.get(src, src), is_dir=True, parent=root)
                root.children.append(snode)
                src_nodes[src] = snode
            parts = rel.replace("\\", "/").split("/")
            parent = snode
            path_so_far = ""
            for seg in parts[:-1]:
                path_so_far = f"{path_so_far}/{seg}" if path_so_far else seg
                key = (src, path_so_far.lower())
                fnode = folders.get(key)
                if fnode is None:
                    fnode = _TextNode(seg, is_dir=True, parent=parent)
                    parent.children.append(fnode)
                    folders[key] = fnode
                parent = fnode
            parent.children.append(_TextNode(
                parts[-1], is_dir=False, parent=parent,
                full_path=full, mod=mod, rel_path=rel))
        return root

    # -- filter spec / state ------------------------------------------------
    def filter_spec(self) -> list[dict]:
        return [
            {"title": "By source", "type": "checks", "items": [
                ("src_mod", "Mod folders", True),
                ("src_profile", "Profile", True),
                ("src_game", "Game folder", True),
                ("src_mygames", "My Games", True),
            ]},
            {"title": "By file type", "type": "dynamic", "id": "filetypes"},
        ]

    def apply_filter_state(self, state: dict):
        # Source tri-state checks → include/exclude source keys.
        key_map = {"src_mod": "mod", "src_profile": "profile",
                   "src_game": "game", "src_mygames": "mygames"}
        self._inc_srcs = {key_map[k] for k, v in key_map.items()
                          if state.get(k) == 1}
        self._exc_srcs = {key_map[k] for k, v in key_map.items()
                          if state.get(k) == 2}
        self._inc_exts = set(state.get("filetypes") or ())
        self._exc_exts = set(state.get("filetypes_exclude") or ())
        self._apply()

    def filetype_items(self) -> list[tuple]:
        from collections import Counter
        c = Counter(Path(e[0]).suffix.lower() for e in self._all_entries)
        return [(ext or "(none)", ext or "(no ext)", n)
                for ext, n in sorted(c.items())]

    # -- search -------------------------------------------------------------
    def _on_search(self, text: str):
        self._search = (text or "").strip().casefold()
        t = getattr(self, "_search_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(150)
            t.timeout.connect(self._apply)
            self._search_timer = t
        t.start()

    # -- content search -----------------------------------------------------
    def run_content_search(self, keyword: str):
        keyword = (keyword or "").strip()
        if not keyword:
            self.clear_content_search()
            return
        self._content_keyword = keyword
        self._content_matches = tf.content_search(self._all_entries, keyword)
        self.content_status_changed.emit(keyword)
        self._apply()

    def clear_content_search(self):
        self._content_keyword = None
        self._content_matches = None
        self.content_status_changed.emit(None)
        self._apply()

    # -- expand (no folders, but kept for footer parity) --------------------
    def _toggle_expand_all(self) -> bool:
        return True

    # -- click → expand folder / open file ----------------------------------
    def _on_clicked(self, index):
        node = self._model.node(index)
        if node is None or node is self._model._root:
            return
        if node.is_dir:
            if self._model.rowCount(index) > 0:
                self._tree.setExpanded(index, not self._tree.isExpanded(index))
            return
        if (self.on_open_file is not None and node.full_path is not None
                and node.full_path.is_file()):
            # Pass the active content-search keyword so the editor pre-highlights it.
            self.on_open_file(node.full_path, node.rel_path,
                              self._content_keyword)
