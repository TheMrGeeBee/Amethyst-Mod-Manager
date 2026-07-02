"""Qt dialog to manage download scan locations — toggle the default Downloads
folder, toggle the per-game cache, and add/remove extra folders. Reads/writes
the same Utils.download_locations settings as the Tk app (backward compatible).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QListWidget,
    QPushButton, QFrame,
)

import Utils.download_locations as dl


class DownloadLocationsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Download locations")
        self.setMinimumWidth(520)
        self._build()
        self._load()

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)

        v.addWidget(QLabel(
            "Folders scanned for mod archives in the Downloads tab."))

        self._default_cb = QCheckBox()
        v.addWidget(self._default_cb)
        self._cache_cb = QCheckBox("Scan this game's download cache")
        v.addWidget(self._cache_cb)

        line = QFrame(); line.setFrameShape(QFrame.HLine); v.addWidget(line)
        v.addWidget(QLabel("Additional folders:"))
        self._list = QListWidget()
        v.addWidget(self._list, 1)

        row = QHBoxLayout()
        add = QPushButton("Add folder…")
        add.clicked.connect(self._add)
        rem = QPushButton("Remove selected")
        rem.clicked.connect(self._remove)
        row.addWidget(add); row.addWidget(rem); row.addStretch(1)
        v.addLayout(row)

        line2 = QFrame(); line2.setFrameShape(QFrame.HLine); v.addWidget(line2)
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        save = QPushButton("Save"); save.setObjectName("PrimaryButton")
        save.clicked.connect(self._save)
        btns.addWidget(cancel); btns.addWidget(save)
        v.addLayout(btns)

    def _load(self):
        default = dl.get_default_downloads_dir()
        self._default_cb.setText(f"Scan default Downloads folder ({default})")
        self._default_cb.setChecked(not dl.is_default_downloads_disabled())
        self._cache_cb.setChecked(not dl.is_cache_default_disabled())
        self._list.clear()
        for p in dl.load_extra_download_locations():
            self._list.addItem(p)

    def _add(self):
        from Utils.portal_filechooser import pick_folder

        def _chosen(path):
            if not path:
                return
            folder = str(path)
            existing = {self._list.item(i).text()
                        for i in range(self._list.count())}
            if folder not in existing:
                self._list.addItem(folder)

        pick_folder("Add download folder", _chosen)

    def _remove(self):
        for it in self._list.selectedItems():
            self._list.takeItem(self._list.row(it))

    def _save(self):
        extras = [self._list.item(i).text() for i in range(self._list.count())]
        dl.write_config(
            extras,
            not self._default_cb.isChecked(),
            not self._cache_cb.isChecked())
        self.accept()
