"""Qt theming — builds a QSS stylesheet from the existing theme palettes.

The palette data in ``gui/themes/*.py`` is plain ``{KEY: "#hex"}`` dicts
(toolkit-neutral), so the Qt app reuses it directly rather than duplicating
colours. Per-theme overrides flow through the same ``THEME_DEFAULTS_OVERRIDE``
mechanism the Tk app uses.
"""

from __future__ import annotations

from pathlib import Path

from gui.themes import load_palettes
from Utils.ui_config import get_appearance_mode


# Fallback used if a palette is missing a key, so QSS never renders with an
# empty colour string.
_FALLBACK = "#1a1a1a"

_ICONS_DIR = Path(__file__).resolve().parent.parent / "icons"


def _icon_url(name: str) -> str:
    """Forward-slash absolute path to icons/<name> for QSS `image: url(...)`
    (QSS wants POSIX separators even on the path form)."""
    return _ICONS_DIR.joinpath(name).as_posix()


# The Qt app defaults to the original near-black "dark" palette. (Breeze Dark
# stays available as a selectable theme — appearance_mode = "breeze".) The blue
# checkboxes/selection come from this palette's ACCENT (#0078d4).
_QT_DEFAULT_THEME = "dark"


def active_palette() -> dict:
    """Return the {KEY: hex} palette for the Qt app. Defaults to the dark palette;
    an explicit saved appearance_mode theme wins when present. (Values may be str
    or (light,dark) tuples; _c() normalises them.)"""
    palettes = load_palettes()
    mode = get_appearance_mode()
    if mode and mode in palettes:
        return palettes[mode]
    return (palettes.get(_QT_DEFAULT_THEME)
            or palettes.get("dark")
            or next(iter(palettes.values()), {}))


def _c(pal: dict, key: str) -> str:
    val = pal.get(key, _FALLBACK)
    # Palette values may be (light, dark) tuples in some themes; take a string.
    if isinstance(val, (tuple, list)):
        val = val[-1]
    return str(val)


