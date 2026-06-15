"""Transparent GTK splash screen — launched as a subprocess by gui.py.

Usage:  python splash_gtk.py <image_path> [<x> <y>]

Exits cleanly when terminated (SIGTERM from parent).
Falls back silently if GTK / pycairo is unavailable.
"""
import sys
import os


def main():
    try:
        import gi
        gi.require_version('Gtk', '3.0')
        from gi.repository import Gtk, Gdk, GdkPixbuf
        import cairo
    except Exception:
        sys.exit(1)

    image_path = sys.argv[1] if len(sys.argv) > 1 else None
    x = int(sys.argv[2]) if len(sys.argv) > 2 else None
    y = int(sys.argv[3]) if len(sys.argv) > 3 else None

    # POPUP type is never managed by the window manager — no title bar,
    # no borders, no decorations, guaranteed.
    win = Gtk.Window(type=Gtk.WindowType.POPUP)
    win.set_keep_above(True)
    win.set_app_paintable(True)
    win.connect('destroy', Gtk.main_quit)

    # Enable RGBA visual for per-pixel transparency (requires a compositor)
    screen = win.get_screen()
    visual = screen.get_rgba_visual()
    if visual and screen.is_composited():
        win.set_visual(visual)

    pixbuf = None
    if image_path and os.path.isfile(image_path):
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(image_path)
            win.set_default_size(pixbuf.get_width(), pixbuf.get_height())
        except Exception:
            pass

    if pixbuf is None:
        win.set_default_size(300, 80)

    def on_draw(widget, cr):
        # Paint transparent background first (clears any default window bg)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.paint()
        # Composite the PNG with full alpha intact
        if pixbuf:
            cr.set_operator(cairo.OPERATOR_OVER)
            Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
            cr.paint()
        return False

    win.connect('draw', on_draw)
    # GDK_BACKEND=x11 is set by the parent process, so Gtk.WindowType.POPUP
    # maps directly to an X11 override_redirect window — bypasses KWin entirely.
    win.show_all()

    def _centred_xy():
        """Compute a centred position ourselves as a fallback.

        The parent passes pre-computed x/y, but if it couldn't query the
        monitor layout it may pass nothing.  Centre on the monitor under the
        pointer (or the primary monitor) using GdkDisplay geometry.
        """
        try:
            display = win.get_display()
            try:
                seat = display.get_default_seat()
                ptr = seat.get_pointer()
                _scr, _px, _py = ptr.get_position()
                monitor = display.get_monitor_at_point(_px, _py)
            except Exception:
                monitor = display.get_primary_monitor() or display.get_monitor(0)
            geo = monitor.get_geometry()
            ww, wh = win.get_size()
            return geo.x + (geo.width - ww) // 2, geo.y + (geo.height - wh) // 2
        except Exception:
            return None, None

    def _place():
        nonlocal x, y
        if x is None or y is None:
            x, y = _centred_xy()
        if x is not None and y is not None:
            win.move(x, y)
        else:
            win.set_position(Gtk.WindowPosition.CENTER)

    _place()

    # On wlroots compositors (Hyprland, Sway) the move() above is frequently
    # dropped because the surface isn't mapped yet — the WM then places the
    # window at an arbitrary spot.  Re-issue the move once from an idle/timeout
    # callback after the surface has been mapped, which those compositors honour.
    from gi.repository import GLib
    win.connect('map', lambda *_: GLib.idle_add(_place))
    GLib.timeout_add(50, lambda: (_place(), False)[1])

    Gtk.main()


if __name__ == '__main__':
    main()
