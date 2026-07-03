"""
UI scaling configuration stored in ~/.config/AmethystModManager/amethyst.ini.

Users can set ui_scale (e.g. 1.0, 1.25, 1.5, 2.0) for HiDPI displays.
Set scale=auto to use automatic scaling based on screen size.
"""

import configparser
import os
import re as _re
import subprocess
from pathlib import Path

from Utils.config_paths import get_config_dir

_INI_SECTION = "ui"

# Bumped when amethyst.ini's schema changes incompatibly. On startup,
# ensure_ini_version() wipes a file whose [meta] version differs (or is absent)
# so the Qt build starts from a clean ini — this clears Tk-era ini files when
# users move over.
_META_SECTION = "meta"
_APP_INI_VERSION = 2
# Last-used game + profile (restored at startup).
_SESSION_SECTION = "session"
# First-run onboarding completion flag (missing/0 → show onboarding on launch).
_ONBOARDING_SECTION = "onboarding"
_INI_OPTION = "scale"
_INI_AUTO = "auto"
_DEFAULT_SCALE = 1.0
_MIN_SCALE = 0.5
_MAX_SCALE = 3.0
# Auto-detection caps out lower than the manual ceiling: HiDPI/portal readings
# can be over-eager (e.g. 4K TVs reporting 2.0) and the UI is still legible at
# 1.5x. Users who want larger can still pick it manually.
_AUTO_MAX_SCALE = 1.5

_DEFAULT_FONT_FAMILY = "Noto Sans"
_INI_FONT_OPTION = "font_family"

# Language / locale. Empty string ("") means "follow the system locale"; any
# other value is a locale code (e.g. "en", "de", "fr") matching a compiled
# translations/amethyst_<code>.qm file.
_DEFAULT_LANGUAGE = ""
_INI_LANGUAGE_OPTION = "language"

_ui_scale: float = _DEFAULT_SCALE
_font_family: str = _DEFAULT_FONT_FAMILY
_language: str = _DEFAULT_LANGUAGE


def get_ui_config_path() -> Path:
    """Return the path to the amethyst.ini config file."""
    return get_config_dir() / "amethyst.ini"


def _new_parser() -> "configparser.ConfigParser":
    """A ConfigParser tolerant of a duplicated option/section (last value wins
    instead of raising). amethyst.ini is shared with column_state.py, which uses
    a case-preserving optionxform; a legacy key that differs only in case (e.g.
    ``w_Mod Name`` vs ``w_mod name`` in [qt_columns]) would otherwise make EVERY
    read here raise DuplicateOptionError and break all saves. strict=False lets
    the file still load so settings can be written."""
    return configparser.ConfigParser(strict=False)


def _get_portal_scale() -> float:
    """Read the DE scale via the XDG Settings portal.

    Works inside Flatpak sandboxes and on Wayland where xrandr / kscreen-doctor
    are absent.  Reads ``org.gnome.desktop.interface`` because every major
    portal backend (xdg-desktop-portal-gnome/-kde/-hyprland/-wlr) exposes
    ``scaling-factor`` and ``text-scaling-factor`` under that namespace
    regardless of the actual DE.  Returns the larger of the two, or 1.0 if the
    portal is unreachable.
    """
    def _read(key: str) -> str:
        # Use gdbus if available, fall back to dbus-send — one of the two
        # ships with essentially every Linux distro and Flatpak runtime.
        for cmd in (
            ["gdbus", "call", "--session",
             "--dest", "org.freedesktop.portal.Desktop",
             "--object-path", "/org/freedesktop/portal/desktop",
             "--method", "org.freedesktop.portal.Settings.Read",
             "org.gnome.desktop.interface", key],
            ["dbus-send", "--session", "--print-reply=literal",
             "--dest=org.freedesktop.portal.Desktop",
             "/org/freedesktop/portal/desktop",
             "org.freedesktop.portal.Settings.Read",
             "string:org.gnome.desktop.interface", f"string:{key}"],
        ):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
                if r.returncode == 0 and r.stdout.strip():
                    return r.stdout
            except Exception:
                continue
        return ""

    scale = 1.0
    # Integer scaling-factor (uint32): 0 or 1 → no scaling, ≥2 → HiDPI
    raw = _read("scaling-factor")
    m = _re.search(r"uint32\s+(\d+)", raw) or _re.search(r"\b(\d+)\b", raw)
    if m:
        try:
            v = int(m.group(1))
            if v >= 2:
                scale = max(scale, float(v))
        except ValueError:
            pass
    # Fractional text-scaling-factor (double), e.g. 1.25
    raw = _read("text-scaling-factor")
    m = _re.search(r"([0-9]+\.[0-9]+)", raw)
    if m:
        try:
            v = float(m.group(1))
            if v > 1.0:
                scale = max(scale, v)
        except ValueError:
            pass
    return scale


def _get_compositor_scale() -> float:
    """Return the display compositor's global scale factor (>1.0 on HiDPI).

    Tries, in order:
      1. XDG Settings portal (Flatpak-safe, works on Wayland & fractional)
      2. kscreen-doctor  (KDE Plasma 6 — per-output scale from compositor)
      3. gsettings       (GNOME — integer scaling-factor)
      4. GDK_SCALE / QT_SCALE_FACTOR / GDK_DPI_SCALE environment variables

    Returns 1.0 if nothing is detected or all sources fail.
    """
    portal = _get_portal_scale()
    if portal > 1.0:
        return portal

    # KDE Plasma 6: per-output scale lives in the compositor; kscreen-doctor
    # exposes it.  Output contains ANSI colour codes so strip those first.
    # The binary isn't in Flatpak runtimes, so retry via flatpak-spawn --host
    # (--directory=/ because the sandbox cwd doesn't exist on the host) so
    # Flatpak and AppImage detect the same scale on the same machine.
    out = _run_capture(["kscreen-doctor", "-o"])
    if not out:
        out = _run_capture(["flatpak-spawn", "--host", "--directory=/", "kscreen-doctor", "-o"])
    if out:
        clean = _re.sub(r"\x1b\[[0-9;]*m", "", out)
        scales = [float(m.group(1)) for m in _re.finditer(r"Scale:\s*([\d.]+)", clean)]
        if scales:
            return max(1.0, max(scales))

    # GNOME: integer scaling-factor (fractional scaling is not exposed here,
    # but integer scaling is still better than nothing).  Output looks like
    # "uint32 2" — anchor on the type prefix so the regex doesn't match the
    # "32" inside "uint32".  Inside Flatpak, sandboxed gsettings reads default
    # (empty) dconf, so ask the host when the sandbox copy reports nothing.
    for argv in (
        ["gsettings", "get", "org.gnome.desktop.interface", "scaling-factor"],
        ["flatpak-spawn", "--host", "--directory=/",
         "gsettings", "get", "org.gnome.desktop.interface", "scaling-factor"],
    ):
        out = _run_capture(argv, timeout=2)
        m = _re.search(r"uint32\s+(\d+)", out)
        if m and int(m.group(1)) > 1:
            return float(int(m.group(1)))

    # Environment variables set by some DEs / launch wrappers
    env_scale = 1.0
    for var in ("GDK_SCALE", "QT_SCALE_FACTOR"):
        try:
            v = os.environ.get(var, "").strip()
            if v:
                f = float(v)
                if f > 1.0:
                    env_scale = max(env_scale, f)
        except Exception:
            pass
    # GDK_DPI_SCALE is a fractional multiplier applied on top of GDK_SCALE
    try:
        v = os.environ.get("GDK_DPI_SCALE", "").strip()
        if v:
            f = float(v)
            if f > 1.0:
                env_scale *= f
    except Exception:
        pass
    if env_scale > 1.0:
        return env_scale

    return 1.0


