"""
Shared theme constants and icon loader for the GUI.
Used by gui.py and all gui submodules.
"""

from functools import lru_cache
from pathlib import Path

import customtkinter as ctk
from PIL import Image as PilImage

from Utils.ui_config import (
    get_ui_scale, get_font_family, load_font_family,
    load_theme_colors, get_theme_color, get_appearance_mode,
)

# ---------------------------------------------------------------------------
# Color palette
# ---------------------------------------------------------------------------
# Palettes live in src/gui/themes/. Each *.py file there defines a PALETTE
# dict + NAME; this module discovers them at import time and binds the
# palette named by the user's appearance_mode setting. Switching requires an
# app restart.

from gui.themes import load_palettes

_PALETTES: dict[str, dict[str, str | tuple]] = load_palettes()


def _bind_palette(mode: str) -> None:
    """Bind the selected palette's values onto this module's globals.

    Falls back to 'dark' if the requested palette is missing. If 'dark'
    itself is missing (shouldn't happen — it ships with the app) we pick
    any available palette so the app doesn't crash on import.
    """
    palette = _PALETTES.get(mode)
    if palette is None:
        palette = _PALETTES.get("dark") or next(iter(_PALETTES.values()), {})
    globals().update(palette)


def available_themes() -> dict[str, str]:
    """Return {theme_id: display_name} for the Settings dropdown."""
    from gui.themes import load_display_names
    return load_display_names()


# Static name declarations so linters / IDE autocomplete can see every
# constant. The values are placeholders — _bind_palette() below overwrites
# them immediately with the real palette values.
BG_DEEP = BG_PANEL = BG_HEADER = BG_ROW = BG_ROW_ALT = BG_ROW_HOVER = ""
BG_LIST = BG_SEP = BG_HOVER = BG_SELECT = BG_HOVER_ROW = ""
ACCENT = ACCENT_HOV = TEXT_ON_ACCENT = ""
TEXT_MAIN = TEXT_DIM = TEXT_MUTED = TEXT_FAINT = TEXT_SEP = ""
TEXT_WHITE = TEXT_BLACK = TEXT_OK = TEXT_ERR = TEXT_WARN = ""
TEXT_OK_BRIGHT = TEXT_ERR_BRIGHT = TEXT_WARN_BRIGHT = ""
BORDER = BORDER_DIM = BORDER_FAINT = ""
RED_BTN = RED_HOV = ""
BTN_DANGER = BTN_DANGER_HOV = BTN_DANGER_ALT = BTN_DANGER_ALT_HOV = ""
BTN_DANGER_DEEP = BTN_DANGER_DEEP_HOV = BTN_CANCEL = BTN_CANCEL_HOV = ""
BTN_SUCCESS = BTN_SUCCESS_HOV = BTN_SUCCESS_ALT = BTN_SUCCESS_ALT_HOV = ""
BTN_SUCCESS_DEEP = BTN_SUCCESS_DEEP_HOV = ""
BTN_WARN = BTN_WARN_HOV = BTN_WARN_DEEP = BTN_WARN_DEEP_HOV = ""
BTN_WARN_BROWN = BTN_WARN_BROWN_HOV = BTN_WARN_ORANGE = BTN_WARN_ORANGE_HOV = ""
BTN_INFO = BTN_INFO_HOV = BTN_INFO_DEEP = BTN_INFO_DEEP_HOV = ""
BTN_NEUTRAL = BTN_NEUTRAL_HOV = ""
BTN_GREY = BTN_GREY_HOV = BTN_GREY_ALT = BTN_GREY_ALT_HOV = ""
BTN_PURPLE = BTN_PURPLE_HOV = ""
TAG_FOLDER = TAG_BSA = TAG_BSA_ALT = TAG_INI_PROFILE = ""
TAG_BUNDLED_FG = TAG_BUNDLED_BG = TAG_INSTALLED_BG = TAG_UNORDERED_FG = ""
TONE_GREEN = TONE_RED = TONE_BLUE = TONE_CYAN = TONE_BLUE_SOFT = TONE_FLAG = ""
SCROLL_BG = SCROLL_TROUGH = SCROLL_ACTIVE = ""
BG_OVERLAY_ERR = BG_OVERLAY_DEEP = BG_CARD = BG_CARD_ALT = ""
BG_GREEN_ROW = BG_GREEN_DEEP = BG_RED_DEEP = BG_GREEN_TEXT = BG_RED_TEXT = ""
BG_ORANGE_DEEP = BG_ORANGE_TEXT = ""
BG_BLUE_DEEP = BG_BLUE_TEXT = ""
BG_DARK_BLUE = BG_DARK_GREEN = BG_ENTRY = BG_BTN_SAVE = BG_SELECT_BAR = ""
BG_MOD_REQ = BG_MOD_OPT = ""
STATUS_ERR_BRIGHT = STATUS_BADGE_RED = STATUS_BADGE_GREEN = ""
STATUS_SUCCESS_SOLID = STATUS_QUEUED = STATUS_DL_GREEN = ""
TEXT_CARD = TEXT_CARD_DIM = TEXT_CARD_MED = TEXT_TREE_FG = ""
CTK_TEXT: tuple = ("", "")
CTK_FOOTER_FG: tuple = ("", "")
CTK_FOOTER_HOV: tuple = ("", "")
CTK_SEP: tuple = ("", "")
CTK_SEP_ALT: tuple = ("", "")
CTK_BTN_HOVER: tuple = ("", "")
LINK_BLUE = ""