def build_qss(pal: dict | None = None) -> str:
    """Build the application QSS from a palette (default: active palette)."""
    p = pal or active_palette()
    c = lambda k: _c(p, k)
    return f"""
    QWidget {{
        color: {c('TEXT_MAIN')};
        font-size: 13px;
    }}
    QMainWindow, QDialog {{ background: {c('BG_DEEP')}; }}
    /* Transparent by default so labels/checkboxes don't paint near-black boxes
       over their container — containers set their own background explicitly. */
    QLabel, QCheckBox, QRadioButton {{ background: transparent; }}
    QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; }}

    /* Toggle indicators — blue when checked, consistent size everywhere. */
    QCheckBox::indicator {{
        width: 16px; height: 16px;
        border: 1px solid {c('BORDER_FAINT')};
        border-radius: 3px;
        background: {c('BG_DEEP')};
    }}
    QCheckBox::indicator:hover {{ border: 1px solid {c('ACCENT')}; }}
    QCheckBox::indicator:checked {{
        background: {c('ACCENT')};
        border: 1px solid {c('ACCENT')};
        image: url({_icon_url('check_white.png')});
    }}
    /* Radio: same look as the dropdown-menu exclusive indicator — hollow ring
       unchecked, a fully accent-filled circle when checked (border-radius = half
       the box). 14px to match QMenu::indicator. */
    QRadioButton::indicator {{
        width: 14px; height: 14px;
        border: 1px solid {c('BORDER_FAINT')};
        border-radius: 7px;
        background: {c('BG_DEEP')};
    }}
    QRadioButton::indicator:hover {{ border: 1px solid {c('ACCENT')}; }}
    QRadioButton::indicator:checked {{
        border: 1px solid {c('ACCENT')};
        border-radius: 7px;
        background: {c('ACCENT')};
    }}

    /* Toolbar */
    QToolBar {{
        background: {c('BG_HEADER')};
        border: none;
        spacing: 4px;
        padding: 4px 6px;
    }}
    QToolButton {{
        background: transparent;
        color: {c('TEXT_MAIN')};
        padding: 5px 10px;
        border-radius: 4px;
    }}
    QToolButton:hover {{ background: {c('BG_ROW_HOVER')}; }}
    QToolButton:pressed {{ background: {c('ACCENT')}; color: {c('TEXT_ON_ACCENT')}; }}
    QToolButton::menu-button {{ width: 16px; border-left: 1px solid {c('BORDER')}; }}
    QToolButton::menu-arrow {{ width: 8px; height: 8px; }}

    QMenu {{
        background: {c('BG_PANEL')};
        border: 1px solid {c('BORDER')};
        padding: 5px;
    }}
    QMenu::item {{
        padding: 7px 28px 7px 12px;
        border-radius: 4px;
        margin: 1px 2px;
    }}
    QMenu::item:selected {{ background: {c('BG_SELECT')}; color: {c('TEXT_ON_ACCENT')}; }}
    /* Radio indicator for exclusive (selector) menus — a blue-filled dot when
       checked, a hollow ring otherwise. */
    QMenu::indicator {{
        width: 14px; height: 14px;
        margin-left: 4px;
    }}
    QMenu::indicator:exclusive:unchecked {{
        border: 1px solid {c('BORDER_FAINT')};
        border-radius: 7px;
        background: {c('BG_DEEP')};
    }}
    QMenu::indicator:exclusive:checked {{
        border: 1px solid {c('ACCENT')};
        border-radius: 7px;
        background: {c('ACCENT')};
    }}
    QMenu::separator {{ height: 1px; background: {c('BORDER')}; margin: 5px 8px; }}

    /* List / tree */
    QTreeView, QListView {{
        background: {c('BG_LIST')};
        alternate-background-color: {c('BG_ROW_ALT')};
        border: none;
        outline: none;
    }}
    QTreeView::item:selected, QListView::item:selected {{
        background: {c('BG_SELECT')};
        color: {c('TEXT_ON_ACCENT')};
    }}
    QHeaderView::section {{
        background: {c('BG_HEADER')};
        color: {c('TEXT_DIM')};
        padding: 5px 8px;
        border: none;
        border-right: 1px solid {c('BORDER')};
        border-bottom: 1px solid {c('BORDER')};
    }}

    /* Detachable tabs (overlay replacement) — modern flat look: rounded top,
       no boxy borders, an accent underline on the selected tab. */
    QTabWidget::pane {{ border: none; top: 0; }}
    QTabBar {{ background: {c('BG_HEADER')}; }}
    QTabBar::tab {{
        background: transparent;
        color: {c('TEXT_DIM')};
        /* extra right padding gives the close button its own square area */
        padding: 8px 10px 8px 16px;
        margin: 3px 1px 0 1px;
        border: none;
        border-top-left-radius: 6px;
        border-top-right-radius: 6px;
        /* reserve space for the selected underline so text doesn't shift */
        border-bottom: 2px solid transparent;
    }}
    QTabBar::tab:hover {{
        background: {c('BG_ROW_HOVER')};
        color: {c('TEXT_MAIN')};
    }}
    QTabBar::tab:selected {{
        background: {c('BG_PANEL')};
        color: {c('TEXT_MAIN')};
        border-bottom: 2px solid {c('ACCENT')};
    }}
    /* Close button — a clear square on the right of the tab (matches the
       mockup). Larger hit area, subtle by default, soft rounded red on hover. */
    QTabBar::close-button {{
        image: url({_icon_url('close_white.png')});
        subcontrol-position: right;
        margin: 3px 6px 3px 4px;
        border-radius: 4px;
    }}
    QTabBar::close-button:hover {{ background: {c('BTN_DANGER')}; }}

    /* Slim modern scrollbars — applied globally (modlist, plugins, log, …) */
    QScrollBar:vertical {{
        background: transparent;
        width: 14px;
        margin: 0;
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 12px;
        margin: 0;
    }}
    QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
        background: {c('BORDER_FAINT')};
        border-radius: 5px;
        min-height: 28px;
        min-width: 28px;
        margin: 2px;
    }}
    QScrollBar::handle:hover {{ background: {c('TEXT_DIM')}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{
        width: 0; height: 0; background: none; border: none;
    }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    /* Status bar + bottom bar */
    QStatusBar {{
        background: {c('BG_HEADER')};
        color: {c('TEXT_DIM')};
        border-top: 1px solid {c('BORDER')};
    }}
    QStatusBar::item {{ border: none; }}
    #BottomBar {{
        background: {c('BG_PANEL')};
        border-top: 1px solid {c('BORDER')};
    }}

    /* Generic buttons / inputs */
    QPushButton {{
        background: {c('ACCENT')};
        color: {c('TEXT_ON_ACCENT')};
        border: none;
        padding: 6px 12px;
        border-radius: 4px;
    }}
    QPushButton:hover {{ background: {c('ACCENT_HOV')}; }}
    QComboBox, QLineEdit {{
        background: {c('BG_ROW')};
        border: 1px solid {c('BORDER')};
        border-radius: 4px;
        padding: 4px 8px;
    }}
    QSplitter::handle {{ background: {c('BORDER')}; }}
    QSplitter::handle:horizontal {{ width: 2px; }}

    #StatusChip {{
        background: {c('ACCENT')};
        color: {c('TEXT_ON_ACCENT')};
        border-radius: 3px;
        padding: 3px 8px;
    }}
    #PlaceholderPane {{
        background: {c('BG_PANEL')};
        color: {c('TEXT_FAINT')};
    }}

    /* Add-Game picker cards */
    #GameCard {{
        background: {c('BG_PANEL')};
        border: 1px solid {c('BORDER')};
        border-radius: 8px;
    }}
    #GameCard:hover {{ border: 1px solid {c('ACCENT')}; }}
    #GameCardName {{ color: {c('TEXT_MAIN')}; font-weight: 600; font-size: 12px; }}
    #GameSelectBtn {{
        background: {c('BTN_SUCCESS')}; color: #fff; font-weight: 600;
        border: none; border-radius: 4px; padding: 5px 0;
    }}
    #GameSelectBtn:hover {{ background: {c('BTN_SUCCESS_HOV')}; }}
    #GameAddBtn {{
        background: {c('ACCENT')}; color: {c('TEXT_ON_ACCENT')}; font-weight: 600;
        border: none; border-radius: 4px; padding: 5px 0;
    }}
    #GameAddBtn:hover {{ background: {c('ACCENT_HOV')}; }}

    /* Header bars (left two-tier header + right play bar) */
    #HeaderBar {{
        background: {c('BG_HEADER')};
        border-bottom: 1px solid {c('BORDER')};
    }}
    #GroupSep {{ background: {c('BORDER')}; border: none; }}
    #ActionButton {{
        background: {c('BG_ROW')};
        color: {c('TEXT_MAIN')};
        border: 1px solid {c('BORDER')};
        border-radius: 5px;
        padding: 6px 12px;
        font-size: 14px;
    }}
    /* Split buttons (with a dropdown arrow) need extra right padding so the
       label never runs under the 22px arrow section. */
    #ActionButton[split="true"] {{ padding: 6px 28px 6px 12px; }}
    #ActionButton:hover {{ background: {c('BG_ROW_HOVER')}; }}
    /* Menu open (menuOpen property) OR pressed → whole button + arrow go blue. */
    #ActionButton:pressed, #ActionButton[menuOpen="true"] {{
        background: {c('ACCENT')}; color: {c('TEXT_ON_ACCENT')};
    }}
    /* Split-button arrow section (right of the divider), like the mockup. */
    #ActionButton::menu-button {{
        background: transparent;
        border-left: 1px solid {c('BORDER')};
        width: 22px;
        border-top-right-radius: 5px;
        border-bottom-right-radius: 5px;
    }}
    #ActionButton::menu-button:hover {{ background: {c('BG_ROW_HOVER')}; }}
    /* When the menu is open the arrow section matches the highlighted button. */
    #ActionButton[menuOpen="true"]::menu-button {{
        background: {c('ACCENT')};
        border-left: 1px solid {c('ACCENT_HOV')};
    }}
    #ActionButton::menu-arrow {{ width: 10px; height: 10px; }}
    /* Square icon-only toolbar button (Settings). */
    #IconButton {{
        background: {c('BG_ROW')};
        border: 1px solid {c('BORDER')};
        border-radius: 5px;
    }}
    #IconButton:hover {{ background: {c('BG_ROW_HOVER')}; }}
    #IconButton:pressed {{ background: {c('ACCENT')}; }}
    /* Configure-Game form body + monospace path fields + buttons. */
    #FormBody {{ background: {c('BG_PANEL')}; }}
    #PathEdit {{
        background: {c('BG_ROW')};
        color: {c('TEXT_MAIN')};
        border: 1px solid {c('BORDER')};
        border-radius: 4px;
        padding: 8px 10px;
    }}
    /* Consistent form button (Browse/Open/Scan/Reset, Cancel) — same height as
       the primary Save/Danger buttons so rows line up. */
    #FormButton {{
        background: {c('BG_ROW')};
        color: {c('TEXT_MAIN')};
        border: 1px solid {c('BORDER')};
        border-radius: 4px;
        padding: 0 14px;
        min-height: 30px;
        font-size: 13px;
    }}
    #FormButton:hover {{ background: {c('BG_ROW_HOVER')}; }}
    #FormButton:pressed {{ background: {c('ACCENT')}; color: {c('TEXT_ON_ACCENT')}; }}
    #PrimaryButton {{
        background: {c('ACCENT')}; color: {c('TEXT_ON_ACCENT')}; font-weight: 600;
        border: none; border-radius: 4px; padding: 0 18px;
        min-height: 30px; font-size: 13px;
    }}
    #PrimaryButton:hover {{ background: {c('ACCENT_HOV')}; }}
    #PrimaryButton:disabled {{ background: {c('BG_ROW')}; color: {c('TEXT_DIM')}; }}
    #DangerButton {{
        background: {c('RED_BTN')}; color: #fff; font-weight: 600;
        border: none; border-radius: 4px; padding: 0 16px;
        min-height: 30px; font-size: 13px;
    }}
    #DangerButton:hover {{ background: {c('RED_HOV')}; }}
    #FooterButton {{
        background: {c('BG_ROW')};
        color: {c('TEXT_MAIN')};
        border: 1px solid {c('BORDER')};
        border-radius: 4px;
        padding: 4px 12px;
        font-size: 12px;
    }}
    #FooterButton:hover {{ background: {c('BG_ROW_HOVER')}; }}
    #FooterButton:pressed {{ background: {c('ACCENT')}; color: {c('TEXT_ON_ACCENT')}; }}
    #PlayButton {{
        background: {c('BTN_SUCCESS')};
        color: #fff;
        font-weight: 600;
        font-size: 14px;
        padding: 6px 18px;
        border: none;
        border-radius: 5px;
    }}
    #PlayButton:hover {{ background: {c('BTN_SUCCESS')}; }}

    /* Bottom log panel */
    #LogBar {{
        background: {c('BG_HEADER')};
        border-top: 1px solid {c('BORDER')};
    }}
    #LogView {{
        background: {c('BG_DEEP')};
        color: {c('TEXT_MAIN')};
        border: none;
        border-top: 1px solid {c('BORDER')};
        font-family: monospace;
        font-size: 12px;
    }}
    """