def _parse_xrandr_rects(stdout: str) -> list[tuple[int, int, int, int]]:
    """Parse xrandr output → list of (x, y, w, h) for every connected monitor."""
    rects: list[tuple[int, int, int, int]] = []
    for line in stdout.splitlines():
        if " connected " not in line:
            continue
        m = _re.search(r"(\d+)x(\d+)\+(\d+)\+(\d+)", line)
        if m:
            w, h, x, y = (int(g) for g in m.groups())
            rects.append((x, y, w, h))
    return rects


def _parse_xrandr(stdout: str) -> tuple[int, int]:
    """Pick (w, h) of the 'primary' monitor from xrandr output, else first connected."""
    lines = stdout.splitlines()
    for line in lines:
        if " connected " in line and "primary" in line:
            m = _re.search(r"(\d+)x(\d+)\+\d+\+\d+", line)
            if m:
                return int(m.group(1)), int(m.group(2))
    for line in lines:
        if " connected " in line:
            m = _re.search(r"(\d+)x(\d+)\+\d+\+\d+", line)
            if m:
                return int(m.group(1)), int(m.group(2))
    return 0, 0


def _parse_wlr_randr_rects(stdout: str) -> list[tuple[int, int, int, int]]:
    """Parse wlr-randr output → list of (x, y, w, h) for every monitor with a 'current' mode."""
    rects: list[tuple[int, int, int, int]] = []
    # wlr-randr blocks each monitor; pair "Position: x,y" with the first "current" mode size.
    for block in _re.split(r"\n(?=\S)", stdout):
        size_match = _re.search(r"(\d+)x(\d+) px.*\bcurrent\b", block)
        pos_match = _re.search(r"Position:\s*(\d+),(\d+)", block)
        if size_match and pos_match:
            w, h = int(size_match.group(1)), int(size_match.group(2))
            x, y = int(pos_match.group(1)), int(pos_match.group(2))
            rects.append((x, y, w, h))
    return rects


def _parse_wlr_randr(stdout: str) -> tuple[int, int]:
    """Pick (w, h) of the first 'current' mode line from wlr-randr output."""
    for line in stdout.splitlines():
        m = _re.search(r"(\d+)x(\d+) px.*\bcurrent\b", line)
        if m:
            return int(m.group(1)), int(m.group(2))
    return 0, 0


