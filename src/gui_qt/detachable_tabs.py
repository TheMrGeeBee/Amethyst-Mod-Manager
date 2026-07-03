"""Detachable tab widget — the Qt replacement for the Tk overlays.

Views that the Tk app showed as overlays (Add Game, Nexus browser, settings,
wizards, …) open here as TABS instead of new windows. A tab can be:
  * switched between (multiple "overlays" open at once),
  * closed,
  * dragged out of the tab bar → becomes a floating window,
  * closed → redocks back as a tab (close == redock),
  * dragged back over the tab bar → redocks as a tab.
Programmatic close_tab(key) dismisses the view for real (no redock).

State is preserved across detach/reattach because the page widget is reparented,
never recreated.

Usage:
    tabs = DetachableTabWidget()
    tabs.add_permanent(mods_widget, "Mods")      # tab 0, not closable
    tabs.open_tab(add_game_widget, "Add game", key="add_game")  # focus if exists
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QPoint, Signal, QCoreApplication
from PySide6.QtWidgets import (
    QTabWidget, QTabBar, QWidget, QVBoxLayout, QMainWindow, QStackedWidget,
    QLabel,
)


# -- reliable mouse-button-down probe -----------------------------------------
# Qt's QGuiApplication.mouseButtons() is NOT updated during a window-manager
# title-bar drag on X11 (the WM grabs the pointer), so it can't tell us when the
# user lets go. XQueryPointer reads the real server-side button state and works
# even mid-grab. We probe libX11 via ctypes (always present on an X11 session);
# anything else falls back to Qt's state.
_X11_BTN1 = 0x100   # Button1Mask


def _make_x11_button_probe():
    try:
        import ctypes
        import ctypes.util
        from PySide6.QtWidgets import QApplication
        if QApplication.instance() is None or \
                QApplication.platformName() not in ("xcb", "x11"):
            return None
        lib = ctypes.util.find_library("X11")
        if not lib:
            return None
        x = ctypes.CDLL(lib)
        x.XOpenDisplay.restype = ctypes.c_void_p
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XDefaultRootWindow.restype = ctypes.c_ulong
        x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x.XQueryPointer.restype = ctypes.c_int
        x.XQueryPointer.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint)]
        dpy = x.XOpenDisplay(None)
        if not dpy:
            return None
        root = x.XDefaultRootWindow(dpy)
        rr = ctypes.c_ulong(); cc = ctypes.c_ulong()
        rx = ctypes.c_int(); ry = ctypes.c_int()
        wx = ctypes.c_int(); wy = ctypes.c_int()
        mask = ctypes.c_uint()

        def _probe() -> bool:
            x.XQueryPointer(dpy, root, rr, cc, rx, ry, wx, wy, mask)
            return bool(mask.value & _X11_BTN1)
        return _probe
    except Exception:
        return None


_x11_button_probe = None
_x11_probe_inited = False


def _left_button_down() -> bool:
    """True while the left mouse button is physically held — reliable during a
    WM title-bar drag (X11 via XQueryPointer), else Qt's mouseButtons()."""
    global _x11_button_probe, _x11_probe_inited
    if not _x11_probe_inited:
        _x11_button_probe = _make_x11_button_probe()
        _x11_probe_inited = True
    if _x11_button_probe is not None:
        try:
            return _x11_button_probe()
        except Exception:
            pass
    from PySide6.QtGui import QGuiApplication
    return bool(QGuiApplication.mouseButtons() & Qt.LeftButton)


def _have_reliable_release_probe() -> bool:
    """True when we can detect the real mouse-button-release during a WM drag
    (X11 XQueryPointer). On native Wayland we CAN'T (the compositor owns the drag
    + global button state is blocked), so the drag-back gesture would misfire —
    callers disable it there and rely on close-to-redock instead.
    NOTE: re-test on a Wayland session — XWayland reports platformName 'xcb' and
    works; only native 'wayland' lacks the probe."""
    global _x11_button_probe, _x11_probe_inited
    if not _x11_probe_inited:
        _x11_button_probe = _make_x11_button_probe()
        _x11_probe_inited = True
    return _x11_button_probe is not None


