"""Restore backup overlay — lists a profile's backups (snapshots of
modlist.txt / plugins.txt / state JSON taken before every deploy) so the user
can restore one, mark it "kept" (never pruned), or create a fresh backup.

Opens as a plugins-panel-scoped tab (covers the whole plugins panel while the
modlist stays live). Qt port of the Tk gui/backup_restore_dialog.py; reuses the
neutral backup logic in Utils.profile_backup verbatim.

Backup operations are fast local file copies, so everything runs synchronously
on the UI thread — no worker/Signal marshalling needed.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QAbstractItemView,
)

from gui_qt.theme_qt import active_palette, _c, danger_close_button
from Utils.profile_backup import (
    create_backup, list_backups, restore_backup,
    is_backup_kept, set_backup_kept,
)

# Exact Tk highlight colours (backup_restore_dialog.py) — kept for parity.
_KEEP_BG = QColor("#1a3a1a")
_KEEP_FG = QColor("#6ecf6e")


class BackupRestoreView(QWidget):
    """Scoped-tab body listing profile backups with restore / keep / create."""

    def __init__(self, profile_dir: Path, profile_name: str = "default",
                 on_restored=None, on_close=None, log_fn=None):
        super().__init__()
        self._profile_dir = Path(profile_dir)
        self._profile_name = profile_name
        self._on_restored = on_restored or (lambda: None)
        self._on_close = on_close or (lambda: None)
        self._log = log_fn or (lambda _m: None)
        self._backups: list = []

        self.setObjectName("BackupRestoreView")
        self._build()
        self._reload_list()

    # ---- layout -----------------------------------------------------------
    def _build(self):
        p = active_palette()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Toolbar: title + Close.
        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8); hb.setSpacing(8)
        title = QLabel(self.tr("Restore backup — {0}").format(self._profile_name))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        hb.addWidget(title)
        hb.addStretch(1)
        close = danger_close_button(pal=p)
        close.clicked.connect(lambda: self._on_close())
        hb.addWidget(close)
        v.addWidget(bar)

        # Instruction line.
        info = QLabel(
            self.tr("Select a backup to restore the modlist and plugins for this profile."))
        info.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; padding:8px 12px 4px 12px;")
        v.addWidget(info)

        # Backup list.
        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._list.itemSelectionChanged.connect(self._on_selection)
        v.addWidget(self._list, 1)

        # Empty-state label (shown in place of the list when there are none).
        self._empty = QLabel(self.tr("No backups yet. Backups are created when you deploy."))
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; padding:24px;")
        self._empty.setVisible(False)
        v.addWidget(self._empty, 1)

        # Button row: New backup (left) | Keep, Cancel, Restore (right).
        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(12, 8, 12, 12); rh.setSpacing(8)
        self._new_btn = QPushButton(self.tr("New backup"))
        self._new_btn.clicked.connect(self._on_create)
        rh.addWidget(self._new_btn)
        rh.addStretch(1)
        self._keep_btn = QPushButton(self.tr("Keep"))
        self._keep_btn.setEnabled(False)
        self._keep_btn.clicked.connect(self._on_keep)
        rh.addWidget(self._keep_btn)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.clicked.connect(lambda: self._on_close())
        rh.addWidget(cancel)
        self._restore_btn = QPushButton(self.tr("Restore"))
        self._restore_btn.setEnabled(False)
        self._restore_btn.clicked.connect(self._on_restore)
        rh.addWidget(self._restore_btn)
        v.addWidget(row)

    # ---- data -------------------------------------------------------------
    def _reload_list(self):
        self._backups = list_backups(self._profile_dir)
        self._list.clear()
        for dt, bdir in self._backups:
            label = dt.strftime("%Y-%m-%d %H:%M:%S")
            kept = is_backup_kept(bdir)
            if kept:
                label += "  [kept]"
            item = QListWidgetItem(label)
            if kept:
                item.setBackground(_KEEP_BG)
                item.setForeground(_KEEP_FG)
            self._list.addItem(item)
        has_any = bool(self._backups)
        self._list.setVisible(has_any)
        self._empty.setVisible(not has_any)
        self._on_selection()

    def _selected_index(self) -> int:
        return self._list.currentRow() if self._backups else -1

    # ---- handlers ---------------------------------------------------------
    def _on_selection(self):
        idx = self._selected_index()
        has_sel = 0 <= idx < len(self._backups)
        self._restore_btn.setEnabled(has_sel)
        self._keep_btn.setEnabled(has_sel)
        if has_sel:
            _dt, bdir = self._backups[idx]
            self._keep_btn.setText(self.tr("Unkeep") if is_backup_kept(bdir) else self.tr("Keep"))
        else:
            self._keep_btn.setText(self.tr("Keep"))

    def _on_create(self):
        try:
            create_backup(self._profile_dir, log_fn=self._log)
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the tab
            self._log(f"[backup] create failed: {exc}")
        self._reload_list()

    def _on_keep(self):
        idx = self._selected_index()
        if not (0 <= idx < len(self._backups)):
            return
        _dt, bdir = self._backups[idx]
        set_backup_kept(bdir, not is_backup_kept(bdir))
        self._reload_list()
        self._list.setCurrentRow(idx)

    def _on_restore(self):
        idx = self._selected_index()
        if not (0 <= idx < len(self._backups)):
            return
        _dt, backup_dir = self._backups[idx]
        try:
            restore_backup(self._profile_dir, backup_dir)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[backup] restore failed: {exc}")
            return
        self._on_restored()
        self._on_close()
