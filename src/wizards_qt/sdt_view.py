"""SSE Display Tweaks config editor — Qt port of wizards/sse_display_tweaks.py.

A modlist-panel-scoped tab: a scrollable grid form of every SSEDisplayTweaks.ini
setting with a per-key enable checkbox, a typed value control (bool → two
radios, enum → combo, else line edit) and a dim description.  Save renders the
values into the managed 'SSE Display Tweaks ini' mod (schema/parse/render in
Utils/sdt_config.py); Reset restores the built-in defaults.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QRadioButton, QScrollArea, QVBoxLayout, QWidget,
)

from gui_qt.theme_qt import active_palette, _c
from wizards_qt._view_base import GREEN, RED, WizardViewBase
import Utils.sdt_config as cfg

if TYPE_CHECKING:
    from Games.base_game import BaseGame


class SDTView(WizardViewBase):
    """Edit the SSE Display Tweaks ini as a managed mod."""

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=f"SSE Display Tweaks — {game.name}")
        # (section,key) -> (enable_chk, getter()->str, setter(str))
        self._rows: dict = {}
        self._stack.addWidget(self._build_form())

    def _build_form(self) -> QWidget:
        p = active_palette()
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(8)

        values, source = cfg.load_initial_values(self._game)

        head = QLabel(self.tr("Editing values from {0}. Save writes the managed mod '{1}'.").format(source, cfg.MOD_NAME))
        head.setWordWrap(True)
        head.setStyleSheet(self._dim)
        outer.addWidget(head)

        self._status = QLabel("")
        self._status.setStyleSheet(self._dim)
        outer.addWidget(self._status)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setColumnStretch(2, 1)
        grid.setContentsMargins(4, 4, 4, 4)
        grid.setVerticalSpacing(2)

        row = 0
        last_section = None
        for s in cfg.SCHEMA:
            if s.section != last_section:
                sec = QLabel(self.tr("[{0}]").format(s.section))
                sec.setStyleSheet("color:#2d8fd0; font-weight:700;")
                grid.addWidget(sec, row, 0, 1, 3)
                row += 1
                last_section = s.section

            value, enabled = values.get(s.id, (s.default, s.enabled_by_default))

            chk = QCheckBox()
            chk.setChecked(enabled)
            grid.addWidget(chk, row, 0)

            key_lbl = QLabel(s.key)
            key_lbl.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')};")
            grid.addWidget(key_lbl, row, 1)

            widget, getter, setter = self._value_control(s)
            setter(value)
            grid.addWidget(widget, row, 2)
            self._rows[s.id] = (chk, getter, setter)
            row += 1

            desc = QLabel(s.desc)
            desc.setWordWrap(True)
            desc.setStyleSheet(self._dim)
            grid.addWidget(desc, row, 1, 1, 2)
            row += 1

        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        bar = QWidget()
        bh = QHBoxLayout(bar); bh.setContentsMargins(0, 4, 0, 0); bh.setSpacing(8)
        bh.addStretch(1)
        close = QPushButton(self.tr("Close"))
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self._finish)
        bh.addWidget(close)
        reset = QPushButton(self.tr("Reset to defaults"))
        reset.setCursor(Qt.PointingHandCursor)
        reset.clicked.connect(self._on_reset)
        bh.addWidget(reset)
        save = self._accent_btn(self.tr("Save"))
        save.clicked.connect(self._on_save)
        bh.addWidget(save)
        outer.addWidget(bar)
        return page

    def _value_control(self, s):
        """Return (widget, getter()->str, setter(str)) for a schema setting."""
        if s.kind == "bool":
            w = QWidget()
            h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0)
            grp = QButtonGroup(w)
            rb_t = QRadioButton("true"); rb_f = QRadioButton("false")
            grp.addButton(rb_t); grp.addButton(rb_f)
            h.addWidget(rb_t); h.addWidget(rb_f); h.addStretch(1)
            return (w,
                    lambda: "true" if rb_t.isChecked() else "false",
                    lambda v: (rb_t if str(v).lower() == "true"
                               else rb_f).setChecked(True))
        if s.kind == "enum":
            cb = QComboBox()
            cb.addItems(s.choices or [])
            return (cb, lambda: cb.currentText(),
                    lambda v: cb.setCurrentText(str(v)))
        le = QLineEdit()
        le.setMaximumWidth(180)
        return (le, lambda: le.text(), lambda v: le.setText(str(v)))

    def _collect_values(self):
        return {ident: (getter(), chk.isChecked())
                for ident, (chk, getter, _setter) in self._rows.items()}

    def _on_reset(self):
        defaults = cfg.schema_defaults()
        for ident, (chk, _getter, setter) in self._rows.items():
            value, enabled = defaults.get(ident, ("", True))
            chk.setChecked(enabled)
            setter(value)
        self._status.setStyleSheet(self._dim)
        self._status.setText(self.tr("Form reset to built-in defaults (not yet saved)."))

    def _on_save(self):
        values = self._collect_values()
        try:
            target = cfg.save_config(self._game, values)
        except OSError as exc:
            self._status.setStyleSheet(f"color:{RED};")
            self._status.setText(self.tr("Save failed: {0}").format(exc))
            self._log(f"SSE Display Tweaks wizard: save failed: {exc}")
            return
        self._log(f"SSE Display Tweaks wizard: wrote {target}")
        self._status.setStyleSheet(f"color:{GREEN};")
        self._status.setText(self.tr("Saved to {0}/{1}.").format(cfg.MOD_NAME, cfg.REL_INI_PATH))
        self._ran = True
        if getattr(self._ctx, "refresh_modlist", None):
            self._ctx.refresh_modlist()