APPEARANCE_MODE = get_appearance_mode()
_bind_palette(APPEARANCE_MODE)

# ---------------------------------------------------------------------------
# Contrast helper
# ---------------------------------------------------------------------------
@lru_cache(maxsize=256)
def contrasting_text_color(hex_bg: str) -> str:
    """Return '#111111' or '#eeeeee' (dark/light) based on the luminance of
    *hex_bg* (e.g. '#3a7bd5') so text always stays readable."""
    try:
        hex_bg = hex_bg.lstrip("#")
        r, g, b = (int(hex_bg[i:i+2], 16) / 255.0 for i in (0, 2, 4))
        # Convert sRGB to linear light (WCAG 2 formula)
        def _lin(c: float) -> float:
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        lum = 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)
        # Threshold: use dark text on light backgrounds, light text on dark
        return "#111111" if lum > 0.179 else "#eeeeee"
    except Exception:
        return TEXT_SEP


@lru_cache(maxsize=256)
def hover_tint(hex_bg: str, amount: int = 20) -> str:
    """Return a hover variant of *hex_bg* — brighter by *amount* per channel,
    or darker if brightening would clip (channel already near 255).

    When darkening a near-saturated colour, a plain ``-amount`` on one channel
    is barely perceptible (e.g. pure red #ff0000 only shifts luminance by ~8%).
    So darkening scales all channels proportionally toward black to produce a
    shift roughly comparable to the lighten case.
    """
    try:
        h = hex_bg.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        # If any channel would clip when brightening, darken instead.
        if max(r, g, b) + amount > 255:
            # Scale toward black by ~15% — perceptible regardless of hue.
            factor = 0.82
            r = int(r * factor)
            g = int(g * factor)
            b = int(b * factor)
        else:
            r = min(255, r + amount)
            g = min(255, g + amount)
            b = min(255, b + amount)
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return hex_bg

# Highlight colours — user-customisable via Settings → Theme, persisted in amethyst.ini.
load_theme_colors()
plugin_separator   = get_theme_color("plugin_separator")
plugin_mod         = get_theme_color("plugin_mod")
conflict_separator = get_theme_color("conflict_separator")
conflict_higher    = get_theme_color("conflict_higher")
conflict_lower     = get_theme_color("conflict_lower")
BG_SEP             = get_theme_color("separator_bg")