def _run_capture(argv: list[str], timeout: int = 3) -> str:
    """Run argv, return stdout on success, '' on any failure."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return r.stdout
    except Exception:
        pass
    return ""


def _gdk_monitor_rects() -> list[tuple[int, int, int, int]]:
    """Return per-monitor (x, y, w, h) via Gdk — no external binary needed.

    PyGObject/Gdk is a hard dependency (the GTK splash and file portal use it),
    so this works even on bare WMs (DWM, i3) that ship no xrandr/wlr-randr.
    Gdk reports each monitor's *logical* geometry (the mode actually in use),
    which is exactly what we want for window placement. Tries GTK4's
    Display.get_monitors() first, then GTK3's get_n_monitors()/get_monitor().
    """
    try:
        import gi
        try:
            gi.require_version("Gdk", "4.0")
        except Exception:
            gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk
        display = Gdk.Display.get_default()
        if display is None:
            return []
        rects: list[tuple[int, int, int, int]] = []
        # GTK4: get_monitors() -> Gio.ListModel of Gdk.Monitor
        if hasattr(display, "get_monitors"):
            monitors = display.get_monitors()
            n = monitors.get_n_items()
            for i in range(n):
                g = monitors.get_item(i).get_geometry()
                rects.append((g.x, g.y, g.width, g.height))
        # GTK3: get_n_monitors()/get_monitor(i)
        elif hasattr(display, "get_n_monitors"):
            for i in range(display.get_n_monitors()):
                g = display.get_monitor(i).get_geometry()
                rects.append((g.x, g.y, g.width, g.height))
        return rects
    except Exception:
        return []


def get_monitor_rects() -> list[tuple[int, int, int, int]]:
    """Return list of (x, y, w, h) for every connected monitor.

    Source order: Gdk (no binary needed, works on bare WMs and reports the
    in-use mode) → xrandr (X11) → wlr-randr (wlroots Wayland). Returns [] if
    all fail. Inside Flatpak the sandbox has no xrandr binary, so the CLI paths
    fall through to ``flatpak-spawn --host`` with ``--directory=/`` (the
    sandbox cwd /app/... doesn't exist on the host).
    """
    rects = _gdk_monitor_rects()
    if rects:
        return rects
    out = _run_capture(["xrandr", "--current"])
    if not out:
        out = _run_capture(["flatpak-spawn", "--host", "--directory=/", "xrandr", "--current"])
    if out:
        rects = _parse_xrandr_rects(out)
        if rects:
            return rects
    out = _run_capture(["wlr-randr"])
    if not out:
        out = _run_capture(["flatpak-spawn", "--host", "--directory=/", "wlr-randr"])
    if out:
        rects = _parse_wlr_randr_rects(out)
        if rects:
            return rects
    return []


def _get_primary_monitor_size() -> tuple[int, int]:
    """Return (width, height) of the primary monitor.

    On multi-monitor setups winfo_screenwidth/height returns the combined
    virtual desktop size, which inflates the auto-detected UI scale.  Tries
    xrandr (X11), then wlr-randr (wlroots Wayland).  Inside a Flatpak sandbox
    neither binary is in the runtime, so each call is also retried via
    ``flatpak-spawn --host`` so the host's binaries can answer instead.
    """
    # xrandr — sandbox first, then host (--directory=/ because the sandbox
    # cwd /app/... doesn't exist on the host, and the portal Spawn inherits cwd).
    out = _run_capture(["xrandr", "--current"])
    if not out:
        out = _run_capture(["flatpak-spawn", "--host", "--directory=/", "xrandr", "--current"])
    if out:
        wh = _parse_xrandr(out)
        if wh != (0, 0):
            return wh

    # wlr-randr — sandbox first, then host
    out = _run_capture(["wlr-randr"])
    if not out:
        out = _run_capture(["flatpak-spawn", "--host", "--directory=/", "wlr-randr"])
    if out:
        wh = _parse_wlr_randr(out)
        if wh != (0, 0):
            return wh

    return 0, 0


# Toolkit-specific display probe → (width, height, de_scale). Injected by the
# GUI (set_screen_probe) so this module needn't import a GUI toolkit. When
# unset, get_screen_info falls back to the xrandr/wlr-randr + portal path.
_screen_probe: "callable | None" = None


def set_screen_probe(fn: "callable | None") -> None:
    """Register a callable() -> (width, height, de_scale) display probe."""
    global _screen_probe
    _screen_probe = fn


def get_screen_info() -> tuple[int, int, float]:
    """Return (screen_width, screen_height, detected_scale) for the primary display."""
    if _screen_probe is not None:
        try:
            w, h, de_scale = _screen_probe()
        except Exception:
            return 0, 0, _DEFAULT_SCALE
    else:
        # No GUI toolkit attached: derive size from xrandr/wlr-randr and let the
        # portal/compositor path below supply the scale.
        w, h = _get_primary_monitor_size()
        de_scale = 1.0
    if w <= 0 or h <= 0:
        return w, h, _DEFAULT_SCALE

    # Wayland guard: Tk is X11-only, so on Wayland we run under XWayland and
    # most compositors (GNOME always, KDE in "Scaled by the system" mode)
    # already upscale the window. Scaling again on top would double-scale.
    # The reliable tell is Xft.dpi: when the session expects X11 apps to
    # scale *themselves*, the DE raises Xft.dpi (e.g. 192 at 200%); when the
    # compositor scales them, Xft.dpi stays at ~96. So only trust a reported
    # compositor scale if Xft.dpi confirms we are expected to apply it.
    on_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    compositor = max(_get_portal_scale(), _get_compositor_scale())
    if on_wayland and compositor > 1.0 and de_scale <= 1.05:
        return w, h, 1.0

    # XDG Settings portal / compositor gives an authoritative scale on every
    # backend that supports fractional scaling (GNOME/KDE/wlroots). When it
    # reports a value, trust it and skip the brittle "derive from monitor
    # height" path — that exists only for setups that don't tell us their
    # scale.
    if compositor > 1.0:
        scale = round(min(_AUTO_MAX_SCALE, compositor) * 20) / 20
        return w, h, scale

    # On multi-monitor setups winfo_screenwidth/height is the combined virtual
    # desktop — use xrandr to get just the primary monitor's physical size.
    # xrandr reports unscaled physical pixels, so we divide by de_scale.
    # When xrandr is unavailable (e.g. Flatpak sandbox without host xrandr),
    # Tk's winfo_screenheight on Wayland/XWayland typically reports the
    # logical (already-scaled) size, so dividing again would halve the scale.
    pm_w, pm_h = _get_primary_monitor_size()
    if pm_h > 0:
        w, h = pm_w, pm_h
        physical_h = h / de_scale if de_scale > 1.0 else h
    else:
        physical_h = h
    # Resolution-only fallback. A bare resolution is a weak scaling signal —
    # a 27" 1440p desktop monitor at 100% DE scale wants 1.0 — so only kick
    # in above 1600p (e.g. 4K), capped at _AUTO_MAX_SCALE. Never auto-scale
    # below 1.0: detection is unreliable enough on Wayland / Flatpak /
    # multi-monitor that a sub-1.0 result is almost always wrong.
    if physical_h > 1600:
        scale = min(_AUTO_MAX_SCALE, physical_h / 1080)
    else:
        scale = 1.0
    scale = round(scale * 20) / 20  # Snap to nearest 0.05
    return w, h, scale


def detect_hidpi_scale() -> float:
    """Detect suggested UI scale (compositor-reported, else screen height).

    Prefers the DE/compositor's own scale (portal, kscreen-doctor, gsettings,
    env vars), skipped when the Wayland compositor already upscales XWayland.
    Resolution fallback: heights ≤1600 → 1.0; above scales by h/1080, capped
    at _AUTO_MAX_SCALE (1.5x); manual selection can still go higher.
    """
    _, _, scale = get_screen_info()
    return scale


def load_ui_scale() -> float:
    """Load ui_scale from INI. Returns the value, clamped to [0.5, 3.0].

    When config is missing or scale=auto, uses detect_hidpi_scale() for automatic
    scaling based on screen size.
    """
    global _ui_scale
    path = get_ui_config_path()
    if not path.is_file():
        _ui_scale = detect_hidpi_scale()
        _write_ini(path, _INI_AUTO)
        _seed_first_run_defaults(path)
        return _ui_scale
    try:
        parser = _new_parser()
        parser.read(path)
        if parser.has_section(_INI_SECTION) and parser.has_option(_INI_SECTION, _INI_OPTION):
            raw = parser.get(_INI_SECTION, _INI_OPTION).strip().lower()
            if raw == _INI_AUTO:
                _ui_scale = detect_hidpi_scale()
            else:
                _ui_scale = _clamp(float(raw))
        else:
            _ui_scale = detect_hidpi_scale()
    except (configparser.Error, ValueError):
        _ui_scale = detect_hidpi_scale()
    return _ui_scale


def _write_ini(path: Path, scale_str: str) -> None:
    """Write the [ui] scale to amethyst.ini."""
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _INI_SECTION not in parser:
        parser[_INI_SECTION] = {}
    parser[_INI_SECTION][_INI_OPTION] = scale_str
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


def _seed_first_run_defaults(path: Path) -> None:
    """Write first-run-only defaults for collections and hidden columns.

    Called exactly once, when the INI file is first created. Existing installs
    never run this code path so their behaviour is unchanged.
    """
    try:
        parser = _new_parser()
        if path.is_file():
            parser.read(path)
        if _COLLECTIONS_SECTION not in parser:
            parser[_COLLECTIONS_SECTION] = {}
        parser[_COLLECTIONS_SECTION]["max_concurrent"] = str(_FIRST_RUN_MAX_CONCURRENT)
        parser[_COLLECTIONS_SECTION]["max_extract_workers"] = str(_FIRST_RUN_MAX_EXTRACT_WORKERS)
        if _COLUMNS_SECTION not in parser:
            parser[_COLUMNS_SECTION] = {}
        parser[_COLUMNS_SECTION]["hidden"] = ",".join(str(x) for x in _FIRST_RUN_HIDDEN_COLUMNS)
        parser[_COLUMNS_SECTION]["introduced"] = ",".join(str(x) for x in _FIRST_RUN_HIDDEN_COLUMNS)
        with path.open("w", encoding="utf-8") as f:
            parser.write(f)
    except Exception:
        pass


def save_ui_scale(scale: float | str) -> None:
    """Write ui_scale to INI. Value is clamped to [0.5, 3.0]. Pass 'auto' for automatic."""
    global _ui_scale
    if isinstance(scale, str) and scale.strip().lower() == _INI_AUTO:
        _ui_scale = detect_hidpi_scale()
        scale_str = _INI_AUTO
    else:
        _ui_scale = _clamp(float(scale))
        scale_str = str(_ui_scale)
    _write_ini(get_ui_config_path(), scale_str)


def get_ui_scale() -> float:
    """Return the current ui_scale (call load_ui_scale first at startup)."""
    return _ui_scale


def load_font_family() -> str:
    """Load font_family from INI. Returns the value, or the default if unset."""
    global _font_family
    path = get_ui_config_path()
    if not path.is_file():
        return _font_family
    try:
        parser = _new_parser()
        parser.read(path)
        value = parser.get(_INI_SECTION, _INI_FONT_OPTION, fallback="").strip()
        _font_family = value if value else _DEFAULT_FONT_FAMILY
    except Exception:
        pass
    return _font_family


def save_font_family(family: str) -> None:
    """Persist font_family to amethyst.ini [ui] section."""
    global _font_family
    _font_family = family.strip() or _DEFAULT_FONT_FAMILY
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _INI_SECTION not in parser:
        parser[_INI_SECTION] = {}
    parser[_INI_SECTION][_INI_FONT_OPTION] = _font_family
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


def get_font_family() -> str:
    """Return the current font family (call load_font_family first at startup)."""
    return _font_family


def load_language() -> str:
    """Load the UI language code from amethyst.ini [ui] language.

    Returns "" (follow system locale) when unset. The value is cached so
    get_language() can be called cheaply after this runs once at startup.
    """
    global _language
    path = get_ui_config_path()
    if not path.is_file():
        return _language
    try:
        parser = _new_parser()
        parser.read(path)
        _language = parser.get(
            _INI_SECTION, _INI_LANGUAGE_OPTION, fallback="").strip()
    except Exception:
        pass
    return _language


def save_language(code: str) -> None:
    """Persist the UI language code to amethyst.ini [ui] language.

    Pass "" to follow the system locale. Takes effect on next launch.
    """
    global _language
    _language = (code or "").strip()
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _INI_SECTION not in parser:
        parser[_INI_SECTION] = {}
    parser[_INI_SECTION][_INI_LANGUAGE_OPTION] = _language
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


def get_language() -> str:
    """Return the current language code (call load_language first at startup)."""
    return _language


def _clamp(value: float) -> float:
    return max(_MIN_SCALE, min(_MAX_SCALE, value))


# ---------------------------------------------------------------------------
# Collection settings
# ---------------------------------------------------------------------------
_COLLECTIONS_SECTION = "collections"

# Download order is no longer configurable — collection downloads always use
# the double-ended scheduler (one big-first worker + the rest small-first).
_DEFAULT_MAX_CONCURRENT = 3
_DEFAULT_MAX_EXTRACT_WORKERS = 4

# First-run defaults — written to the INI only when it is being created for
# the first time (see load_ui_scale). Existing installs keep whatever defaults
# they had even if they have never saved these settings explicitly.
_FIRST_RUN_MAX_CONCURRENT = 8
_FIRST_RUN_MAX_EXTRACT_WORKERS = 8
_FIRST_RUN_HIDDEN_COLUMNS = [2, 5, 8]  # category, installed, size


def load_collection_settings() -> dict:
    """Return collection settings dict with keys: max_concurrent, max_extract_workers, check_download_locations, clear_archive_after_install, download_order.

    NB *download_order* is retained ONLY for the legacy Tk settings dialog
    (gui/status_bar.py) — the Qt download scheduler ignores it (downloads always
    use the double-ended big-first + small-first policy)."""
    path = get_ui_config_path()
    defaults = {
        "max_concurrent": _DEFAULT_MAX_CONCURRENT,
        "max_extract_workers": _DEFAULT_MAX_EXTRACT_WORKERS,
        "check_download_locations": True,
        "clear_archive_after_install": False,
        "download_order": "largest",   # legacy Tk key; unused by Qt
    }
    if not path.is_file():
        return defaults
    try:
        parser = _new_parser()
        parser.read(path)
        if not parser.has_section(_COLLECTIONS_SECTION):
            return defaults
        s = parser[_COLLECTIONS_SECTION]
        max_concurrent = int(s.get("max_concurrent", str(_DEFAULT_MAX_CONCURRENT)))
        max_concurrent = max(1, min(8, max_concurrent))
        max_extract_workers = int(s.get("max_extract_workers", str(_DEFAULT_MAX_EXTRACT_WORKERS)))
        max_extract_workers = max(1, min(8, max_extract_workers))
        check_download_locations = s.getboolean("check_download_locations", True)
        clear_archive_after_install = s.getboolean("clear_archive_after_install", False)
        download_order = s.get("download_order", "largest").strip().lower()
        if download_order not in ("largest", "smallest"):
            download_order = "largest"
        return {
            "max_concurrent": max_concurrent,
            "max_extract_workers": max_extract_workers,
            "check_download_locations": check_download_locations,
            "clear_archive_after_install": clear_archive_after_install,
            "download_order": download_order,
        }
    except Exception:
        return defaults


def save_collection_settings(max_concurrent: int,
                              check_download_locations: bool = True,
                              clear_archive_after_install: bool = False,
                              max_extract_workers: int = _DEFAULT_MAX_EXTRACT_WORKERS,
                              download_order: str | None = None) -> None:
    """Persist collection settings to amethyst.ini.

    *download_order* is accepted for backward-compatibility (the Tk settings
    dialog still passes it) but the Qt download scheduler ignores it — downloads
    always use the double-ended (big-first + small-first) policy. When given, it
    is still written through so the Tk UI round-trips.
    """
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _COLLECTIONS_SECTION not in parser:
        parser[_COLLECTIONS_SECTION] = {}
    if download_order is not None:
        parser[_COLLECTIONS_SECTION]["download_order"] = download_order
    parser[_COLLECTIONS_SECTION]["max_concurrent"] = str(max(1, min(8, max_concurrent)))
    parser[_COLLECTIONS_SECTION]["max_extract_workers"] = str(max(1, min(8, max_extract_workers)))
    parser[_COLLECTIONS_SECTION]["check_download_locations"] = "true" if check_download_locations else "false"
    parser[_COLLECTIONS_SECTION]["clear_archive_after_install"] = "true" if clear_archive_after_install else "false"
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


# ---------------------------------------------------------------------------
# Nexus browser settings
# ---------------------------------------------------------------------------
_NEXUS_SECTION = "nexus"


def load_nexus_show_adult() -> bool:
    """Return the persisted show_adult setting (default False)."""
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = _new_parser()
        parser.read(path)
        return parser.getboolean(_NEXUS_SECTION, "show_adult", fallback=False)
    except Exception:
        return False


_COLUMNS_SECTION = "columns"
_WINDOW_SECTION = "window"


def load_column_widths() -> dict[int, int]:
    """Load saved column width overrides from amethyst.ini. Returns {col_index: width}."""
    path = get_ui_config_path()
    if not path.is_file():
        return {}
    try:
        parser = _new_parser()
        parser.read(path)
        if _COLUMNS_SECTION not in parser:
            return {}
        result = {}
        for key, val in parser[_COLUMNS_SECTION].items():
            try:
                result[int(key)] = int(val)
            except (ValueError, TypeError):
                pass
        return result
    except Exception:
        return {}


def save_column_widths(widths: dict[int, int]) -> None:
    """Persist column width overrides to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    # Preserve column order/hidden/sort keys across the section overwrite
    existing_order = parser.get(_COLUMNS_SECTION, "order", fallback=None)
    existing_hidden = parser.get(_COLUMNS_SECTION, "hidden", fallback=None)
    existing_sort_col = parser.get(_COLUMNS_SECTION, "sort_column", fallback=None)
    existing_sort_asc = parser.get(_COLUMNS_SECTION, "sort_ascending", fallback=None)
    parser[_COLUMNS_SECTION] = {str(k): str(v) for k, v in widths.items()}
    if existing_order:
        parser[_COLUMNS_SECTION]["order"] = existing_order
    if existing_hidden is not None:
        parser[_COLUMNS_SECTION]["hidden"] = existing_hidden
    if existing_sort_col is not None:
        parser[_COLUMNS_SECTION]["sort_column"] = existing_sort_col
    if existing_sort_asc is not None:
        parser[_COLUMNS_SECTION]["sort_ascending"] = existing_sort_asc
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