class _DetachTabBar(QTabBar):
    """Tab bar that emits detach_requested when a tab is dragged far enough
    outside the bar (the gesture people get subtly wrong — so it's centralised
    here with a clear vertical-distance threshold)."""

    detach_requested = Signal(int, QPoint)   # (tab index, global drop pos)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMovable(True)               # reorder within the bar
        self._press_index = -1

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_index = self.tabAt(event.position().toPoint())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # Detach when the pointer leaves the bar vertically by a clear margin
        # (so ordinary horizontal reordering still works).
        if self._press_index != -1:
            off = event.position().toPoint()
            if off.y() < -24 or off.y() > self.height() + 24:
                idx = self._press_index
                self._press_index = -1
                self.detach_requested.emit(idx, event.globalPosition().toPoint())
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._press_index = -1
        super().mouseReleaseEvent(event)


class _FloatingTab(QMainWindow):
    """A detached tab living in its own window. Closing the window REDOCKS the
    view back into the tab bar (close == redock); dragging it back over the tab
    bar also redocks. The page widget is reparented, never recreated."""

    # Emitted when the window should give its page back to the tab widget.
    redock_requested = Signal(QWidget, str)   # (page, title)
    moved = Signal(object)                     # (self) — drag-in-progress tracking
    drag_finished = Signal(object)             # (self) — title-bar drag released

    def __init__(self, page: QWidget, title: str, parent=None):
        super().__init__(parent)
        self._page = page
        self._title = title
        self._redocking = False
        self.setWindowTitle(title)
        self.setCentralWidget(page)
        page.show()                            # was hidden by removeTab → show it
        self.resize(max(900, page.sizeHint().width()),
                    max(600, page.sizeHint().height()))

    def take_page(self) -> QWidget | None:
        """Release the page without deleting it (for redocking)."""
        self._redocking = True
        return self.takeCentralWidget()

    def moveEvent(self, event):
        super().moveEvent(event)
        self.moved.emit(self)

    def event(self, event):
        # A native title-bar drag ends with a NonClientArea mouse-button-release
        # (Qt receives this even though it doesn't own the drag). Use it as the
        # reliable "let go" signal — mouseButtons() is unreliable mid-WM-drag.
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.NonClientAreaMouseButtonRelease:
            self.drag_finished.emit(self)
        return super().event(event)

    def closeEvent(self, event):
        # Close means redock (unless the page was already taken for a drag-back
        # redock, in which case just let the empty window close).
        if not self._redocking:
            page = self.takeCentralWidget()
            if page is not None:
                self.redock_requested.emit(page, self._title)
        super().closeEvent(event)