def refresh_theme_colors() -> None:
    """Re-read theme colours from amethyst.ini and rebind the module globals.

    Rendering sites must use `theme.X` attribute access for values to update
    live — callers that captured names via `from gui.theme import X` will see
    their old (startup) binding only.
    """
    global plugin_separator, plugin_mod, conflict_separator, conflict_higher, conflict_lower, BG_SEP
    load_theme_colors()
    plugin_separator   = get_theme_color("plugin_separator")
    plugin_mod         = get_theme_color("plugin_mod")
    conflict_separator = get_theme_color("conflict_separator")
    conflict_higher    = get_theme_color("conflict_higher")
    conflict_lower     = get_theme_color("conflict_lower")
    BG_SEP             = get_theme_color("separator_bg")

# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
# Font families — loaded from amethyst.ini at startup via load_font_family().
load_font_family()
FONT_FAMILY = get_font_family()
FONT_MONO_FAMILY = "Liberation Mono"

# Scaling contract (two widget worlds, two font families of constants):
#   - CustomTkinter widgets scale user values themselves (set_widget_scaling):
#     font tuples, width/height/wraplength args, CTkImage sizes and window
#     geometry are all multiplied by ui_scale inside CTk. They must receive
#     UNSCALED design values: FONT_*, CTK_FS*, font_sized(), plain ints.
#   - Plain tk/ttk/canvas widgets scale nothing. They must receive PRE-SCALED
#     values: TK_FONT_*, FS*, font_sized_px(), scaled().
# Passing a pre-scaled value to a CTk widget double-scales it (ui_scale²) —
# that was the historic HiDPI cut-off bug.
def _font_pt(base: int) -> int:
    """Return the design point size for CTk font tuples (CTk scales these)."""
    return max(8, round(base))


def font_sized(name: str, base_pt: int, *styles: str) -> tuple:
    """Return an UNSCALED font tuple (name, size, *styles) for CTk widgets.
    Use for one-off fonts instead of hardcoding e.g. font=(FONT_FAMILY, 11)."""
    return (name, _font_pt(base_pt), *styles)


def font_sized_px(name: str, base_pt: int, *styles: str) -> tuple:
    """Return a ui_scale-scaled negative-pixel font tuple for plain tk widgets."""
    return (name, _pt_to_px(base_pt), *styles)


# Unscaled font tuples — CTk widgets only (CTk multiplies by ui_scale itself).
FONT_NORMAL = (FONT_FAMILY, _font_pt(14))
FONT_BOLD   = (FONT_FAMILY, _font_pt(14), "bold")
FONT_SMALL  = (FONT_FAMILY, _font_pt(12))
FONT_MONO   = (FONT_MONO_FAMILY, _font_pt(14))
FONT_SEP    = (FONT_FAMILY, _font_pt(12), "bold")
FONT_HEADER = (FONT_FAMILY, _font_pt(12), "bold")

# Pixel sizes for tk.Label / canvas create_text / ttk.Style font= args.
# Negative values tell Tk to treat them as pixels rather than points,
# bypassing Tk's own DPI scaling (which would double-scale on HiDPI).
# The pixel count is fixed at what the design looks like at 96 DPI
# (tk scaling 1.3333): e.g. 11pt * 1.3333 ≈ 15px.
# Multiplied by get_ui_scale() for HiDPI support.
_BASELINE = 1.3333  # 96 DPI / 72 pt

def _pt_to_px(pt: int) -> int:
    """Convert a design point size to a ui_scaled negative-pixel size (tk only)."""
    return -max(8, round(pt * _BASELINE * get_ui_scale()))


def _pt_to_px_design(pt: int) -> int:
    """Convert a design point size to an UNSCALED negative-pixel size (CTk only)."""
    return -max(8, round(pt * _BASELINE))

