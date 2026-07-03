"""Progress popup + transient notification toasts for the Qt UI.

Mirrors the Tk app's deploy/restore feedback:
  * `ProgressPopup` — a small bottom-right card with a title, phase label, a
    determinate (done/total) or indeterminate (animated) bar. Reused/updated via
    `set_progress(done, total, phase)` and dismissed via `clear()`.
  * `NotificationManager.notify(text, state)` — a stacked toast (info/success/
    warning/error) that auto-dismisses after a few seconds.

Both anchor to a host window and reposition with it. All methods must be called
on the UI thread (deploy/restore workers marshal via Qt signals).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QFrame,
)

from gui_qt.theme_qt import active_palette, _c


def _pal():
    return active_palette()


class ProgressPopup(QFrame):
    """A bottom-right progress card. One instance is reused per host; create via
    the host's NotificationHost (or directly) and drive with set_progress()."""

    WIDTH = 420

    def __init__(self, host: QWidget):
        super().__init__(host)
        self._host = host
        self.setObjectName("ProgressPopup")
        self.setFixedWidth(self.WIDTH)
        self.setAttribute(Qt.WA_StyledBackground, True)

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(10)

        self._title = QLabel(self.tr("Deploying"))
        self._title.setStyleSheet("font-size:18px; font-weight:600;")
        v.addWidget(self._title)

        self._phase = QLabel(self.tr("Working…"))
        self._phase.setStyleSheet(f"color:{_c(_pal(),'TEXT_DIM')}; font-size:14px;")
        self._phase.setWordWrap(True)
        v.addWidget(self._phase)

        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(12)
        v.addWidget(self._bar)

        self._count = QLabel("")
        self._count.setStyleSheet(f"color:{_c(_pal(),'TEXT_DIM')}; font-size:13px;")
        self._count.setAlignment(Qt.AlignRight)
        v.addWidget(self._count)

        self.hide()
        host.installEventFilter(self)

    def set_progress(self, done: int, total: int, phase: str | None = None,
                     title: str | None = None):
        if title:
            self._title.setText(title)
        if phase is not None:
            self._phase.setText(phase or "Working…")
        if total > 0:
            self._bar.setRange(0, total)
            self._bar.setValue(min(done, total))
            self._count.setText(self.tr("{0} / {1}").format(done, total))
        else:
            # Indeterminate (busy) — Qt animates a range of 0,0.
            self._bar.setRange(0, 0)
            self._count.setText("")
        if not self.isVisible():
            self.show()
        self._reposition()
        self.raise_()

    def clear(self):
        self.hide()

    def _reposition(self):
        self.adjustSize()
        self.setFixedWidth(self.WIDTH)
        m = 16
        x = self._host.width() - self.width() - m
        y = self._host.height() - self.height() - m
        self.move(max(0, x), max(0, y))

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self._host and event.type() in (QEvent.Resize, QEvent.Move) \
                and self.isVisible():
            self._reposition()
        return super().eventFilter(obj, event)


class _Toast(QFrame):
    """A single auto-dismissing notification card."""

    def __init__(self, manager: "NotificationManager", text: str, state: str):
        super().__init__(manager._host)
        self._manager = manager
        self.setObjectName("Toast")
        self.setProperty("state", state)      # info/success/warning/error
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumWidth(340)
        self.setMaximumWidth(460)

        h = QHBoxLayout(self)
        h.setContentsMargins(18, 14, 18, 14)
        h.setSpacing(12)
        dot = QLabel("●")
        dot.setObjectName("ToastDot")
        dot.setProperty("state", state)
        dot.setStyleSheet("font-size:16px;")
        h.addWidget(dot)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("font-size:14px;")
        h.addWidget(lbl, 1)

        self.adjustSize()
        # Auto-dismiss (errors/warnings linger a little longer).
        ms = 5000 if state in ("warning", "error") else 3200
        QTimer.singleShot(ms, self._dismiss)

    def _dismiss(self):
        self._manager._remove(self)


class NotificationManager:
    """Stacks transient toasts in the host's top-right corner."""

    def __init__(self, host: QWidget):
        self._host = host
        self._toasts: list[_Toast] = []
        host.installEventFilter(self._Filter(self))

    class _Filter(QWidget):
        def __init__(self, mgr):
            super().__init__()
            self._mgr = mgr

        def eventFilter(self, obj, event):
            from PySide6.QtCore import QEvent
            if event.type() in (QEvent.Resize, QEvent.Move):
                self._mgr._restack()
            return False

    def notify(self, text: str, state: str = "info"):
        t = _Toast(self, text, state)
        self._toasts.append(t)
        t.show()
        self._restack()

    def _remove(self, toast: _Toast):
        if toast in self._toasts:
            self._toasts.remove(toast)
            toast.deleteLater()
            self._restack()

    def _restack(self):
        m = 16
        y = m
        for t in self._toasts:
            t.adjustSize()
            x = self._host.width() - t.width() - m
            t.move(max(0, x), y)
            t.raise_()
            y += t.height() + 8