_DEFAULT_COL_ORDER = [2, 3, 4, 5, 6, 7, 8]  # category, flags, conflicts, installed, priority, version, size


def load_column_order() -> list[int]:
    """Load saved column display order from amethyst.ini. Returns list of data col indices [2..6]."""
    path = get_ui_config_path()
    if not path.is_file():
        return list(_DEFAULT_COL_ORDER)
    try:
        parser = _new_parser()
        parser.read(path)
        raw = parser.get(_COLUMNS_SECTION, "order", fallback=None)
        if raw is None:
            return list(_DEFAULT_COL_ORDER)
        order = [int(x) for x in raw.split(",")]
        # Drop unknown ids, de-dup, then append any new defaults the user hasn't seen yet.
        seen: set[int] = set()
        cleaned: list[int] = []
        for x in order:
            if x in _DEFAULT_COL_ORDER and x not in seen:
                cleaned.append(x)
                seen.add(x)
        for x in _DEFAULT_COL_ORDER:
            if x not in seen:
                cleaned.append(x)
                seen.add(x)
        return cleaned if cleaned else list(_DEFAULT_COL_ORDER)
    except Exception:
        return list(_DEFAULT_COL_ORDER)


def save_column_order(order: list[int]) -> None:
    """Persist column display order to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _COLUMNS_SECTION not in parser:
        parser[_COLUMNS_SECTION] = {}
    parser[_COLUMNS_SECTION]["order"] = ",".join(str(x) for x in order)
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


def load_column_hidden() -> set[int]:
    """Load hidden column indices from amethyst.ini. Returns set of data col indices.

    Columns added after a user's first run are folded into their saved hidden set
    once (tracked via the `introduced` key) so new optional columns like Size
    default to hidden for existing installs too, without re-hiding columns the
    user has since chosen to show."""
    path = get_ui_config_path()
    if not path.is_file():
        return set()
    try:
        parser = _new_parser()
        parser.read(path)
        raw = parser.get(_COLUMNS_SECTION, "hidden", fallback=None)
        if raw is None:
            return set()
        hidden = {int(x) for x in raw.split(",") if x.strip()}
        # One-time migration: hide any newly-introduced default-hidden column.
        intro_raw = parser.get(_COLUMNS_SECTION, "introduced", fallback="")
        introduced = {int(x) for x in intro_raw.split(",") if x.strip()}
        new_defaults = set(_FIRST_RUN_HIDDEN_COLUMNS) - introduced
        if new_defaults:
            hidden |= new_defaults
            _save_columns_hidden_and_introduced(path, hidden, introduced | set(_FIRST_RUN_HIDDEN_COLUMNS))
        return hidden
    except Exception:
        return set()


def _save_columns_hidden_and_introduced(path: Path, hidden: set[int], introduced: set[int]) -> None:
    """Persist both the hidden set and the introduced marker together."""
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _COLUMNS_SECTION not in parser:
        parser[_COLUMNS_SECTION] = {}
    parser[_COLUMNS_SECTION]["hidden"] = ",".join(str(x) for x in sorted(hidden))
    parser[_COLUMNS_SECTION]["introduced"] = ",".join(str(x) for x in sorted(introduced))
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


def save_column_hidden(hidden: set[int]) -> None:
    """Persist hidden column indices to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _COLUMNS_SECTION not in parser:
        parser[_COLUMNS_SECTION] = {}
    parser[_COLUMNS_SECTION]["hidden"] = ",".join(str(x) for x in sorted(hidden))
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


