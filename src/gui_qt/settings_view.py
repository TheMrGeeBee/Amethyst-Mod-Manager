"""Settings view — a panel-scoped tab that overlays the Modlist panel.

Opened from the top-bar gear button (app.py `_open_settings_tab`) via
`DetachableTabWidget.open_scoped_tab(..., self._modlist_panel_stack, key="settings")`
— the same modlist-scoped mechanism as the image preview / text editor. The
modlist content is swapped out for this widget while the rest of the UI (plugins
panel, headers, footers) stays live; closing the tab restores the modlist.

Save-on-change: every control writes straight to amethyst.ini through the
toolkit-free `Utils.ui_config` load_*/save_* helpers the moment it changes — there
is no Save/Cancel button. A couple of settings (Font, Appearance) only take effect
on restart and say so inline.

A curated subset of the Tk Settings panel (gui/status_bar.py `SettingsPanel`):
User Interface, Downloads & Collections, General, Appearance, Paths — plus a
Manage Caches action. Theme colour pickers are intentionally omitted (Qt has no
colour-override system yet).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QScrollArea, QFrame,
    QLabel, QCheckBox, QComboBox, QSlider, QLineEdit, QPushButton, QGroupBox,
    QMessageBox,
)

from gui_qt.theme_qt import active_palette, _c
from Utils import ui_config as uc


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
class SettingsView(QWidget):
    # Carries the cache-size scan result from a daemon worker thread to the UI
    # thread (queued — thread-safe). A plain QThread crashed on app exit if the
    # rglob was still running; a daemon thread is safely abandoned instead.
    _cache_size_ready = Signal(int)

    def __init__(self, window):
        super().__init__()
        self._window = window          # main window — for _notify, threads
        self._pal = active_palette()
        self.setObjectName("SettingsView")
        self._cache_scanning = False
        self._cache_size_ready.connect(self._on_cache_size)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)

        body = QWidget()
        scroll.setWidget(body)
        self._v = QVBoxLayout(body)
        self._v.setContentsMargins(16, 14, 16, 18)
        self._v.setSpacing(14)

        title = QLabel("Settings")
        f = title.font(); f.setPointSize(f.pointSize() + 4); f.setBold(True)
        title.setFont(f)
        self._v.addWidget(title)

        self.setStyleSheet(self._qss())

        self._build_user_interface()
        self._build_downloads()
        self._build_general()
        self._build_appearance()
        self._build_paths()
        self._v.addStretch(1)

    # ---- styling ----------------------------------------------------------
    def _qss(self) -> str:
        c = lambda k: _c(self._pal, k)
        return f"""
        QGroupBox {{
            border: 1px solid {c('BORDER')};
            border-radius: 6px;
            margin-top: 10px;
            padding: 10px 12px 12px 12px;
            background: {c('BG_PANEL')};
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 10px; padding: 0 5px;
            color: {c('TEXT_MAIN')};
            font-weight: bold;
        }}
        QLabel#Help {{ color: {c('TEXT_DIM')}; }}
        QLabel#RestartNote {{ color: {c('TEXT_WARN')}; }}
        QSlider::groove:horizontal {{
            height: 4px; background: {c('BG_DEEP')}; border-radius: 2px;
        }}
        QSlider::handle:horizontal {{
            background: {c('ACCENT')}; width: 14px; margin: -6px 0;
            border-radius: 7px;
        }}
        QSlider::sub-page:horizontal {{ background: {c('ACCENT')}; border-radius: 2px; }}
        """

    # ---- section + control builders --------------------------------------
    def _section(self, title: str) -> QGridLayout:
        """Add a QGroupBox and return a QGridLayout to fill (label | control)."""
        box = QGroupBox(title)
        grid = QGridLayout(box)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)
        self._v.addWidget(box)
        # Track the next free row per grid via a dynamic attribute.
        grid.setProperty("_row", 0)
        return grid

    def _next_row(self, grid: QGridLayout) -> int:
        r = int(grid.property("_row") or 0)
        grid.setProperty("_row", r + 1)
        return r

    def _add_help(self, grid: QGridLayout, text: str) -> None:
        lbl = QLabel(text)
        lbl.setObjectName("Help")
        lbl.setWordWrap(True)
        grid.addWidget(lbl, self._next_row(grid), 0, 1, 2)

    def _checkbox(self, grid: QGridLayout, label: str, load_fn, save_fn,
                  help: str | None = None, on_changed=None) -> QCheckBox:
        cb = QCheckBox(label)
        try:
            cb.setChecked(bool(load_fn()))
        except Exception:
            pass

        def _toggled(v):
            self._safe_save(save_fn, v)
            if on_changed is not None:
                try:
                    on_changed(v)
                except Exception:
                    pass

        cb.toggled.connect(_toggled)
        grid.addWidget(cb, self._next_row(grid), 0, 1, 2)
        if help:
            self._add_help(grid, help)
        return cb

    def _combo(self, grid: QGridLayout, label: str,
               pairs: list[tuple[str, str]], current_value: str, save_fn,
               restart_note: bool = False) -> QComboBox:
        """`pairs` = [(display, value), ...]; selecting saves the value."""
        row = self._next_row(grid)
        grid.addWidget(QLabel(label), row, 0)
        combo = QComboBox()
        values = [v for _d, v in pairs]
        for disp, _v in pairs:
            combo.addItem(disp)
        if current_value in values:
            combo.setCurrentIndex(values.index(current_value))
        combo.currentIndexChanged.connect(
            lambda i: self._safe_save(save_fn, values[i]))
        grid.addWidget(combo, row, 1, Qt.AlignLeft)
        if restart_note:
            note = QLabel("Changes take effect after restart.")
            note.setObjectName("RestartNote")
            grid.addWidget(note, self._next_row(grid), 0, 1, 2)
        return combo

    def _slider(self, grid: QGridLayout, label: str, lo: int, hi: int,
                value: int, on_change) -> QSlider:
        """Integer slider lo..hi with a live value label. `on_change(int)`."""
        row = self._next_row(grid)
        grid.addWidget(QLabel(label), row, 0)
        wrap = QHBoxLayout()
        sld = QSlider(Qt.Horizontal)
        sld.setMinimum(lo); sld.setMaximum(hi)
        sld.setValue(max(lo, min(hi, value)))
        sld.setFixedWidth(220)
        val_lbl = QLabel(str(sld.value()))
        val_lbl.setMinimumWidth(48)
        sld.valueChanged.connect(lambda v: val_lbl.setText(str(v)))
        sld.valueChanged.connect(lambda v: on_change(v))
        wrap.addWidget(sld)
        wrap.addWidget(val_lbl)
        wrap.addStretch(1)
        holder = QWidget(); holder.setLayout(wrap)
        grid.addWidget(holder, row, 1)
        return sld, val_lbl

    def _path_row(self, grid: QGridLayout, label: str, load_fn, save_fn,
                  help: str | None = None) -> QLineEdit:
        row = self._next_row(grid)
        grid.addWidget(QLabel(label), row, 0)
        wrap = QHBoxLayout()
        edit = QLineEdit()
        try:
            edit.setText(load_fn() or "")
        except Exception:
            pass
        edit.editingFinished.connect(
            lambda: self._safe_save(save_fn, edit.text().strip()))
        browse = QPushButton("Browse")
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(lambda: self._browse_into(edit, save_fn, label))
        clear = QPushButton("Clear")
        clear.setCursor(Qt.PointingHandCursor)
        clear.clicked.connect(lambda: self._clear_path(edit, save_fn))
        wrap.addWidget(edit, 1)
        wrap.addWidget(browse)
        wrap.addWidget(clear)
        holder = QWidget(); holder.setLayout(wrap)
        grid.addWidget(holder, row, 1)
        if help:
            self._add_help(grid, help)
        return edit

    # ---- sections ---------------------------------------------------------
    def _build_user_interface(self):
        # NB: no UI Scale / Font here — Qt scales via the OS/compositor natively
        # (unlike Tk/CustomTkinter, which had to reimplement HiDPI), so a manual
        # scale slider + font picker would be dead controls.
        g = self._section("User Interface")
        self._checkbox(
            g, "Hide BSA conflicts",
            uc.load_hide_bsa_conflicts, uc.save_hide_bsa_conflicts,
            help="Hide BSA/BA2 archive conflict flags (also skips that "
                 "conflict scan for a small speed-up).",
            on_changed=lambda _v: self._rebuild_conflicts())

    def _build_downloads(self):
        g = self._section("Downloads & Collections")
        try:
            cs = uc.load_collection_settings()
        except Exception:
            cs = {}
        self._cs = {
            "max_concurrent": int(cs.get("max_concurrent", 3)),
            "max_extract_workers": int(cs.get("max_extract_workers", 4)),
            "check_download_locations": bool(cs.get("check_download_locations", True)),
            "clear_archive_after_install": bool(cs.get("clear_archive_after_install", False)),
        }

        self._checkbox(
            g, "Clear archive after install",
            uc.load_clear_archive_after_install,
            uc.save_clear_archive_after_install,
            help="Delete a mod's downloaded archive after it is extracted.")
        self._checkbox(
            g, "Keep FOMOD archives",
            uc.load_keep_fomod_archives, uc.save_keep_fomod_archives,
            help="Mods installed via a FOMOD installer keep their archive even "
                 "when 'Clear archive after install' is on.")

        # Collection settings — all persisted together via save_collection_settings.
        self._slider(
            g, "Max concurrent downloads", 1, 8, self._cs["max_concurrent"],
            self._save_max_concurrent)
        self._slider(
            g, "Max extractions", 1, 8, self._cs["max_extract_workers"],
            self._save_max_extract)
        self._add_help(
            g, "Extractions are gated by available memory; the effective number "
               "may be lower than set.")
        self._checkbox(
            g, "Check downloads locations",
            self._load_check_dl_locations, self._save_check_dl_locations,
            help="Scan the system Downloads folder (and any custom locations) "
                 "for an archive before downloading it again.")

        # Manage Caches action.
        row = self._next_row(g)
        g.addWidget(QLabel("Caches"), row, 0)
        self._cache_btn = QPushButton("Manage Caches… (—)")
        self._cache_btn.setCursor(Qt.PointingHandCursor)
        self._cache_btn.clicked.connect(self._on_manage_caches)
        cwrap = QHBoxLayout()
        cwrap.addWidget(self._cache_btn)
        cwrap.addStretch(1)
        holder = QWidget(); holder.setLayout(cwrap)
        g.addWidget(holder, row, 1)
        self._refresh_cache_size()

    def _build_general(self):
        g = self._section("General")
        self._checkbox(
            g, "Normalise folder casing",
            uc.load_normalize_folder_case, uc.save_normalize_folder_case,
            help="Unify folder names to a single casing across mods. Disable on "
                 "case-insensitive filesystems.")
        self._checkbox(
            g, "Rename mod after install",
            uc.load_rename_mod_after_install, uc.save_rename_mod_after_install,
            help="Show a rename prompt after installing a mod.")
        self._checkbox(
            g, "Restore on close",
            uc.load_restore_on_close, uc.save_restore_on_close,
            help="Restore all deployed games to vanilla when the app is closed.")
        self._checkbox(
            g, "Use pre-release versions",
            uc.load_allow_prerelease, uc.save_allow_prerelease,
            help="Also offer beta and release-candidate app builds when checking "
                 "for updates.")

    def _build_appearance(self):
        g = self._section("Appearance")
        try:
            from gui.themes import load_display_names
            themes = load_display_names() or {"dark": "Dark"}
        except Exception:
            themes = {"dark": "Dark", "light": "Light"}
        try:
            current = uc.get_appearance_mode()
        except Exception:
            current = "dark"
        pairs = [(disp, tid) for tid, disp in themes.items()]
        self._combo(g, "Appearance", pairs, current,
                    uc.save_appearance_mode, restart_note=True)

    def _build_paths(self):
        g = self._section("Paths")
        from Utils.config_paths import get_config_dir
        base = get_config_dir()
        self._path_row(
            g, "Default Mod Staging Folder",
            uc.load_default_staging_path, uc.save_default_staging_path,
            help=f"When set, games added after this point stage mods here. "
                 f"Blank = default ({base / 'Profiles'}).")
        self._path_row(
            g, "Download Cache Folder",
            uc.load_download_cache_path, uc.save_download_cache_path,
            help=f"Where downloaded mod archives are stored. "
                 f"Blank = default ({base / 'download_cache'}).")
        self._path_row(
            g, "Heroic Config Location",
            uc.load_heroic_config_path, uc.save_heroic_config_path,
            help="Folder containing Heroic's config.json. Blank = auto-detect "
                 "(Flatpak and native locations).")
        self._path_row(
            g, "Steam libraryfolders.vdf",
            uc.load_steam_libraries_vdf_path, uc.save_steam_libraries_vdf_path,
            help="Path to libraryfolders.vdf (or its folder). Blank = auto-detect "
                 "(standard, Flatpak and Snap locations).")

    # ---- collection setting handlers (all persist the whole group) --------
    def _persist_collection(self):
        self._safe_save(
            uc.save_collection_settings,
            self._cs["max_concurrent"],
            self._cs["check_download_locations"],
            self._cs["clear_archive_after_install"],
            self._cs["max_extract_workers"])

    def _save_max_concurrent(self, value: int):
        self._cs["max_concurrent"] = int(value)
        self._persist_collection()

    def _save_max_extract(self, value: int):
        self._cs["max_extract_workers"] = int(value)
        self._persist_collection()

    def _load_check_dl_locations(self) -> bool:
        return self._cs["check_download_locations"]

    def _save_check_dl_locations(self, value: bool):
        self._cs["check_download_locations"] = bool(value)
        self._persist_collection()

    # ---- path browse / clear ----------------------------------------------
    def _browse_into(self, edit: QLineEdit, save_fn, title: str):
        from Utils.portal_filechooser import pick_folder

        def _chosen(path):
            if path:
                edit.setText(str(path))
                self._safe_save(save_fn, str(path))

        pick_folder(f"Select {title}", _chosen)

    def _clear_path(self, edit: QLineEdit, save_fn):
        edit.clear()
        self._safe_save(save_fn, "")

    # ---- Manage Caches ----------------------------------------------------
    def _refresh_cache_size(self):
        """Scan the cache size on a daemon thread and push the result to the UI
        thread via _cache_size_ready. Daemon (not QThread) so app exit mid-scan
        doesn't abort — the thread is just abandoned, and the queued signal is
        dropped if the widget is gone."""
        if self._cache_scanning:
            return
        self._cache_scanning = True
        import threading

        def worker():
            try:
                from Utils.cache_tools import total_cache_size
                size = total_cache_size()
            except Exception:
                size = 0
            try:
                self._cache_size_ready.emit(size)
            except RuntimeError:
                pass   # widget (and its signal) already destroyed

        threading.Thread(target=worker, daemon=True).start()

    def _on_cache_size(self, size: int):
        self._cache_scanning = False
        from Utils.cache_tools import format_size
        if self._cache_btn is not None:
            self._cache_btn.setText(f"Manage Caches… ({format_size(size)})")

    def _on_manage_caches(self):
        from Utils.cache_tools import (
            total_cache_size, orphaned_tmp_dirs, format_size,
            clear_download_cache,
        )
        size = total_cache_size()
        n_orphans = len(orphaned_tmp_dirs())
        msg = (f"Clear the download cache?\n\n"
               f"This removes cached mod archives"
               + (f" and {n_orphans} leftover temp folder(s)" if n_orphans else "")
               + f".\nTotal to free: {format_size(size)}")
        box = QMessageBox(self)
        box.setWindowTitle("Manage Caches")
        box.setText(msg)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        if box.exec() != QMessageBox.Yes:
            return
        removed = clear_download_cache()
        self._notify(f"Cleared {removed} cache item(s).", "info")
        self._refresh_cache_size()

    # ---- helpers ----------------------------------------------------------
    def _rebuild_conflicts(self):
        """Ask the window to rebuild conflicts so a setting that affects them
        (e.g. Hide BSA conflicts) applies live without a manual refresh."""
        win = self._window
        if win is not None and hasattr(win, "_rebuild_conflicts_async"):
            try:
                win._rebuild_conflicts_async()
            except Exception:
                pass

    def _safe_save(self, save_fn, *args):
        try:
            save_fn(*args)
        except Exception as exc:
            self._notify(f"Failed to save setting: {exc}", "warning")

    def _notify(self, text: str, state: str = "info"):
        win = self._window
        if win is not None and hasattr(win, "_notify"):
            try:
                win._notify(text, state)
                return
            except Exception:
                pass
        print(f"[settings] {text}")