# Pre-scaled pixel sizes — plain tk/ttk/canvas widgets only.
FS9  = _pt_to_px(9)
FS10 = _pt_to_px(10)
FS11 = _pt_to_px(11)
FS12 = _pt_to_px(12)
FS13 = _pt_to_px(13)
FS16 = _pt_to_px(16)

# Unscaled pixel sizes — CTk widgets only (CTk multiplies by ui_scale itself).
CTK_FS9  = _pt_to_px_design(9)
CTK_FS10 = _pt_to_px_design(10)
CTK_FS11 = _pt_to_px_design(11)
CTK_FS12 = _pt_to_px_design(12)
CTK_FS13 = _pt_to_px_design(13)
CTK_FS16 = _pt_to_px_design(16)

# Pre-scaled font tuples — plain tk/ttk/canvas widgets only.
TK_FONT_NORMAL = (FONT_FAMILY, _pt_to_px(14))
TK_FONT_BOLD   = (FONT_FAMILY, _pt_to_px(14), "bold")
TK_FONT_SMALL  = (FONT_FAMILY, _pt_to_px(12))
TK_FONT_MONO   = (FONT_MONO_FAMILY, _pt_to_px(14))
TK_FONT_SEP    = (FONT_FAMILY, _pt_to_px(12), "bold")
TK_FONT_HEADER = (FONT_FAMILY, _pt_to_px(12), "bold")


def init_fonts(widget) -> None:
    """Set Tk scaling to design baseline. Font scaling is done via explicit
    point sizes in FONT_* and FS* (not via tk scaling) so it works on Linux
    where CustomTkinter doesn't scale user-provided fonts.
    """
    try:
        widget.tk.call("tk", "scaling", _BASELINE)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Scrollbar helper — single source of truth for tk.Scrollbar styling.
# ---------------------------------------------------------------------------
def scrollbar_kwargs() -> dict:
    """Return the kwargs every tk.Scrollbar in the app uses.
    Callers: ``tk.Scrollbar(parent, orient=..., command=..., **scrollbar_kwargs())``.
    """
    return dict(
        bg=SCROLL_BG,
        troughcolor=SCROLL_TROUGH,
        activebackground=SCROLL_ACTIVE,
        highlightthickness=0,
        bd=0,
    )

# ---------------------------------------------------------------------------
# Icons (package-relative: src/gui/theme.py -> src/icons)
# ---------------------------------------------------------------------------
_ICONS_DIR = Path(__file__).resolve().parent.parent / "icons"


def load_icon(name: str, size: tuple[int, int] = (16, 16)) -> ctk.CTkImage | None:
    """Load a CTkImage from the icons directory. Returns None if file not found.

    Pass the UNSCALED design size — CTk scales CTkImages by widget scaling
    (ui_scale) itself when rendering.
    """
    path = _ICONS_DIR / name
    if not path.is_file():
        return None
    img = PilImage.open(path).convert("RGBA")
    return ctk.CTkImage(light_image=img, dark_image=img, size=(size[0], size[1]))


def scaled(px: int | float) -> int:
    """Scale a pixel value by the current UI scale. Use for layout constants
    (row heights, paddings, icon sizes) that CustomTkinter does not scale."""
    return max(1, round(float(px) * get_ui_scale()))


def scaled_layout_minsize(base: int | float) -> int:
    """Scale a layout minsize so it preserves weight ratios at low scales.

    At 1x scale returns *base* unchanged. Below 1x uses scale² so minsize
    shrinks more aggressively and doesn't force ~50/50 when weights expect 5:4.
    Use for grid minsize in weighted layouts (e.g. plugin panel).
    """
    s = get_ui_scale()
    if s >= 1.0:
        return max(1, round(float(base) * s))
    # Below 1x: use scale² so 0.5x → base*0.25, 0.75x → base*0.56
    return max(1, round(float(base) * s * s))