def load_sort_state() -> tuple[str | None, bool]:
    """Load saved sort column and direction from amethyst.ini.
    Returns (sort_column, ascending) where sort_column is None if no sort is active."""
    path = get_ui_config_path()
    if not path.is_file():
        return None, True
    try:
        parser = _new_parser()
        parser.read(path)
        col = parser.get(_COLUMNS_SECTION, "sort_column", fallback=None)
        if col == "none":
            col = None
        asc = parser.get(_COLUMNS_SECTION, "sort_ascending", fallback="true").lower() == "true"
        return col, asc
    except Exception:
        return None, True


def save_sort_state(sort_column: str | None, ascending: bool) -> None:
    """Persist sort column and direction to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _COLUMNS_SECTION not in parser:
        parser[_COLUMNS_SECTION] = {}
    parser[_COLUMNS_SECTION]["sort_column"] = sort_column if sort_column is not None else "none"
    parser[_COLUMNS_SECTION]["sort_ascending"] = "true" if ascending else "false"
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


def load_window_geometry() -> str | None:
    """Load saved window geometry string (WxH+X+Y) from amethyst.ini."""
    path = get_ui_config_path()
    if not path.is_file():
        return None
    try:
        parser = _new_parser()
        parser.read(path)
        return parser.get(_WINDOW_SECTION, "geometry", fallback=None)
    except Exception:
        return None


def save_window_geometry(geometry: str) -> None:
    """Persist window geometry string to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _WINDOW_SECTION not in parser:
        parser[_WINDOW_SECTION] = {}
    parser[_WINDOW_SECTION]["geometry"] = geometry
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