def _apply_qpalette(app, p: dict) -> None:
    """Seed a role-based QPalette so stock widgets (menus, combos, tooltips,
    disabled states) read the theme colours even where QSS doesn't reach."""
    from PySide6.QtGui import QPalette, QColor
    from PySide6.QtCore import Qt

    c = lambda k: QColor(_c(p, k))
    pal = QPalette()
    pal.setColor(QPalette.Window, c("BG_DEEP"))
    pal.setColor(QPalette.WindowText, c("TEXT_MAIN"))
    pal.setColor(QPalette.Base, c("BG_LIST"))
    pal.setColor(QPalette.AlternateBase, c("BG_ROW_ALT"))
    pal.setColor(QPalette.Text, c("TEXT_MAIN"))
    pal.setColor(QPalette.Button, c("BG_HEADER"))
    pal.setColor(QPalette.ButtonText, c("TEXT_MAIN"))
    pal.setColor(QPalette.Highlight, c("BG_SELECT"))
    pal.setColor(QPalette.HighlightedText, c("TEXT_ON_ACCENT"))
    pal.setColor(QPalette.ToolTipBase, c("BG_PANEL"))
    pal.setColor(QPalette.ToolTipText, c("TEXT_MAIN"))
    pal.setColor(QPalette.PlaceholderText, c("TEXT_FAINT"))
    pal.setColor(QPalette.Link, c("LINK_BLUE"))
    pal.setColor(QPalette.BrightText, c("TEXT_WHITE"))
    # Fusion draws bevels/frames from these shade roles — seed them so panels,
    # group boxes, frames and sunken borders read on the dark theme instead of
    # the default near-black/near-white guesses.
    pal.setColor(QPalette.Light, c("BORDER_FAINT"))
    pal.setColor(QPalette.Midlight, c("BORDER_DIM"))
    pal.setColor(QPalette.Mid, c("BORDER"))
    pal.setColor(QPalette.Dark, c("BG_DEEP"))
    pal.setColor(QPalette.Shadow, c("BG_OVERLAY_DEEP"))
    # Keep selection vivid even when the window/widget isn't focused (otherwise
    # Fusion greys the Inactive-group highlight, which looks broken in lists).
    pal.setColor(QPalette.Inactive, QPalette.Highlight, c("BG_SELECT"))
    pal.setColor(QPalette.Inactive, QPalette.HighlightedText, c("TEXT_ON_ACCENT"))
    # Disabled states (greyed text) across all relevant roles.
    dim = c("TEXT_DIM")
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText,
                 QPalette.ToolTipText):
        pal.setColor(QPalette.Disabled, role, dim)
    app.setPalette(pal)


