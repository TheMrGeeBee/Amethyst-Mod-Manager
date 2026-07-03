"""Borderless in-window overlay for the game-launch settings.

Qt port of the game-exe branch of Tk's ExeConfigPanel: a "Launch via"
selector (Auto / Steam / Heroic / None) plus the "Deploy mods before
launching" checkbox. ``on_done(mode, deploy)`` fires with the lowercase mode
string on Save, or ``on_done(None, None)`` on Cancel / Esc.

Follows the ConfirmOverlay pattern (dimmed child backdrop + centered card —
NOT a top-level window; gaming-mode opens those behind the app).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QComboBox, QCheckBox,
)

from gui_qt.theme_qt import active_palette, _c
from gui_qt.wheel_guard import no_wheel

_MODES = ["Auto", "Steam", "Heroic", "None"]


class LauncherSettingsOverlay(QWidget):
    CARD_W = 480
    CARD_H = 260

    def __init__(self, host: QWidget, game_name: str, mode: str, deploy: bool,
                 on_done):
        super().__init__(host)
        self._host = host
        self._on_done = on_done
        self._done = False
        p = active_palette()

        self.setObjectName("OverlayBackdrop")
        self.setStyleSheet("#OverlayBackdrop { background: rgba(0,0,0,150); }")
        self.setGeometry(host.rect())

        self._card = QFrame(self)
        self._card.setObjectName("ConfirmCard")
        self._card.setStyleSheet(
            f"#ConfirmCard {{ background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:8px; }}")
        v = QVBoxLayout(self._card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(8)

        title_lbl = QLabel(self.tr("Launch settings — {0}").format(game_name))
        title_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title_lbl)

        row = QHBoxLayout()
        row.setSpacing(8)
        via_lbl = QLabel(self.tr("Launch via"))
        via_lbl.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        row.addWidget(via_lbl)
        self._mode_combo = QComboBox()
        self._mode_combo.addItems(_MODES)
        cap = (mode or "auto").capitalize()
        self._mode_combo.setCurrentText(cap if cap in _MODES else "Auto")
        no_wheel(self._mode_combo)
        row.addWidget(self._mode_combo)
        row.addStretch(1)
        v.addLayout(row)

        hint = QLabel(self.tr("Auto detects Steam/Heroic ownership. Force a specific "
                      "launcher, or None to always launch the exe directly "
                      "via Proton."))
        hint.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self._deploy_check = QCheckBox(self.tr("Deploy mods before launching"))
        self._deploy_check.setChecked(bool(deploy))
        v.addWidget(self._deploy_check)
        v.addStretch(1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(False))
        bar.addWidget(cancel)
        save = QPushButton(self.tr("Save"))
        save.setObjectName("PrimaryButton")
        save.setCursor(Qt.PointingHandCursor)
        save.clicked.connect(lambda: self._finish(True))
        bar.addWidget(save)
        v.addLayout(bar)

        host.installEventFilter(self)
        self._reposition()
        self.show()
        self.raise_()

    @classmethod
    def show_over(cls, host, *, game_name, mode, deploy, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, game_name, mode, deploy, on_done)

    # -- internals ----------------------------------------------------------
    def _reposition(self):
        self.setGeometry(self._host.rect())
        w = min(self.CARD_W, self._host.width() - 40)
        h = min(self.CARD_H, self._host.height() - 40)
        self._card.setFixedSize(max(340, w), max(200, h))
        self._card.move((self.width() - self._card.width()) // 2,
                        (self.height() - self._card.height()) // 2)

    def _finish(self, saved: bool):
        if self._done:
            return
        self._done = True
        self._host.removeEventFilter(self)
        cb = self._on_done
        mode = self._mode_combo.currentText().lower()
        deploy = self._deploy_check.isChecked()
        self.hide()
        self.deleteLater()
        if cb is not None:
            if saved:
                cb(mode, deploy)
            else:
                cb(None, None)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._finish(False)
        else:
            super().keyPressEvent(event)

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)