# ---------------------------------------------------------------------------
# Dev mode
# ---------------------------------------------------------------------------
_DEV_SECTION = "dev"


def load_dev_mode() -> bool:
    """Return True if [dev] devmode = true is set in amethyst.ini."""
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = _new_parser()
        parser.read(path)
        return parser.get(_DEV_SECTION, "devmode", fallback="false").strip().lower() == "true"
    except Exception:
        return False


def load_force_manual_install() -> bool:
    """Return True if [dev] force_manual_install = true is set in amethyst.ini.

    When True, collection installs use the non-premium manual-download flow
    regardless of the user's actual Nexus premium status.
    """
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = _new_parser()
        parser.read(path)
        return parser.get(_DEV_SECTION, "force_manual_install", fallback="false").strip().lower() == "true"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Folder case normalisation setting
# ---------------------------------------------------------------------------
_FILEMAP_SECTION = "filemap"


def load_normalize_folder_case() -> bool:
    """Return the global normalize_folder_case setting (default True)."""
    path = get_ui_config_path()
    if not path.is_file():
        return True
    try:
        parser = _new_parser()
        parser.read(path)
        return parser.getboolean(_FILEMAP_SECTION, "normalize_folder_case", fallback=True)
    except Exception:
        return True


def save_normalize_folder_case(value: bool) -> None:
    """Persist the normalize_folder_case setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _FILEMAP_SECTION not in parser:
        parser[_FILEMAP_SECTION] = {}
    parser[_FILEMAP_SECTION]["normalize_folder_case"] = "true" if value else "false"
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


# ---------------------------------------------------------------------------
# App update channel setting
# ---------------------------------------------------------------------------
_UPDATES_SECTION = "updates"


def load_allow_prerelease() -> bool:
    """Return the allow_prerelease setting (default False)."""
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = _new_parser()
        parser.read(path)
        return parser.getboolean(_UPDATES_SECTION, "allow_prerelease", fallback=False)
    except Exception:
        return False


def save_allow_prerelease(value: bool) -> None:
    """Persist the allow_prerelease setting to amethyst.ini under [updates]."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _UPDATES_SECTION not in parser:
        parser[_UPDATES_SECTION] = {}
    parser[_UPDATES_SECTION]["allow_prerelease"] = "true" if value else "false"
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


def load_clear_archive_after_install() -> bool:
    """Return the clear_archive_after_install setting (default True)."""
    path = get_ui_config_path()
    if not path.is_file():
        return True
    try:
        parser = _new_parser()
        parser.read(path)
        return parser.getboolean(_FILEMAP_SECTION, "clear_archive_after_install", fallback=True)
    except Exception:
        return True


def save_clear_archive_after_install(value: bool) -> None:
    """Persist the clear_archive_after_install setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _FILEMAP_SECTION not in parser:
        parser[_FILEMAP_SECTION] = {}
    parser[_FILEMAP_SECTION]["clear_archive_after_install"] = "true" if value else "false"
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


def load_keep_fomod_archives() -> bool:
    """Return the keep_fomod_archives setting (default False).

    When True, archives of mods that use a FOMOD installer are always kept
    regardless of the clear_archive_after_install setting.
    """
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = _new_parser()
        parser.read(path)
        return parser.getboolean(_FILEMAP_SECTION, "keep_fomod_archives", fallback=False)
    except Exception:
        return False


def save_keep_fomod_archives(value: bool) -> None:
    """Persist the keep_fomod_archives setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _FILEMAP_SECTION not in parser:
        parser[_FILEMAP_SECTION] = {}
    parser[_FILEMAP_SECTION]["keep_fomod_archives"] = "true" if value else "false"
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


def load_show_summary_tooltips() -> bool:
    """Return the show_summary_tooltips setting (default True)."""
    path = get_ui_config_path()
    if not path.is_file():
        return True
    try:
        parser = _new_parser()
        parser.read(path)
        return parser.getboolean(_FILEMAP_SECTION, "show_summary_tooltips", fallback=True)
    except Exception:
        return True


def save_show_summary_tooltips(value: bool) -> None:
    """Persist the show_summary_tooltips setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _FILEMAP_SECTION not in parser:
        parser[_FILEMAP_SECTION] = {}
    parser[_FILEMAP_SECTION]["show_summary_tooltips"] = "true" if value else "false"
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


def load_hide_bsa_conflicts() -> bool:
    """Return the hide_bsa_conflicts setting (default False).

    When True, the modlist panel hides BSA/BA2 archive conflict flags and the
    BSA conflict parsing is skipped entirely (for better performance).
    """
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = _new_parser()
        parser.read(path)
        return parser.getboolean(_FILEMAP_SECTION, "hide_bsa_conflicts", fallback=False)
    except Exception:
        return False


def save_hide_bsa_conflicts(value: bool) -> None:
    """Persist the hide_bsa_conflicts setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _FILEMAP_SECTION not in parser:
        parser[_FILEMAP_SECTION] = {}
    parser[_FILEMAP_SECTION]["hide_bsa_conflicts"] = "true" if value else "false"
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


def load_rename_mod_after_install() -> bool:
    """Return the rename_mod_after_install setting (default False).

    When True, a rename prompt is shown after each (non-collection) mod install.
    """
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = _new_parser()
        parser.read(path)
        return parser.getboolean(_FILEMAP_SECTION, "rename_mod_after_install", fallback=False)
    except Exception:
        return False


def save_rename_mod_after_install(value: bool) -> None:
    """Persist the rename_mod_after_install setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _FILEMAP_SECTION not in parser:
        parser[_FILEMAP_SECTION] = {}
    parser[_FILEMAP_SECTION]["rename_mod_after_install"] = "true" if value else "false"
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


def load_restore_on_close() -> bool:
    """Return the restore_on_close setting (default False).

    When True, every configured game with active deployment is restored to
    vanilla when the application window is closed.
    """
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = _new_parser()
        parser.read(path)
        return parser.getboolean(_FILEMAP_SECTION, "restore_on_close", fallback=False)
    except Exception:
        return False


def save_restore_on_close(value: bool) -> None:
    """Persist the restore_on_close setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _FILEMAP_SECTION not in parser:
        parser[_FILEMAP_SECTION] = {}
    parser[_FILEMAP_SECTION]["restore_on_close"] = "true" if value else "false"
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


