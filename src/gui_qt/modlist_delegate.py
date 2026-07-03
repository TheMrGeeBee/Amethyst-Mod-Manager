"""Modlist delegate — paints rows for the QTreeView.

Graduates the spike's painting onto the multi-column model:
  - separator rows: full-width band + bold label
  - Name column: conflict strip, checkbox, lock glyph, elided name
  - other columns: plain text via the base delegate

Colours come from the active palette so themes carry over.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QRect, QSize, QEvent
from PySide6.QtGui import QColor, QFont, QPen, QBrush
from PySide6.QtWidgets import QStyledItemDelegate, QStyle, QToolTip

from gui_qt.theme_qt import active_palette, _c
from gui_qt.icons import icon
from gui_qt.modlist_model import (
    EntryRole, ConflictRole, BsaConflictRole, FlagsRole, HighlightRole,
    COL_NAME, COL_FLAGS, COL_CONFLICTS,
)
from gui_qt.modlist_data import (
    FLAG_UPDATE, FLAG_ENDORSED, FLAG_ROOT, FLAG_MODIFIED_MF, FLAG_MISSING_REQS,
    FLAG_COLLECTION_BUNDLED, FLAG_COLLECTION_PATCHED, FLAG_NOTE, FLAG_XEDIT,
    FLAG_BUNDLE, FLAG_MODIO_UPDATE, FLAG_PRERTX, FLAG_ROOT_RULE,
)

# Flag bit → icon filename, painted left-to-right in the Flags column, in the
# SAME order as the Tk modlist (gui/modlist_panel.py ~2878): note, bundle,
# missing-reqs, update, modio-update, endorsed, info (pre-RTX OR collection —
# mutually exclusive, handled in _flag_icons), modified-MF, xEdit, root.
_FLAG_ICONS = [
    (FLAG_NOTE, "note.png"),
    (FLAG_BUNDLE, "settings.png"),
    (FLAG_MISSING_REQS, "warning.png"),
    (FLAG_UPDATE, "update.png"),
    (FLAG_MODIO_UPDATE, "update_modio.png"),
    (FLAG_ENDORSED, "endorsed.png"),
    # info.png: pre-RTX OR collection bundled/patched — only ONE ever paints (see
    # _flag_icons). The hover tooltip distinguishes which.
    (FLAG_PRERTX, "info.png"),
    (FLAG_COLLECTION_BUNDLED, "info.png"),
    (FLAG_COLLECTION_PATCHED, "info.png"),
    (FLAG_MODIFIED_MF, "eye2_white.png"),
    (FLAG_XEDIT, "brush.png"),
    # root.png: meta root_folder OR a custom root-routing rule (same icon).
    (FLAG_ROOT, "root.png"),
    (FLAG_ROOT_RULE, "root.png"),
]

# The info-icon flags, in precedence order (Tk: pre-RTX wins, else collection).
_INFO_FLAGS = (FLAG_PRERTX, FLAG_COLLECTION_BUNDLED, FLAG_COLLECTION_PATCHED)
# The root-icon flags — only one root.png ever paints.
_ROOT_FLAGS = (FLAG_ROOT, FLAG_ROOT_RULE)

# Flag bit → hover tooltip text (verbatim from the Tk modlist, ~5114). The two
# root sources have DISTINCT text (meta root_folder vs a custom routing rule);
# the note/xedit/missing tips are per-mod dynamic (see _flag_tip in the delegate).
_FLAG_TIPS = {
    FLAG_NOTE: "Note",
    FLAG_BUNDLE: "Click here to open bundle settings",
    FLAG_MISSING_REQS: "Missing requirements",
    FLAG_UPDATE: "Update available on Nexus Mods",
    FLAG_MODIO_UPDATE: "Update available on mod.io",
    FLAG_ENDORSED: "Endorsed",
    FLAG_PRERTX: "Pre-RTX mod",
    FLAG_COLLECTION_BUNDLED: "This mod is a collection bundled mod",
    FLAG_COLLECTION_PATCHED: "This mod has diff patches applied by the collection install",
    FLAG_MODIFIED_MF: "Modified in Mod Files tab",
    FLAG_XEDIT: "Contains a plugin modified in xEdit",
    FLAG_ROOT: "This mod is sent to the root folder",
    FLAG_ROOT_RULE: "This mod contains files that route to the game root",
}

# Conflict code → icon (lightning), painted in the Conflicts column (Tk parity).
_CONFLICT_ICONS = {
    1: "conflict-winner.png",
    -1: "conflict-loser.png",
    2: "conflict-mixed.png",
    3: "conflict-redundant.png",   # FULL — fully overridden / redundant
}

# BSA/BA2 archive conflict gets its own icon set (drawn right of the loose one).
_BSA_CONFLICT_ICONS = {
    1: "archive-conflict-winner.png",
    -1: "archive-conflict-loser.png",
    2: "archive-conflict-mixed.png",
}

def _contrasting_text_color(hex_bg: str) -> str:
    """'#111111' or '#eeeeee' based on the luminance of *hex_bg* so separator
    text stays readable on a custom colour. Inlined from gui.theme (which pulls
    in customtkinter/tkinter — unavailable in the Qt app)."""
    try:
        hex_bg = hex_bg.lstrip("#")
        r, g, b = (int(hex_bg[i:i + 2], 16) / 255.0 for i in (0, 2, 4))

        def _lin(c: float) -> float:
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        lum = 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)
        return "#111111" if lum > 0.179 else "#eeeeee"
    except Exception:
        return "#eeeeee"


# Row metrics — ~10% larger than the Tk baseline (30px) for readability.
ROW_H = 33
SEP_H = 33
CHECK_BOX = 17
ICON_SZ = 20        # flag / conflict / arrow / lock icon size
FONT_PX = 14        # row text size


class ModRowDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        p = active_palette()
        self.c_sep_bg = QColor(_c(p, "BG_SEP"))
        self.c_sep_text = QColor(_c(p, "TEXT_SEP"))
        self.c_row = QColor(_c(p, "BG_ROW"))
        self.c_row_alt = QColor(_c(p, "BG_ROW_ALT"))
        self.c_sel = QColor(_c(p, "BG_SELECT"))
        self.c_hover = QColor(_c(p, "BG_ROW_HOVER"))
        self.c_text = QColor(_c(p, "TEXT_MAIN"))
        self.c_text_dim = QColor(_c(p, "TEXT_DIM"))
        self.c_text_on_sel = QColor(_c(p, "TEXT_ON_ACCENT"))
        self.c_border = QColor(_c(p, "BORDER"))
        self.c_lock = QColor(_c(p, "TEXT_WARN"))
        self.c_win = QColor(_c(p, "TEXT_OK_BRIGHT"))
        self.c_lose = QColor(_c(p, "TEXT_ERR_BRIGHT"))
        self.c_check = QColor(_c(p, "ACCENT"))   # checkbox fill when enabled (blue)
        self.c_check_off = QColor(_c(p, "BG_DEEP"))   # checkbox fill when disabled
        self.c_overwrite_bg = QColor(_c(p, "BG_DARK_GREEN"))  # Overwrite band
        self.c_root_bg = QColor(_c(p, "BG_DARK_BLUE"))        # Root Folder band
        # Cross-panel highlight row tints (exact Tk conflict colours).
        self.c_hl_higher = QColor("#108d00")   # selection beats this mod (green)
        self.c_hl_lower = QColor("#9a0e0e")    # this mod beats selection (red)
        self.c_hl_anchor = QColor("#A45500")   # plugin-selected mod (orange)
        self.c_root_text = QColor(_c(p, "TONE_BLUE_SOFT"))
        self.c_overwrite_text = QColor(_c(p, "TEXT_OK_BRIGHT"))
        # Shared row/label fonts — paint() runs per visible cell, so build
        # these once instead of allocating a QFont per call.
        self.f_row = QFont()
        self.f_row.setPixelSize(FONT_PX)
        self.f_bold = QFont()
        self.f_bold.setBold(True)
        self.f_bold.setPixelSize(FONT_PX)

    def sizeHint(self, opt, index):
        e = index.data(EntryRole)
        h = SEP_H if (e and e.is_separator) else ROW_H
        return QSize(opt.rect.width(), h)

    def paint(self, p, opt, index):
        e = index.data(EntryRole)
        if e is None:
            super().paint(p, opt, index)
            return
        r = opt.rect
        p.save()
        p.setRenderHint(p.RenderHint.Antialiasing, False)

        # Separator: paint a full band only on the name column; blank elsewhere
        # so the band reads as one strip across the row. A collapsed separator
        # whose child mod is a conflict partner is tinted green/red/orange.
        if e.is_separator:
            from gui_qt.modlist_model import OVERWRITE_NAME, ROOT_FOLDER_NAME
            from gui_qt.modlist_sort import DIVIDER_NAME
            if e.name == DIVIDER_NAME:
                # Reverse-priority float divider: a thin dashed centred line on
                # a plain row background, no controls (Tk BOUNDARY_NAME row).
                p.fillRect(r, self.c_row)
                pen = QPen(self.c_border, 1)
                pen.setStyle(Qt.PenStyle.DashLine)
                p.setPen(pen)
                cy = r.center().y()
                p.drawLine(r.left() + 8, cy, r.right() - 8, cy)
                p.restore()
                return
            sep_hl = index.data(HighlightRole) or 0
            selected = bool(opt.state & QStyle.State_Selected)
            # A custom colour applies only to plain separators, and only when no
            # selection / cross-panel highlight band overrides it (Tk parity).
            custom = index.model().sep_color(e.name)
            sep_text = None
            if selected:
                p.fillRect(r, self.c_sel)
            elif sep_hl == 2:
                p.fillRect(r, self.c_hl_anchor)
            elif sep_hl == 1:
                p.fillRect(r, self.c_hl_higher)
            elif sep_hl == -1:
                p.fillRect(r, self.c_hl_lower)
            elif e.name == OVERWRITE_NAME:
                p.fillRect(r, self.c_overwrite_bg)
            elif e.name == ROOT_FOLDER_NAME:
                p.fillRect(r, self.c_root_bg)
            elif custom:
                p.fillRect(r, QColor(custom))
                sep_text = QColor(_contrasting_text_color(custom))
            else:
                p.fillRect(r, self.c_sep_bg)
            if index.column() == COL_NAME:
                self._paint_separator(p, r, e, index, sep_text)
            p.restore()
            return

        # Row background. Selection wins, then cross-panel highlight tint
        # (green/red/orange), then hover, then the zebra base.
        selected = bool(opt.state & QStyle.State_Selected)
        hl = index.data(HighlightRole) or 0
        highlighted = False
        if selected:
            p.fillRect(r, self.c_sel)
        elif hl == 2:
            p.fillRect(r, self.c_hl_anchor); highlighted = True
        elif hl == 1:
            p.fillRect(r, self.c_hl_higher); highlighted = True
        elif hl == -1:
            p.fillRect(r, self.c_hl_lower); highlighted = True
        elif opt.state & QStyle.State_MouseOver:
            p.fillRect(r, self.c_hover)
        else:
            p.fillRect(r, self.c_row_alt if index.row() % 2 else self.c_row)

        text_color = (self.c_text_on_sel if (selected or highlighted)
                      else (self.c_text if e.enabled else self.c_text_dim))

        if index.column() == COL_NAME:
            self._paint_name(p, r, e, index, text_color)
        elif index.column() == COL_FLAGS:
            self._paint_icons(p, r, self._flag_icons(index.data(FlagsRole) or 0))
        elif index.column() == COL_CONFLICTS:
            self._paint_conflicts(p, r, index.data(ConflictRole) or 0,
                                  index.data(BsaConflictRole) or 0)
        else:
            # Plain columns (Installed/Version/Priority): centred to match the
            # centred headers + the icon columns.
            val = index.data(Qt.DisplayRole) or ""
            p.setPen(text_color)
            p.setFont(self.f_row)
            pad = QRect(r.left() + 6, r.top(), r.width() - 12, r.height())
            p.drawText(pad, Qt.AlignVCenter | Qt.AlignHCenter, str(val))

        p.restore()

    # Geometry shared by paint + editorEvent so the hit areas match exactly.
    ARROW_SZ = ICON_SZ
    LOCK_SZ = ICON_SZ
    PAD = 10

    def _arrow_rect(self, r):
        y = r.top() + (r.height() - self.ARROW_SZ) // 2
        return QRect(r.left() + self.PAD, y, self.ARROW_SZ, self.ARROW_SZ)

    def _lock_rect(self, r):
        y = r.top() + (r.height() - self.LOCK_SZ) // 2
        return QRect(r.right() - self.PAD - self.LOCK_SZ, y,
                     self.LOCK_SZ, self.LOCK_SZ)

    def _col_rect(self, col, r):
        """Sub-rect of a (full-width, spanned) separator row aligned with a
        given column, so separator content sits under the right header."""
        view = self.parent()
        try:
            x = view.columnViewportPosition(col)
            w = view.columnWidth(col)
            return QRect(x, r.top(), w, r.height())
        except Exception:
            return r

    def _paint_separator(self, p, r, e, index, text_color=None):
        model = index.model()
        text_color = text_color or self.c_sep_text
        # Boundary separators (Overwrite / Root Folder) are pinned + not
        # collapsible/lockable: just a centred name + strikethrough, no controls.
        from gui_qt.modlist_model import (_BOUNDARY_NAMES, ROOT_FOLDER_NAME,
                                          OVERWRITE_NAME)
        if e.name in _BOUNDARY_NAMES:
            p.setFont(self.f_bold)
            cy = r.center().y()
            nr = self._col_rect(COL_NAME, r)
            tw = p.fontMetrics().horizontalAdvance(e.display_name)
            cx = nr.center().x(); gap = tw // 2 + 12
            p.setPen(QPen(self.c_border, 1))
            p.drawLine(r.left() + 6, cy, cx - gap, cy)
            p.drawLine(cx + gap, cy, r.right() - 6, cy)
            txt = (self.c_overwrite_text if e.name == OVERWRITE_NAME
                   else self.c_root_text if e.name == ROOT_FOLDER_NAME
                   else self.c_sep_text)
            p.setPen(txt)
            p.drawText(nr, Qt.AlignVCenter | Qt.AlignHCenter, e.display_name)
            return

        name = e.display_name
        collapsed = model.is_collapsed(name)
        locked = model.is_sep_locked(name)
        block = model.sep_block_rows(index.row())

        name_rect = self._col_rect(COL_NAME, r)
        p.setFont(self.f_bold)
        label = f"{name}   ({len(block)})"
        tw = p.fontMetrics().horizontalAdvance(label)
        cx = name_rect.center().x()
        cy = r.center().y()

        # Strikethrough line across the row, broken around the centred name
        # (Tk-style — makes separators easy to distinguish).
        p.setPen(QPen(self.c_border, 1))
        gap = tw // 2 + 12
        p.drawLine(r.left() + 6, cy, cx - gap, cy)
        p.drawLine(cx + gap, cy, r.right() - 6, cy)

        # Collapse arrow — right.png when collapsed, arrow.png when expanded.
        a = self._arrow_rect(r)
        ico = icon("right.png" if collapsed else "arrow.png", self.ARROW_SZ)
        if not ico.isNull():
            ico.paint(p, a)

        # Centred name + "(N)" count over the Mod Name column.
        p.setPen(text_color)
        p.drawText(name_rect, Qt.AlignVCenter | Qt.AlignHCenter, label)

        # Grouped flags/conflicts when collapsed — each under its own column.
        if collapsed:
            self._paint_grouped_icons(p, r, model, block)

        # Lock checkbox on the far right — always drawn so it reads as a
        # clickable control. Empty box when unlocked; the (gold) lock.png on a
        # neutral fill when locked (lock.png is gold, so the fill stays neutral).
        lk = self._lock_rect(r)
        p.setPen(QPen(self.c_border, 1))
        p.setBrush(QBrush(self.c_check_off))
        p.drawRoundedRect(lk, 3, 3)
        if locked:
            lico = icon("lock.png", self.LOCK_SZ - 2)
            if not lico.isNull():
                lico.paint(p, lk.adjusted(1, 1, -1, -1))

    def _paint_grouped_icons(self, p, r, model, block):
        """Collapsed-separator summary: union of the block's flag icons painted
        in the Flags column, and its conflict icons in the Conflicts column —
        each kept under the relevant header."""
        bits, conflicts, bsa_conflicts = model.sep_block_summary(block)

        def _summarise(codes, icons):
            # Both winners + losers (or any mixed) → one mixed icon, else lone.
            # A fully-redundant (3) member is shown with its own icon if present,
            # else folds into the "loses" bucket.
            if 2 in codes or (1 in codes and -1 in codes):
                return [icons[2]]
            if 3 in codes and 3 in icons:
                return [icons[3]]
            if 1 in codes:
                return [icons[1]]
            if -1 in codes or 3 in codes:
                return [icons[-1]]
            return []

        conflict_icons = (_summarise(conflicts, _CONFLICT_ICONS)
                          + _summarise(bsa_conflicts, _BSA_CONFLICT_ICONS))
        self._paint_icons(p, self._col_rect(COL_FLAGS, r), self._flag_icons(bits))
        self._paint_icons(p, self._col_rect(COL_CONFLICTS, r), conflict_icons)

    def _paint_name(self, p, r, e, index, text_color):
        x = r.left()

        # Checkbox (accent fill + white tick when enabled; hollow when not).
        box = QRect(x + 10, r.top() + (r.height() - CHECK_BOX) // 2,
                    CHECK_BOX, CHECK_BOX)
        p.setRenderHint(p.RenderHint.Antialiasing, True)
        p.setPen(QPen(self.c_border, 1))
        p.setBrush(QBrush(self.c_check if e.enabled else self.c_check_off))
        p.drawRoundedRect(box, 3, 3)
        if e.enabled:
            p.setPen(QPen(QColor("white"), 2))
            p.drawLine(box.left() + 4, box.center().y() + 1,
                       box.center().x() - 1, box.bottom() - 4)
            p.drawLine(box.center().x() - 1, box.bottom() - 4,
                       box.right() - 3, box.top() + 4)
        p.setRenderHint(p.RenderHint.Antialiasing, False)

        tx = box.right() + 10

        # Lock glyph.
        if e.locked:
            p.setPen(self.c_lock)
            p.drawText(QRect(tx, r.top(), 16, r.height()),
                       Qt.AlignVCenter, "\U0001F512")
            tx += 18

        # Name (elided).
        p.setPen(text_color)
        p.setFont(self.f_row)
        name_rect = QRect(tx, r.top(), r.right() - tx - 6, r.height())
        elided = opt_fm(p).elidedText(e.display_name, Qt.ElideRight,
                                      name_rect.width())
        p.drawText(name_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)

    def _paint_conflicts(self, p, r, loose, bsa):
        """Conflicts cell: loose-file conflict icon on the left, BSA/BA2 archive
        conflict icon on the right (Tk parity). Either may be absent; a lone icon
        is centred, both pair up around the cell centre."""
        loose_ico = _CONFLICT_ICONS.get(loose)
        bsa_ico = _BSA_CONFLICT_ICONS.get(bsa)
        names = [n for n in (loose_ico, bsa_ico) if n]
        if names:
            self._paint_icons(p, r, names)

    @staticmethod
    def _effective_flag_bits(bits):
        """Collapse the mutually-exclusive icon groups: only ONE info.png (pre-RTX
        wins over collection bundled/patched) and only ONE root.png ever paint —
        matching Tk. Returns the active FLAG_ICONS entries after the collapse."""
        # Info group: keep the first present in precedence order, drop the rest.
        info_keep = next((f for f in _INFO_FLAGS if bits & f), 0)
        root_keep = next((f for f in _ROOT_FLAGS if bits & f), 0)
        out = []
        for bit, name in _FLAG_ICONS:
            if bit in _INFO_FLAGS and bit != info_keep:
                continue
            if bit in _ROOT_FLAGS and bit != root_keep:
                continue
            if bits & bit:
                out.append((bit, name))
        return out

    def _flag_icons(self, bits):
        return [name for _bit, name in self._effective_flag_bits(bits)]

    def _hit_flag_bit(self, pos, r, bits):
        """Which FLAG_* bit's icon (if any) is under *pos* within the Flags cell
        rect *r*. Recomputes the same left-to-right centred geometry as
        _paint_icons so a click lands on the icon the user sees."""
        active = self._effective_flag_bits(bits)
        if not active:
            return 0
        sz, gap = ICON_SZ, 3
        total = len(active) * sz + (len(active) - 1) * gap
        x = r.left() + max(6, (r.width() - total) // 2)
        y = r.top() + (r.height() - sz) // 2
        # Walk the collapsed ACTIVE bits in paint order so the hit maps to the
        # correct flag even when several share an icon (info / root).
        for bit, _name in active:
            if QRect(x, y, sz, sz).contains(pos):
                return bit
            x += sz + gap
        return 0

    def _flag_tip(self, hit, index):
        """Tooltip for the hovered flag *hit*. The Note flag shows the actual
        note text (Tk parity); everything else uses the static _FLAG_TIPS."""
        if hit == FLAG_NOTE:
            try:
                model = index.model()
                name = index.data(EntryRole).name
                note = model.note_for(name) if hasattr(model, "note_for") else ""
                if note:
                    return note if len(note) <= 500 else note[:500] + "…"
            except Exception:
                pass
        return _FLAG_TIPS.get(hit)

    def helpEvent(self, event, view, opt, index):
        """Show the per-flag tooltip when hovering a flag icon (Tk parity —
        distinguishes e.g. collection bundled vs patched)."""
        try:
            if (event.type() == QEvent.ToolTip
                    and index.isValid() and index.column() == COL_FLAGS):
                bits = index.data(FlagsRole) or 0
                if bits:
                    hit = self._hit_flag_bit(event.pos(), opt.rect, bits)
                    tip = self._flag_tip(hit, index)
                    if tip:
                        # Pass the flags-cell rect so Qt hides the tooltip as soon
                        # as the cursor leaves the cell (instead of keeping it up
                        # for its full length-based timeout).
                        QToolTip.showText(event.globalPos(), tip, view, opt.rect)
                        return True
                QToolTip.hideText()
        except Exception:
            pass
        return super().helpEvent(event, view, opt, index)

    def _paint_icons(self, p, r, names):
        """Paint a horizontally-centred row of icons (Flags + Conflicts cells,
        and the collapsed-separator summary) so they line up under the column."""
        if not names:
            return
        sz, gap = ICON_SZ, 3
        total = len(names) * sz + (len(names) - 1) * gap
        x = r.left() + max(6, (r.width() - total) // 2)
        y = r.top() + (r.height() - sz) // 2
        for name in names:
            ic = icon(name, sz)
            if not ic.isNull():
                ic.paint(p, QRect(x, y, sz, sz))
            x += sz + gap
            if x > r.right() - sz:
                break

    def editorEvent(self, event, model, opt, index):
        if event.type() != QEvent.MouseButtonRelease:
            return False
        if index.column() not in (COL_NAME, COL_FLAGS):
            return False
        pos = event.position().toPoint()
        e = model.entry(index.row())

        # Flags cell: a click on a flag icon may trigger an action (the update
        # flag opens Change Version). Other flags are inert for now.
        if index.column() == COL_FLAGS:
            if e.is_separator:
                return False
            bits = model.data(index, FlagsRole) or 0
            hit = self._hit_flag_bit(pos, opt.rect, bits)
            if hit:
                view = self.parent()
                cb = getattr(view, "on_flag_clicked", None)
                if cb is not None:
                    cb(index.row(), hit)
                    return True
            return False

        if e.is_separator:
            # Real separators are never selectable: the view consumes their
            # press (mousePressEvent) and does the collapse/lock toggle on
            # release (_handle_separator_click), so it never reaches here. The
            # _arrow_rect/_lock_rect geometry still lives on this delegate and
            # is reused by that handler.
            return False

        # Mod row: checkbox area toggles enabled.
        box = QRect(opt.rect.left() + 6, opt.rect.top(), 26, opt.rect.height())
        if box.contains(pos):
            model.toggle(index.row())
            return True
        return False


def opt_fm(painter):
    """Font metrics from the painter's current font (for eliding)."""
    return painter.fontMetrics()
