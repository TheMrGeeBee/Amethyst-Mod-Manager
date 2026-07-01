"""Right-click context menu for the Plugins panel.

Mirrors the Tk menu (gui/plugin_panel.py `_show_plugin_context_menu`, 4760-4935)
and follows the same show-vs-hide convention as the modlist menu
(gui_qt/modlist_menu.py): each item is SHOWN only when its Tk condition holds and
HIDDEN otherwise. The only greyed items are the ones still awaiting a Qt backend
(userlist / groups / cycles / BOS-SP / overlapping-plugins / LOOT links), and even
those appear only when their Tk show-condition passes.

Vanilla (base-game) plugins are always-on and can't be toggled — right-clicking a
vanilla-only selection shows NO menu (Tk parity: it filters to non-vanilla rows and
returns early if none remain).

Core items wired now: Enable / Disable (single + multi) and the ESL flag toggle
(single + multi). The rest are gated greyed stubs to fill in 1-by-1 later.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMenu
from PySide6.QtGui import QAction


def show_context_menu(view, global_pos, index):
    """Build + exec the plugins context menu for *index* at *global_pos*."""
    menu = build_context_menu(view, index)
    if menu is not None:
        menu.exec(global_pos)


def build_context_menu(view, index):
    """Construct (but don't exec) the context QMenu — split out so headless tests
    can inspect the actions. Returns None if there's no menu (e.g. vanilla-only)."""
    model = view.model()
    if not index.isValid():
        return None

    # Selected rows, filtered to non-vanilla ("toggleable") — Tk hides the whole
    # menu when nothing toggleable is selected.
    sel_rows = sorted({i.row() for i in view.selectionModel().selectedRows()
                       or view.selectionModel().selectedIndexes()})
    if not sel_rows:
        sel_rows = [index.row()]
    toggleable = [r for r in sel_rows
                  if 0 <= r < model.rowCount() and not model.row(r).vanilla]
    if not toggleable:
        return None
    multi = len(toggleable) > 1

    menu = QMenu(view)
    state = {"group_started": False, "any": False}

    def _connect(action, slot):
        # QAction.triggered emits a `checked` bool. If a slot captures data via a
        # default arg (e.g. `lambda ns=idxs:`), Qt passes `checked` positionally
        # and clobbers that default. Wrap so the bool is always swallowed.
        action.triggered.connect(lambda _checked=False, _s=slot: _s())

    def act(label, slot, enabled=True):
        a = QAction(label, menu)
        _connect(a, slot)
        a.setEnabled(enabled)
        menu.addAction(a)
        state["group_started"] = True
        state["any"] = True
        return a

    def stub(label):
        # Greyed-out placeholder for an action not yet wired.
        return act(label, lambda: None, enabled=False)

    def submenu(label, items, enabled=True):
        sub = QMenu(label, menu)
        sub.setEnabled(enabled)
        for text, slot in items:
            a = QAction(text, sub)
            _connect(a, slot)
            sub.addAction(a)
        menu.addMenu(sub)
        state["group_started"] = True
        state["any"] = True
        return sub

    def divider():
        if state["group_started"]:
            menu.addSeparator()
            state["group_started"] = False

    _build_plugin_menu(view, model, index.row(), toggleable, multi,
                       act, stub, submenu, divider)
    return menu if state["any"] else None


def _build_plugin_menu(view, model, row, toggleable, multi,
                       act, stub, submenu, divider):
    game = getattr(view, "game", None)

    # ---- Enable / Disable (always) ---------------------------------------
    if multi:
        n = len(toggleable)
        act(f"Enable selected ({n})",
            lambda: _set_enabled(view, toggleable, True))
        act(f"Disable selected ({n})",
            lambda: _set_enabled(view, toggleable, False))
    else:
        act("Enable plugin", lambda: _set_enabled(view, toggleable, True))
        act("Disable plugin", lambda: _set_enabled(view, toggleable, False))

    # ---- Disable — BOS/SkyPatcher patch replaces it (stub) ----------------
    # Tk: gated on _bos_sp_plugins detection. Qt has no BOS/SP backend yet, so
    # _bos_sp_kind()/_bos_sp_rows() return empty → hidden until that lands.
    if multi:
        bos_rows = _bos_sp_rows(view, toggleable)
        if bos_rows:
            stub(f"Disable {len(bos_rows)} BOS/SP-patched (safe to disable)")
    else:
        kind = _bos_sp_kind(view, model.row(row).name)
        if kind:
            label = {"bos": "BOS", "sp": "SkyPatcher",
                     "both": "BOS+SkyPatcher"}.get(kind, kind)
            stub(f"Disable — {label} patch replaces it")

    # ---- ESL flag toggle --------------------------------------------------
    if getattr(game, "supports_esl_flag", False):
        # Only .esp/.esm rows can toggle (.esl is always light by extension).
        esl_rows = [i for i in toggleable
                    if not model.row(i).name.lower().endswith(".esl")]
        if esl_rows:
            divider()
            _build_esl_items(view, model, esl_rows, multi, act, stub)

    # ---- userlist / groups / cycles (stubs — LOOT userlist.yaml) ----------
    # All gated on userlist backend not ported to Qt → predicates false → hidden.
    divider()
    if not multi:
        name = model.row(row).name
        if not _in_userlist(view, name):
            stub("Add to userlist…")
        stub("Add to group…")
        if _in_userlist(view, name):
            stub("Remove from userlist")
        if _in_cycle(view, name):
            stub("Show cycle…")
        elif _in_userlist(view, name):
            stub("Show userlist rules…")
    else:
        names = [model.row(i).name for i in toggleable]
        stub("Add selected to group…")
        if any(_in_userlist(view, n) for n in names):
            stub("Remove selected from userlist")

    # ---- Show overlapping plugins… (stub — gated on loot_sort_enabled) ----
    if not multi and getattr(game, "loot_sort_enabled", False):
        divider()
        stub("Show overlapping plugins…")

    # ---- LOOT masterlist location links (stub — _loot_info not in Qt) -----
    if not multi:
        for text in _loot_locations(view, model.row(row).name):
            stub(text)


def _build_esl_items(view, model, esl_rows, multi, act, stub):
    """ESL flag sub-items. Ports the Tk single/multi eligibility logic."""
    game = getattr(view, "game", None)
    game_type_attr = getattr(game, "loot_game_type", "") or ""
    paths = _plugin_paths(view)

    from Utils.plugin_parser import is_esl_flagged, check_esl_eligible

    def esl_state(i):
        p = paths.get(model.row(i).name.lower())
        flagged = bool(p and p.is_file() and is_esl_flagged(p))
        eligible = bool(p and p.is_file() and check_esl_eligible(p, game_type_attr))
        return p, flagged, eligible

    if not multi:
        i = esl_rows[0]
        p, flagged, eligible = esl_state(i)
        if flagged:
            act("Remove ESL flag (un-light)",
                lambda: _toggle_esl(view, [i], False))
        elif eligible:
            act("Mark as Light (ESL)",
                lambda: _toggle_esl(view, [i], True))
        else:
            # Present but greyed — matches Tk's disabled "not ESL-safe" entry.
            stub("Not ESL-safe (per LOOT — compact in xEdit first)")
        return

    # Multi.
    not_esl, already_esl, ineligible = [], [], 0
    for i in esl_rows:
        _p, flagged, eligible = esl_state(i)
        if flagged:
            already_esl.append(i)
        elif eligible:
            not_esl.append(i)
        else:
            ineligible += 1
    if not_esl:
        suffix = f" ({ineligible} ineligible skipped)" if ineligible else ""
        act(f"Mark selected as Light (ESL) ({len(not_esl)}){suffix}",
            lambda: _toggle_esl(view, not_esl, True))
    elif ineligible:
        stub(f"Mark as Light (ESL) — none eligible "
             f"({ineligible} need xEdit compact)")
    if already_esl:
        act(f"Remove ESL flag from selected ({len(already_esl)})",
            lambda: _toggle_esl(view, already_esl, False))


# ---- actions --------------------------------------------------------------
def _set_enabled(view, indices, enabled: bool):
    view.model().set_enabled(indices, enabled)
    cb = getattr(view, "on_plugins_changed", None)
    if callable(cb):
        cb()


def _toggle_esl(view, indices, enable: bool):
    """Port of Tk _toggle_esl_flag: skip .esl / unknown-path / ineligible rows,
    write the header flag, then refresh so the flag column repaints."""
    from Utils.plugin_parser import set_esl_flag, check_esl_eligible
    model = view.model()
    game = getattr(view, "game", None)
    game_type_attr = getattr(game, "loot_game_type", "") or ""
    paths = _plugin_paths(view)
    changed = 0
    for i in indices:
        if not (0 <= i < model.rowCount()):
            continue
        name = model.row(i).name
        if name.lower().endswith(".esl"):
            continue
        p = paths.get(name.lower())
        if p is None or not p.is_file():
            continue
        if enable and not check_esl_eligible(p, game_type_attr):
            continue
        if set_esl_flag(p, enable):
            changed += 1
    if changed:
        cb = getattr(view, "on_plugins_changed", None)
        if callable(cb):
            cb()   # re-reads headers → ESL bit + stats + banner refresh


# ---- helpers / predicates -------------------------------------------------
def _plugin_paths(view) -> dict:
    """{plugin name (lower) → on-disk Path} for the active game (staging mod /
    overwrite / vanilla Data). Reuses the same resolver the Flags column uses."""
    game = getattr(view, "game", None)
    if game is None:
        return {}
    try:
        from gui_qt.plugin_state import resolve_plugin_paths_for_game
        return resolve_plugin_paths_for_game(game)
    except Exception:
        return {}


# The following predicates gate the greyed stubs. They return empty/false until
# their Tk backend is ported to Qt (userlist.yaml overlays, BOS/SP detection,
# LOOT masterlist cache). Wiring an item later = fill in the predicate + swap the
# stub() for act().
def _bos_sp_kind(view, name: str) -> str:
    return ""


def _bos_sp_rows(view, indices) -> list:
    return []


def _in_userlist(view, name: str) -> bool:
    return False


def _in_cycle(view, name: str) -> bool:
    return False


def _loot_locations(view, name: str) -> list:
    return []