def _make_proxy_style(base):
    """Wrap *base* QStyle in a ProxyStyle that enlarges the tab close indicator
    so the close button fills the tab height (QSS width/height alone is clamped
    by the style's PM_TabCloseIndicator metric)."""
    from PySide6.QtWidgets import QProxyStyle, QStyle

    class _TabProxyStyle(QProxyStyle):
        def pixelMetric(self, metric, option=None, widget=None):
            if metric in (QStyle.PM_TabCloseIndicatorWidth,
                          QStyle.PM_TabCloseIndicatorHeight):
                return 22
            return super().pixelMetric(metric, option, widget)

    proxy = _TabProxyStyle(base) if base is not None else _TabProxyStyle()
    return proxy


def _resolve_base_style(p: dict):
    """Return a created QStyle for the theme's declared base style. A palette may
    set BASE_QSTYLE (MO2-style 'base style per theme'); default Fusion, and a
    real 'Breeze' QStyle is used when the plugin is installed. Falls back through
    Breeze→Fusion→whatever's available."""
    from PySide6.QtWidgets import QStyleFactory
    keys = {k.lower(): k for k in QStyleFactory.keys()}
    wanted = str(p.get("BASE_QSTYLE", "") or "").lower()
    pick = (keys.get(wanted)
            or keys.get("breeze")
            or keys.get("fusion")
            or (QStyleFactory.keys()[0] if QStyleFactory.keys() else None))
    return QStyleFactory.create(pick) if pick else None


def apply_theme(app) -> None:
    """Apply the active theme: a base QStyle (Fusion, or the theme's declared
    BASE_QSTYLE / system Breeze when present) wrapped in a ProxyStyle (enlarged
    tab close button) + a role-based QPalette + the QSS overlay. Mirrors MO2's
    QStyle+QSS model, with QPalette added so Fusion's disabled/tooltip/frame/
    inactive states look right (MO2 leans on native styles for those)."""
    p = active_palette()
    base = _resolve_base_style(p)
    app.setStyle(_make_proxy_style(base))
    _apply_qpalette(app, p)
    app.setStyleSheet(build_qss(p))