def save_nexus_show_adult(value: bool) -> None:
    """Persist the show_adult setting to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _NEXUS_SECTION not in parser:
        parser[_NEXUS_SECTION] = {}
    parser[_NEXUS_SECTION]["show_adult"] = "true" if value else "false"
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


# ---------------------------------------------------------------------------
# Custom launcher paths
# ---------------------------------------------------------------------------
_PATHS_SECTION = "paths"


def load_heroic_config_path() -> str:
    """Return the user-configured Heroic config directory path, or '' if unset."""
    path = get_ui_config_path()
    if not path.is_file():
        return ""
    try:
        parser = _new_parser()
        parser.read(path)
        return parser.get(_PATHS_SECTION, "heroic_config_path", fallback="").strip()
    except Exception:
        return ""


def save_heroic_config_path(value: str) -> None:
    """Persist the Heroic config directory path to amethyst.ini. Pass '' to clear."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _PATHS_SECTION not in parser:
        parser[_PATHS_SECTION] = {}
    parser[_PATHS_SECTION]["heroic_config_path"] = value.strip()
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


def load_steam_libraries_vdf_path() -> str:
    """Return the user-configured path to Steam's libraryfolders.vdf, or '' if unset."""
    path = get_ui_config_path()
    if not path.is_file():
        return ""
    try:
        parser = _new_parser()
        parser.read(path)
        return parser.get(_PATHS_SECTION, "steam_libraries_vdf", fallback="").strip()
    except Exception:
        return ""


def save_steam_libraries_vdf_path(value: str) -> None:
    """Persist the Steam libraryfolders.vdf path to amethyst.ini. Pass '' to clear."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _PATHS_SECTION not in parser:
        parser[_PATHS_SECTION] = {}
    parser[_PATHS_SECTION]["steam_libraries_vdf"] = value.strip()
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


def load_default_staging_path() -> str:
    """Return the user-configured default mod staging folder, or '' if unset.

    When set, adding a new game uses ``<this>/<game_name>`` as its mod staging
    folder instead of the built-in default (~/.config/AmethystModManager/Profiles).
    """
    path = get_ui_config_path()
    if not path.is_file():
        return ""
    try:
        parser = _new_parser()
        parser.read(path)
        return parser.get(_PATHS_SECTION, "default_staging_path", fallback="").strip()
    except Exception:
        return ""


def save_default_staging_path(value: str) -> None:
    """Persist the default mod staging folder to amethyst.ini. Pass '' to clear."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _PATHS_SECTION not in parser:
        parser[_PATHS_SECTION] = {}
    parser[_PATHS_SECTION]["default_staging_path"] = value.strip()
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


def load_download_cache_path() -> str:
    """Return the user-configured download cache root, or '' if unset.

    When set, archives downloaded for any game are stored under
    ``<this>/<game name>/`` instead of the built-in default
    (~/.config/AmethystModManager/download_cache).
    """
    path = get_ui_config_path()
    if not path.is_file():
        return ""
    try:
        parser = _new_parser()
        parser.read(path)
        return parser.get(_PATHS_SECTION, "download_cache_path", fallback="").strip()
    except Exception:
        return ""


def save_download_cache_path(value: str) -> None:
    """Persist the download cache root to amethyst.ini. Pass '' to clear."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _PATHS_SECTION not in parser:
        parser[_PATHS_SECTION] = {}
    parser[_PATHS_SECTION]["download_cache_path"] = value.strip()
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


def load_onboarding_complete() -> bool:
    """Return True once first-run onboarding has been finished/dismissed.

    False when the file / section / key is missing (covers 'missing or 0'), so
    a fresh amethyst.ini shows the onboarding on launch exactly once.
    """
    path = get_ui_config_path()
    if not path.is_file():
        return False
    try:
        parser = _new_parser()
        parser.read(path)
        return parser.getboolean(_ONBOARDING_SECTION, "complete", fallback=False)
    except Exception:
        return False


def save_onboarding_complete(value: bool) -> None:
    """Persist the onboarding completion flag to amethyst.ini."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _ONBOARDING_SECTION not in parser:
        parser[_ONBOARDING_SECTION] = {}
    parser[_ONBOARDING_SECTION]["complete"] = "1" if value else "0"
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


# ---------------------------------------------------------------------------
# Theme colours
# ---------------------------------------------------------------------------
_THEME_SECTION = "theme"

THEME_DEFAULTS: dict[str, str] = {
    "conflict_higher":    "#108d00",
    "conflict_lower":     "#9a0e0e",
    "plugin_mod":         "#A45500",
    "plugin_separator":   "#A45500",
    "conflict_separator": "#5A5A5A",
    "separator_bg":       "#3E3E3E",
}

# Keys whose default should swap per appearance mode. Each non-dark theme
# contributes its own overrides via a THEME_DEFAULTS_OVERRIDE dict in its
# theme file (src/gui/themes/<mode>.py); ui_config loads them lazily so the
# Utils package doesn't import the gui package at import time.
#
# User overrides in [theme] of amethyst.ini always win — except when a
# saved value exactly matches the dark default for a key that the current
# theme overrides; that's treated as legacy/uncustomised so existing ini
# files don't strand users with dark separators on a light or cyberpunk UI.
_theme_defaults_override_cache: dict[str, dict[str, str]] = {}


# Resolver that maps a theme *mode* name → {key: hex} overrides. Injected by
# the GUI (set_theme_override_resolver) so this module needn't import the GUI's
# theme package. Headless / non-GUI builds leave it unset → no overrides.
_theme_override_resolver: "callable | None" = None


def set_theme_override_resolver(fn: "callable | None") -> None:
    """Register a callable(mode: str) -> dict[str, str] of theme overrides.

    Clears the per-mode cache so a freshly-registered resolver takes effect.
    """
    global _theme_override_resolver
    _theme_override_resolver = fn
    _theme_defaults_override_cache.clear()


def _theme_defaults_override_for(mode: str) -> dict[str, str]:
    """Return {key: hex} overrides declared by the active theme.

    Delegates to the GUI-registered resolver (set_theme_override_resolver);
    yields {} when no resolver is attached or it fails. Results are cached per
    mode to keep the ini read path cheap.
    """
    if mode in _theme_defaults_override_cache:
        return _theme_defaults_override_cache[mode]
    result: dict[str, str] = {}
    if _theme_override_resolver is not None:
        try:
            raw = _theme_override_resolver(mode)
            if isinstance(raw, dict):
                result = {k: v for k, v in raw.items() if k in THEME_DEFAULTS and _valid_hex(v)}
        except Exception:
            pass
    _theme_defaults_override_cache[mode] = result
    return result

_HEX_RE = _re.compile(r"^#[0-9A-Fa-f]{6}$")

_theme_colors: dict[str, str] = dict(THEME_DEFAULTS)


def _valid_hex(s: str) -> bool:
    return isinstance(s, str) and bool(_HEX_RE.match(s.strip()))