class DetachableTabWidget(QTabWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._bar = _DetachTabBar(self)
        self.setTabBar(self._bar)
        self.setTabsClosable(True)
        self.setMovable(True)
        self.setDocumentMode(True)
        self._bar.setDrawBase(False)        # no baseline frame under the tabs
        self._bar.setExpanding(False)       # tabs hug their text (left-aligned)
        self._permanent: set[int] = set()      # indices that can't be closed
        self._keys: dict[str, QWidget] = {}     # key → page (focus-if-open)
        self._floats: list[_FloatingTab] = []
        # Panel-scoped tabs: a tab in this bar that, instead of a full-UI page,
        # takes over JUST one panel (a target QStackedWidget). id(placeholder) →
        # (target_stack, scoped_widget, scoped_page_index). When such a tab is
        # selected we keep the permanent full-UI page visible and flip the
        # target stack to the scoped page; deselecting restores the stack to 0.
        self._scoped: dict[int, tuple] = {}
        self._permanent_widget: QWidget | None = None
        self._active_scoped: int | None = None

        self.tabCloseRequested.connect(self._on_close_requested)
        self._bar.detach_requested.connect(self._detach)
        self.currentChanged.connect(self._on_current_changed)

    def _update_bar_visibility(self):
        self.tabBar().setVisible(self.count() > 1)

    def _on_current_changed(self, index: int):
        self._update_bar_visibility()
        w = self.widget(index)
        scoped = self._scoped.get(id(w)) if w is not None else None
        if scoped is not None:
            # A panel-scoped tab was selected. Its own page is an empty
            # placeholder — we don't want to show that. Instead snap the content
            # back to the permanent full-UI page and flip the target panel stack
            # to the scoped widget, so the full UI stays live with one panel
            # showing the scoped view. The tab stays HIGHLIGHTED via the bar's
            # own current index (we leave the bar on the scoped tab; only the
            # displayed page is forced to the permanent one).
            target_stack, scoped_widget, _page_idx = scoped
            self._active_scoped = id(w)
            # Reset every scoped stack to page 0 FIRST, then activate this one
            # LAST. Two scoped tabs can share one target stack (e.g. Change
            # Version + Missing Requirements both scope the plugins panel); a
            # single pass would let the "reset other → 0" step clobber the page
            # this tab just set on the shared stack, blanking the panel.
            for (ts, _s, _p) in self._scoped.values():
                ts.setCurrentIndex(0)
            # Resolve the page by WIDGET, not the stored index — closing another
            # scoped tab on the same stack shifts indices (removeWidget), which
            # would make a stored page_idx stale.
            target_stack.setCurrentWidget(scoped_widget)
            self._show_permanent_page_keeping_tab(index)
        else:
            self._active_scoped = None
            for (ts, _s, _p) in self._scoped.values():
                ts.setCurrentIndex(0)

    def _show_permanent_page_keeping_tab(self, scoped_index: int):
        """Display the permanent full-UI page in the content area while leaving
        the tab BAR highlighting the scoped tab at *scoped_index*. Done by
        reaching into the QTabWidget's private QStackedWidget so the bar and the
        shown page can differ (Qt normally keeps them in lockstep)."""
        perm = self._permanent_widget
        if perm is None:
            return
        stack = self.findChild(QStackedWidget)
        if stack is not None:
            stack.setCurrentWidget(perm)

    # -- adding tabs --------------------------------------------------------
    def add_permanent(self, widget: QWidget, title: str) -> int:
        """Add a tab that can't be closed or detached (e.g. the Mods view)."""
        idx = self.addTab(widget, title)
        self._permanent.add(id(widget))
        self._permanent_widget = widget
        self._refresh_close_buttons()
        self._update_bar_visibility()
        return idx

    def open_scoped_tab(self, scoped_widget: QWidget, title: str,
                        target_stack, key: str | None = None):
        """Open a PANEL-SCOPED tab: it appears in the shared tab bar, but selecting
        it keeps the permanent full-UI page visible and shows *scoped_widget* in
        *target_stack* (one panel's QStackedWidget). The rest of the UI — the
        other panel, headers, footers — stays live and interactive.

        Re-opening the same *key* focuses the existing tab. Returns the tab's
        placeholder widget (the handle used as its identity)."""
        if key is not None and key in self._keys:
            self._focus(self._keys[key])
            return self._keys[key]
        # The tab page itself is an empty placeholder — the real content lives in
        # target_stack. addTab needs a widget; this 0-size stub never shows.
        placeholder = QWidget()
        page_idx = target_stack.addWidget(scoped_widget)
        self._scoped[id(placeholder)] = (target_stack, scoped_widget, page_idx)
        idx = self.addTab(placeholder, title)
        if key is not None:
            self._keys[key] = placeholder
            placeholder.setProperty("_tab_key", key)
        self.setCurrentIndex(idx)
        self._update_bar_visibility()
        return placeholder

    def open_tab(self, widget: QWidget, title: str, key: str | None = None):
        """Open *widget* as a tab and focus it. If *key* is already open
        (tab or float), focus that instead of adding a duplicate."""
        if key is not None and key in self._keys:
            existing = self._keys[key]
            self._focus(existing)
            return existing
        idx = self.addTab(widget, title)
        if key is not None:
            self._keys[key] = widget
            widget.setProperty("_tab_key", key)
        self.setCurrentIndex(idx)
        self._update_bar_visibility()
        return widget

    def close_tab(self, key: str):
        """Close the tab/float registered under *key* (no-op if not open)."""
        widget = self._keys.get(key)
        if widget is None:
            for flt in list(self._floats):
                if getattr(flt, "_tab_key", None) == key:
                    self._floats = [f for f in self._floats if f is not flt]
                    page = flt.take_page()      # release without redock
                    flt.close()
                    if page is not None:
                        page.deleteLater()
                    return
            return
        # Scoped tab? Route through the scoped teardown so the target panel
        # stack is reset to page 0 (a plain removeTab would strand it).
        if id(widget) in self._scoped:
            self._close_scoped(widget)
            return
        # Docked tab?
        idx = self.indexOf(widget)
        if idx != -1:
            self.removeTab(idx)
            self._forget(widget)
            widget.deleteLater()
            self._update_bar_visibility()
            return
        # Otherwise a floating window — dismiss it for real (don't redock).
        for flt in list(self._floats):
            if flt._page is widget:
                self._floats = [f for f in self._floats if f is not flt]
                self._forget(widget)
                page = flt.take_page()      # release without redock
                flt.close()
                if page is not None:
                    page.deleteLater()
                return

    # -- close / detach -----------------------------------------------------
    def _on_close_requested(self, index: int):
        w = self.widget(index)
        if id(w) in self._permanent:
            return
        if id(w) in self._scoped:
            self._close_scoped(w)
            return
        self.removeTab(index)
        self._forget(w)
        w.deleteLater()
        self._update_bar_visibility()

    def _close_scoped(self, placeholder: QWidget):
        """Tear down a panel-scoped tab: remove its widget from the target stack,
        reset that stack to page 0, drop the tab, and show the permanent page."""
        target_stack, scoped_widget, _idx = self._scoped.pop(id(placeholder))
        target_stack.setCurrentIndex(0)
        target_stack.removeWidget(scoped_widget)
        scoped_widget.deleteLater()
        tab_idx = self.indexOf(placeholder)
        if tab_idx != -1:
            self.removeTab(tab_idx)
        self._forget(placeholder)
        placeholder.deleteLater()
        self._active_scoped = None
        # Snap to the permanent tab.
        perm = self._permanent_widget
        if perm is not None:
            pi = self.indexOf(perm)
            if pi != -1:
                self.setCurrentIndex(pi)
        self._update_bar_visibility()

    def _detach(self, index: int, drop_pos: QPoint):
        w = self.widget(index)
        if w is None or id(w) in self._permanent:
            return
        title = self.tabText(index)
        key = w.property("_tab_key")
        # SCOPED tab: its QTabWidget page is an empty placeholder — the REAL
        # content lives in a target panel stack. Float that real widget instead
        # (else the detached window is blank), and remember its scope so redock
        # puts it back into the panel stack.
        scoped = self._scoped.pop(id(w), None)
        if scoped is not None:
            target_stack, scoped_widget, _pi = scoped
            target_stack.setCurrentIndex(0)        # reveal the panel again
            target_stack.removeWidget(scoped_widget)
            self.removeTab(index)
            self._forget(w)
            w.deleteLater()                        # drop the placeholder
            flt = _FloatingTab(scoped_widget, title, self.window())
            flt._tab_key = key
            flt._scoped_target = target_stack       # mark for re-scope on redock
            scoped_widget.setProperty("_tab_key", key)
        else:
            self.removeTab(index)
            flt = _FloatingTab(w, title, self.window())
            flt._tab_key = key
            flt._scoped_target = None
        flt.redock_requested.connect(self._on_redock)
        flt.move(drop_pos)
        flt.show()
        flt.raise_()
        flt.activateWindow()
        self._floats.append(flt)
        self._update_bar_visibility()
        # Drag-back redock needs a reliable button-release probe (X11). On native
        # Wayland we can't read it, so the gesture would misfire — skip wiring it
        # there and rely on close-to-redock (the window's X button) instead.
        # (Re-test on a Wayland session; XWayland still reports xcb and works.)
        if _have_reliable_release_probe():
            # Connect only AFTER the window is placed + shown so the initial
            # programmatic move() doesn't trigger anything. `moved` updates the
            # indicator; `drag_finished` (title-bar release) also redocks.
            flt.moved.connect(self._on_float_moved)
            flt.drag_finished.connect(self._on_drag_finished)

    def _on_redock(self, page: QWidget, title: str):
        """A floating window closed → bring its page back as a tab."""
        self._set_drop_indicator(False)
        # Was this a SCOPED float? Re-scope it into its panel stack.
        target = None
        for f in self._floats:
            if f._page is page:
                target = getattr(f, "_scoped_target", None)
                break
        self._floats = [f for f in self._floats if f._page is not page]
        key = page.property("_tab_key")
        if target is not None:
            # Re-create the scoped tab (placeholder in the bar, widget in stack).
            placeholder = QWidget()
            page_idx = target.addWidget(page)
            self._scoped[id(placeholder)] = (target, page, page_idx)
            idx = self.addTab(placeholder, title)
            if key:
                self._keys[key] = placeholder
                placeholder.setProperty("_tab_key", key)
            self.setCurrentIndex(idx)
        else:
            idx = self.addTab(page, title)
            page.show()
            self.setCurrentIndex(idx)
        self._update_bar_visibility()

    def _redock_zone(self):
        """Global-coords rect where dropping a floating window redocks it."""
        bar = self.tabBar()
        if not bar.isVisible() and self.count() <= 1:
            # Tab bar hidden (only permanent tab) — top strip of the widget.
            tl = self.mapToGlobal(self.rect().topLeft())
            zone = self.rect().translated(tl)
            zone.setHeight(48)
            return zone
        tl = bar.mapToGlobal(bar.rect().topLeft())
        return bar.rect().translated(tl).adjusted(-24, -24, 24, 24)

    def _on_float_moved(self, flt):
        """A float's title bar is being dragged. Start (or keep) a poll that
        tracks the cursor + global button state: show the drop indicator while
        dragging over the redock zone, and redock on the release EDGE over the
        zone. On X11 the WM grabs the pointer during the drag (so the
        NonClientArea release event / live button state are unreliable mid-drag),
        but `mouseButtons()` reports released once the grab ends — which the poll
        catches."""
        self._drag_flt = flt
        timer = getattr(self, "_drag_poll", None)
        if timer is None:
            from PySide6.QtCore import QTimer
            timer = QTimer(self)
            timer.setInterval(40)
            timer.timeout.connect(self._poll_drag)
            self._drag_poll = timer
        if not timer.isActive():
            timer.start()

    def _poll_drag(self):
        from PySide6.QtGui import QCursor
        flt = getattr(self, "_drag_flt", None)
        if flt is None or flt not in self._floats:
            self._drag_poll.stop()
            self._set_drop_indicator(False)
            return
        over = self._redock_zone().contains(QCursor.pos())
        if _left_button_down():
            # Still dragging — keep the indicator in sync.
            self._set_drop_indicator(over)
            return
        # Button physically released → stop polling + redock if over the zone.
        self._drag_poll.stop()
        self._set_drop_indicator(False)
        if over:
            page = flt.take_page()
            if page is not None:
                self._on_redock(page, flt._title)
            flt.close()

    def _on_drag_finished(self, flt):
        """NonClientArea release (when the platform delivers it) — redock if over
        the zone. Harmless if it never fires (the poll handles X11)."""
        from PySide6.QtGui import QCursor
        if flt not in self._floats:
            return
        if self._redock_zone().contains(QCursor.pos()):
            self._set_drop_indicator(False)
            page = flt.take_page()
            if page is not None:
                self._on_redock(page, flt._title)
            flt.close()
            if getattr(self, "_drag_poll", None) is not None:
                self._drag_poll.stop()

    def _set_drop_indicator(self, show: bool):
        """Show/hide a translucent accent overlay marking the redock drop zone."""
        ind = getattr(self, "_drop_ind", None)
        if show:
            if ind is None:
                ind = QLabel(self)
                # Explicit context: pyside6-lupdate can't statically resolve
                # self's class here, so self.tr() would land in an empty ("")
                # context and never match at runtime. Pin it to the class name.
                ind.setText(QCoreApplication.translate(
                    "DetachableTabWidget", "Drop to redock"))
                ind.setAlignment(Qt.AlignCenter)
                from gui_qt.theme_qt import active_palette, _c
                acc = _c(active_palette(), "ACCENT")
                ind.setStyleSheet(
                    f"background: rgba(61,174,233,60); color: #fff;"
                    f" border: 2px dashed {acc}; border-radius: 6px;"
                    f" font-size: 14px; font-weight: 600;")
                self._drop_ind = ind
            bar = self.tabBar()
            if bar.isVisible():
                ind.setGeometry(0, 0, self.width(), bar.height() + 8)
            else:
                ind.setGeometry(0, 0, self.width(), 48)
            ind.show()
            ind.raise_()
        elif ind is not None:
            ind.hide()

    # -- helpers ------------------------------------------------------------
    def _focus(self, widget: QWidget):
        idx = self.indexOf(widget)
        if idx != -1:
            self.setCurrentIndex(idx)
        else:
            # It's floating — raise its window.
            for f in self._floats:
                if f._page is widget:
                    f.raise_(); f.activateWindow()
                    break

    def _forget(self, widget: QWidget):
        key = widget.property("_tab_key")
        if key and key in self._keys:
            del self._keys[key]

    # -- key-based helpers --------------------------------------------------
    def has_key(self, key: str) -> bool:
        if key in self._keys:
            return True
        # A detached SCOPED tab forgets its key from _keys but its float still
        # carries _tab_key — treat that as open so close_tab/re-open see it.
        return any(getattr(f, "_tab_key", None) == key for f in self._floats)

    def focus_key(self, key: str):
        w = self._keys.get(key)
        if w is not None:
            self._focus(w)

    def set_tab_title(self, key: str, title: str):
        w = self._keys.get(key)
        if w is None:
            return
        idx = self.indexOf(w)
        if idx != -1:
            self.setTabText(idx, title)

    def _refresh_close_buttons(self):
        """Permanent tabs show no close button."""
        for i in range(self.count()):
            if id(self.widget(i)) in self._permanent:
                self.tabBar().setTabButton(i, QTabBar.RightSide, None)
