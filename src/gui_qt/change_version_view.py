"""Change Version overlay — lists a mod's Nexus files so the user can install a
different version. Opens as a plugins-panel-scoped tab (covers the whole plugins
panel). Qt port of the Tk gui/mod_files_overlay.py; shares the pure highlight /
sort helpers in Utils.mod_files_versions.

The file list is fetched on a daemon thread (a Signal marshals the result back to
the UI thread — never a QThread). Installing a chosen file reuses the same
download → build_meta → install_fn flow as the Nexus browser tab.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QCheckBox, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
)

from gui_qt.theme_qt import active_palette, _c, danger_close_button, button_qss, qc
from gui_qt.safe_emit import safe_emit
from Utils.mod_files_versions import resolve_latest_name_match, fmt_size, sort_key

# File-row highlight colours, resolved from the active theme so a monotone /
# high-contrast theme actually takes effect (were hardcoded Tk hex before).
def _hl_colors(p: dict | None = None) -> dict[str, QColor]:
    p = p or active_palette()
    return {
        "installed_bg": qc(p, "BG_GREEN_DEEP"),
        "installed_fg": qc(p, "TEXT_OK_BRIGHT"),
        "match_bg":     qc(p, "BG_ORANGE_DEEP"),
        "match_fg":     qc(p, "STATUS_QUEUED"),
        "old_bg":       qc(p, "BG_RED_DEEP"),
        "old_fg":       qc(p, "TEXT_ERR_BRIGHT"),
    }


_COLS = ["File", "Version", "Category", "Size", ""]


class _LegendBar(QWidget):
    """Highlight key for the version list. Centered, and reflows between one row
    (4 across, when there's width) and 2×2 (when narrow) so text never clips. In
    2×2 the columns line up: installed/older on the left, newest/none on the
    right (col 0 = items 0 & 2, col 1 = items 1 & 3)."""

    _COL_GAP = 16
    _MARGIN = 12

    def __init__(self, p):
        super().__init__()
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(self._MARGIN, 4, self._MARGIN, 4)
        self._grid.setHorizontalSpacing(self._COL_GAP)
        self._grid.setVerticalSpacing(2)
        hl = _hl_colors(p)
        legend_items = [
            (hl["installed_fg"], "Currently installed"),
            (hl["match_fg"], "Newest matching version"),
            (hl["old_fg"], "Older matching version"),
            (None, "No name match"),
        ]
        self._entries = [self._make_entry(p, c, lbl) for c, lbl in legend_items]
        self._cols = 0          # force first layout
        self._one_row_min = self._one_row_width()
        self._relayout(force=True)

    def _make_entry(self, p, color, label) -> QWidget:
        e = QWidget()
        h = QHBoxLayout(e); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(6)
        sw = QLabel(); sw.setFixedSize(12, 12)
        bg = color.name() if color is not None else _c(p, "BG_DEEP")
        border = "" if color is not None else f" border:1px solid {_c(p,'TEXT_DIM')};"
        sw.setStyleSheet(f"background:{bg}; border-radius:2px;{border}")
        h.addWidget(sw)
        lbl = QLabel(label); lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        h.addWidget(lbl)
        return e

    def _one_row_width(self) -> int:
        """Pixel width needed to show all 4 entries on one row."""
        w = sum(e.sizeHint().width() for e in self._entries)
        w += self._COL_GAP * (len(self._entries) - 1)
        return w + self._MARGIN * 2

    def _relayout(self, force=False):
        avail = self.width()
        cols = 4 if avail >= self._one_row_min else 2
        if cols == self._cols and not force:
            return
        self._cols = cols
        # Detach all entries (without deleting them).
        while self._grid.count():
            self._grid.takeAt(0)
        for e in self._entries:
            self._grid.removeWidget(e)
        # Content occupies grid columns 1..cols; cols 0 and cols+1 stretch so the
        # whole block stays centered no matter how wide the panel gets. In 2×2,
        # placing row-major keeps the top pair (installed, newest) above the
        # bottom pair (older, none): col 0 = installed/older, col 1 = newest/none.
        for i, e in enumerate(self._entries):
            r = (i // cols) if cols == 2 else 0
            c = (i % cols) if cols == 2 else i
            self._grid.addWidget(e, r, c + 1, Qt.AlignLeft | Qt.AlignVCenter)
            e.show()
        for c in range(self._grid.columnCount()):
            self._grid.setColumnStretch(c, 0)
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(cols + 1, 1)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()


class ChangeVersionView(QWidget):
    """Scoped-tab body for picking a mod version to install."""

    # (files | None, error_msg) from the fetch worker → UI thread.
    _files_ready = Signal(object, object)
    # (archive | None, meta | None) from the download worker → UI thread.
    _download_done = Signal(object, object)

    def __init__(self, api, game, mod_name, meta, install_fn,
                 on_close, log_fn=None):
        super().__init__()
        self._api = api
        self._game = game
        self._mod_name = mod_name
        self._meta = meta
        self._install_fn = install_fn or (lambda paths, metas=None: None)
        self._on_close = on_close or (lambda: None)
        self._log = log_fn or (lambda _m: None)
        self._installing = False

        self.setObjectName("ChangeVersionView")
        self._files_ready.connect(self._on_files_ready)
        self._download_done.connect(self._on_download_done)

        self._build()
        self._start_fetch()

    # ---- layout -----------------------------------------------------------
    def _build(self):
        p = active_palette()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Toolbar: title + Ignore Update + Close.
        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8); hb.setSpacing(8)
        title = QLabel(self.tr("Change Version — {0}").format(self._mod_name))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        hb.addWidget(title)
        hb.addStretch(1)

        self._ignore_cb = QCheckBox(self.tr("Ignore Update"))
        self._ignore_cb.setToolTip(
            self.tr("Stop flagging this mod as having an update until a newer version "
            "than the current latest appears."))
        self._ignore_cb.setChecked(bool(getattr(self._meta, "ignore_update", False)))
        self._ignore_cb.toggled.connect(self._on_ignore_toggled)
        hb.addWidget(self._ignore_cb)

        close = danger_close_button(pal=p)
        close.clicked.connect(lambda: self._on_close())
        hb.addWidget(close)
        v.addWidget(bar)

        # Highlight key — explains the row tints. Reflows 1 row ↔ 2×2, centered.
        v.addWidget(_LegendBar(p))

        # File table.
        self._table = QTableWidget(0, len(_COLS))
        self._table.setHorizontalHeaderLabels(_COLS)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setFocusPolicy(Qt.NoFocus)
        self._table.setShowGrid(False)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)        # File
        for c in (1, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)  # buttons
        v.addWidget(self._table, 1)

        # Status line (loading / empty / error).
        self._status = QLabel(self.tr("Loading files…"))
        self._status.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; padding:8px 12px;")
        v.addWidget(self._status)

    # ---- fetch ------------------------------------------------------------
    def _start_fetch(self):
        domain = getattr(self._game, "nexus_game_domain", "") or \
            getattr(self._meta, "game_domain", "") or ""
        mod_id = int(getattr(self._meta, "mod_id", 0) or 0)

        def worker():
            try:
                resp = self._api.get_mod_files(domain, mod_id)
                safe_emit(self._files_ready, list(resp.files), None)
            except Exception as exc:
                safe_emit(self._files_ready, None, str(exc))

        threading.Thread(target=worker, daemon=True, name="change-version-fetch").start()

    def _on_files_ready(self, files, error):
        if error is not None:
            self._status.setText(self.tr("Could not load files: {0}").format(error))
            self._status.setVisible(True)
            return
        if not files:
            self._status.setText(self.tr("No files found."))
            self._status.setVisible(True)
            return
        self._status.setVisible(False)
        self._populate(sorted(files, key=sort_key))

    # ---- table population + highlight ------------------------------------
    def _populate(self, files):
        installed_id = int(getattr(self._meta, "file_id", 0) or 0)
        match_id, old_ids = resolve_latest_name_match(
            files, installed_id, self._mod_name)
        domain = getattr(self._game, "nexus_game_domain", "") or \
            getattr(self._meta, "game_domain", "") or ""
        mod_id = int(getattr(self._meta, "mod_id", 0) or 0)

        hl = _hl_colors()
        self._table.setRowCount(len(files))
        for row, f in enumerate(files):
            is_installed = installed_id > 0 and f.file_id == installed_id
            is_match = not is_installed and match_id > 0 and f.file_id == match_id
            is_old = not is_installed and not is_match and f.file_id in old_ids
            if is_installed:
                bg, name_fg = hl["installed_bg"], hl["installed_fg"]
            elif is_match:
                bg, name_fg = hl["match_bg"], hl["match_fg"]
            elif is_old:
                bg, name_fg = hl["old_bg"], hl["old_fg"]
            else:
                bg = name_fg = None

            name_text = (f.name or f.file_name or "") + ("  ✓" if is_installed else "")
            size = f.size_in_bytes or (f.size_kb * 1024 if f.size_kb else 0)
            cells = [name_text, f.version or "",
                     (f.category_name or "").capitalize(), fmt_size(size)]
            for col, text in enumerate(cells):
                it = QTableWidgetItem(text)
                if bg is not None:
                    it.setBackground(bg)
                if col == 0 and name_fg is not None:
                    it.setForeground(name_fg)
                self._table.setItem(row, col, it)

            # Buttons cell (View + Install).
            cell = QWidget()
            if bg is not None:
                cell.setAutoFillBackground(True)
                cell.setStyleSheet(f"background:{bg.name()};")
            cb = QHBoxLayout(cell); cb.setContentsMargins(8, 4, 8, 4); cb.setSpacing(6)
            view_url = (f"https://www.nexusmods.com/{domain}/mods/{mod_id}"
                        f"?tab=files&file_id={f.file_id}")
            view_btn = QPushButton(self.tr("View")); view_btn.setCursor(Qt.PointingHandCursor)
            view_btn.setStyleSheet(button_qss("BTN_GREY", padding="4px 10px"))
            view_btn.clicked.connect(lambda _=False, u=view_url: self._open_url(u))
            cb.addWidget(view_btn)
            inst_btn = QPushButton(self.tr("Install")); inst_btn.setCursor(Qt.PointingHandCursor)
            # Explicit success colour so the row tint (set on the parent cell)
            # can't bleed into the button background.
            inst_btn.setStyleSheet(button_qss("BTN_SUCCESS", padding="4px 10px"))
            inst_btn.clicked.connect(
                lambda _=False, ff=f: self._install_file(ff))
            cb.addWidget(inst_btn)
            cb.addStretch(1)
            self._table.setCellWidget(row, 4, cell)

    # ---- actions ----------------------------------------------------------
    def _open_url(self, url):
        try:
            from Utils.xdg import open_url
            open_url(url)
        except Exception:
            pass

    def _on_ignore_toggled(self, state):
        """Write ignore_update (+ ignored_version) to the mod's meta.ini. The
        modlist flag refresh happens when the overlay closes (_reload_modlist)."""
        staging = getattr(self._game, "get_effective_mod_staging_path", None)
        try:
            from Nexus.nexus_meta import read_meta, write_meta
            mp = (self._game.get_effective_mod_staging_path()
                  if staging else None)
            if mp is None:
                return
            meta_path = mp / self._mod_name / "meta.ini"
            m = read_meta(meta_path)
            m.ignore_update = bool(state)
            if state:
                m.has_update = False
                m.ignored_version = m.latest_version
            else:
                m.ignored_version = ""
            write_meta(meta_path, m)
            self._meta = m
        except Exception as exc:
            self._log(f"Nexus: could not save ignore flag — {exc}")

    def _install_file(self, f):
        if self._installing:
            return
        self._installing = True
        domain = getattr(self._game, "nexus_game_domain", "") or \
            getattr(self._meta, "game_domain", "") or ""
        mod_id = int(getattr(self._meta, "mod_id", 0) or 0)
        self._log(f"Nexus: downloading {f.file_name or f.name}…")

        # A minimal mod_info-like object for build_meta_from_download.
        class _Info:
            pass
        info = _Info()
        info.mod_id = mod_id
        info.domain_name = domain
        info.name = getattr(self._meta, "nexus_name", "") or self._mod_name

        def worker():
            archive = meta = None
            try:
                from Nexus.nexus_download import NexusDownloader
                from Utils.config_paths import get_download_cache_dir_for_game
                from Nexus.nexus_meta import build_meta_from_download
                dest = get_download_cache_dir_for_game(
                    getattr(self._game, "name", "") or "")
                size = (f.size_in_bytes or 0) or (f.size_kb * 1024)
                result = NexusDownloader(self._api, download_dir=dest).download_file(
                    game_domain=domain, mod_id=mod_id, file_id=f.file_id,
                    dest_dir=dest, known_file_name=f.file_name,
                    expected_size_bytes=size, progress_cb=lambda d, t: None)
                if result.success and result.file_path is not None:
                    archive = str(result.file_path)
                    try:
                        meta = build_meta_from_download(
                            game_domain=domain, mod_id=mod_id, file_id=f.file_id,
                            archive_name=result.file_name, mod_info=info, file_info=f)
                    except Exception:
                        meta = None
                else:
                    self._log(f"Nexus: download failed: "
                              f"{result.error or 'unknown error'}")
            except Exception as exc:
                self._log(f"Nexus: download error: {exc}")
            safe_emit(self._download_done, archive, meta)

        threading.Thread(target=worker, daemon=True, name="change-version-dl").start()

    def _on_download_done(self, archive, meta):
        self._installing = False
        if not archive:
            return
        self._log(f"Nexus: downloaded → {archive}; installing…")
        # Pass the mod we're updating so the app can offer "Remove previous
        # version?" if the new file installs under a different folder name.
        metas = {archive: meta} if meta is not None else None
        try:
            self._install_fn([archive], metas,
                             previous_mod_name=self._mod_name)
        except TypeError:
            # install_fn without the previous_mod_name kwarg (defensive).
            self._install_fn([archive], metas)
        # Close the panel now — the install runs asynchronously and its own
        # completion path refreshes the modlist (+ any "Remove previous?" prompt).
        self._on_close()