def load_theme_colors() -> dict[str, str]:
    """Load [theme] from INI, falling back to defaults for missing/invalid values.

    Theme-aware: the active theme (from get_appearance_mode()) can declare
    THEME_DEFAULTS_OVERRIDE in its theme file to replace defaults for
    user-customisable keys. User overrides in [theme] always win — except
    when a saved value exactly matches the original dark default for a key
    the current theme overrides; that's treated as legacy/uncustomised, so
    existing ini files don't strand users with dark separators on a non-dark
    theme.
    """
    global _theme_colors
    mode = get_appearance_mode()
    overrides = _theme_defaults_override_for(mode)
    result = dict(THEME_DEFAULTS)
    result.update(overrides)
    path = get_ui_config_path()
    if path.is_file():
        try:
            parser = _new_parser()
            parser.read(path)
            if parser.has_section(_THEME_SECTION):
                for key in THEME_DEFAULTS:
                    raw = parser.get(_THEME_SECTION, key, fallback="").strip()
                    if not _valid_hex(raw):
                        continue
                    if (key in overrides
                            and raw.lower() == THEME_DEFAULTS[key].lower()):
                        continue
                    result[key] = raw
        except Exception:
            pass
    _theme_colors = result
    return _theme_colors


def save_theme_color(key: str, value: str) -> None:
    """Persist a single theme colour under [theme] in amethyst.ini.

    Silently ignores unknown keys or invalid hex values to prevent corruption.
    """
    if key not in THEME_DEFAULTS or not _valid_hex(value):
        return
    value = value.strip()
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _THEME_SECTION not in parser:
        parser[_THEME_SECTION] = {}
    parser[_THEME_SECTION][key] = value
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)
    _theme_colors[key] = value


def get_theme_color(key: str) -> str:
    """Return the current cached value for *key*, or its default if unknown."""
    return _theme_colors.get(key, THEME_DEFAULTS.get(key, "#000000"))


# ---------------------------------------------------------------------------
# Appearance mode — applied at startup, requires restart.
#
# Valid values are theme IDs (filenames) under src/gui/themes/ (e.g. "dark",
# "light"). ui_config doesn't validate against that list to avoid importing
# the gui package; theme.py handles unknown IDs by falling back to dark.
# ---------------------------------------------------------------------------
_APPEARANCE_OPTION = "appearance_mode"
_APPEARANCE_DEFAULT = "dark"
_APPEARANCE_ID_RE = _re.compile(r"^[a-z0-9_][a-z0-9_-]*$")


def get_appearance_mode() -> str:
    """Return the saved appearance-mode theme ID, defaulting to 'dark'."""
    path = get_ui_config_path()
    if not path.is_file():
        return _APPEARANCE_DEFAULT
    try:
        parser = _new_parser()
        parser.read(path)
        raw = parser.get(_INI_SECTION, _APPEARANCE_OPTION, fallback=_APPEARANCE_DEFAULT).strip().lower()
        return raw if _APPEARANCE_ID_RE.match(raw) else _APPEARANCE_DEFAULT
    except Exception:
        return _APPEARANCE_DEFAULT


def save_appearance_mode(mode: str) -> None:
    """Persist the appearance mode. Values are normalised to lowercase; any
    string that doesn't match the theme-id regex (lowercase word chars, digits,
    dashes, underscores) is silently rejected."""
    mode = mode.strip().lower()
    if not _APPEARANCE_ID_RE.match(mode):
        return
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    if path.is_file():
        parser.read(path)
    if _INI_SECTION not in parser:
        parser[_INI_SECTION] = {}
    parser[_INI_SECTION][_APPEARANCE_OPTION] = mode
    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


# ---------------------------------------------------------------------------
# Last session (game + profile) — restored at startup
# ---------------------------------------------------------------------------

def load_last_session() -> "tuple[str | None, str | None]":
    """Return (last_game, last_profile) from amethyst.ini, each None if unset."""
    path = get_ui_config_path()
    if not path.is_file():
        return (None, None)
    try:
        parser = _new_parser()
        parser.read(path)
        g = parser.get(_SESSION_SECTION, "last_game", fallback="") or ""
        p = parser.get(_SESSION_SECTION, "last_profile", fallback="") or ""
        return (g or None, p or None)
    except Exception:
        return (None, None)


def save_last_session(game: "str | None", profile: "str | None") -> None:
    """Persist the last-used game + profile to amethyst.ini ([session])."""
    path = get_ui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        parser = _new_parser()
        if path.is_file():
            parser.read(path)
        if _SESSION_SECTION not in parser:
            parser[_SESSION_SECTION] = {}
        parser[_SESSION_SECTION]["last_game"] = game or ""
        parser[_SESSION_SECTION]["last_profile"] = profile or ""
        with path.open("w", encoding="utf-8") as f:
            parser.write(f)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# amethyst.ini schema version gate (migration wipe)
# ---------------------------------------------------------------------------

def ensure_ini_version() -> None:
    """Ensure amethyst.ini matches the current schema version.

    If the file exists but its ``[meta] version`` is missing or != _APP_INI_VERSION
    (an old Tk-era ini), DELETE it so the Qt build starts fresh. Then make sure a
    file exists stamping the current version. amethyst.ini only — other config
    (last_game.json, games/, profiles, caches) is left untouched.

    Call this ONCE at the very start of startup, before anything reads the ini.
    Best-effort: any error falls back to wiping + rewriting so a corrupt/locked
    ini can never block startup.
    """
    path = get_ui_config_path()
    try:
        needs_wipe = False
        if path.is_file():
            try:
                parser = _new_parser()
                parser.read(path)
                ver = parser.getint(_META_SECTION, "version", fallback=0)
            except Exception:
                ver = -1   # unreadable → treat as outdated
            if ver != _APP_INI_VERSION:
                needs_wipe = True
        if needs_wipe:
            try:
                path.unlink()
            except OSError:
                pass
        if not path.is_file():
            # Fresh stamp (brand-new install or just-wiped).
            path.parent.mkdir(parents=True, exist_ok=True)
            parser = _new_parser()
            parser[_META_SECTION] = {"version": str(_APP_INI_VERSION)}
            with path.open("w", encoding="utf-8") as f:
                parser.write(f)
        else:
            # File is current but make sure the version key is present/correct.
            parser = _new_parser()
            parser.read(path)
            if (not parser.has_section(_META_SECTION)
                    or parser.get(_META_SECTION, "version", fallback="")
                    != str(_APP_INI_VERSION)):
                if _META_SECTION not in parser:
                    parser[_META_SECTION] = {}
                parser[_META_SECTION]["version"] = str(_APP_INI_VERSION)
                with path.open("w", encoding="utf-8") as f:
                    parser.write(f)
    except Exception:
        # Last resort: try a clean rewrite; swallow anything so startup proceeds.
        try:
            parser = _new_parser()
            parser[_META_SECTION] = {"version": str(_APP_INI_VERSION)}
            with path.open("w", encoding="utf-8") as f:
                parser.write(f)
        except Exception:
            pass
