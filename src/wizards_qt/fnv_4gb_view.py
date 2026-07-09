"""FNV 4GB Patch wizard — Qt port of wizards/fnv_4gb_patch.py.

Single page: hashes FalloutNV.exe (worker), reports its patch state, and
offers Apply (large-address-aware + NVSE-loader byte patches, original kept
as FalloutNV_backup.exe) / Restore Backup.  Patch core lives in
Utils/fnv4gb_tools.py (shared with the Tk wizard).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import button_qss
from wizards_qt._view_base import GREEN, RED, WizardViewBase
from Utils.fnv4gb_tools import BACKUP_NAME, EXE_NAME

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_AMBER = "#e0a06c"


class Fnv4GbView(WizardViewBase):
    """Apply or revert the FNV 4GB patch."""

    _exe_status_sig = Signal(str, str)
    _backup_status_sig = Signal(str, str)
    _buttons_sig = Signal(bool, bool)     # (apply enabled, restore enabled)
    _refresh_sig = Signal()               # worker → re-run the state scan

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=f"4GB Patch — {game.name}")
        self._game_root = game.get_game_path()
        self._busy = False

        self._exe_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._exe_status, t, c)))
        self._backup_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._backup_status, t, c)))
        self._buttons_sig.connect(self._guard(self._set_buttons))
        self._refresh_sig.connect(self._guard(self._refresh))

        self._stack.addWidget(self._build_page())
        self._refresh()

    def _build_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Fallout New Vegas 4GB Patch"))
        self._make_note(lay, (
            self.tr('Patches FalloutNV.exe so the game can use 4 GB of memory\nand loads NVSE automatically at startup.\n\nUnder Proton this mostly silences in-game warnings from mods\nthat check for the patch, but it is safe and recommended.\n\nThe original exe is kept as {0}.').format(BACKUP_NAME)))
        lay.addSpacing(8)
        self._exe_status = self._make_status(lay)
        self._backup_status = self._make_status(lay)
        lay.addStretch(1)

        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        self._restore_btn = QPushButton(self.tr("Restore Backup"))
        self._restore_btn.setCursor(Qt.PointingHandCursor)
        self._restore_btn.setEnabled(False)
        self._restore_btn.setStyleSheet(
            button_qss("BTN_WARN_ORANGE", padding="8px 20px"))
        self._restore_btn.clicked.connect(self._on_restore)
        rh.addWidget(self._restore_btn)
        self._apply_btn = self._accent_btn(self.tr("Apply 4GB Patch"))
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._on_apply)
        rh.addWidget(self._apply_btn)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _set_buttons(self, apply_ok: bool, restore_ok: bool):
        self._apply_btn.setEnabled(apply_ok)
        self._restore_btn.setEnabled(restore_ok)

    # ---- state refresh (worker re-hashes the exe) -----------------------------
    def _refresh(self):
        game_root = self._game_root
        if game_root is None or not game_root.is_dir():
            self._set_status(self._exe_status,
                             self.tr("Game path is not configured."), RED)
            return
        self._set_status(self._exe_status, self.tr("Checking {0}…").format(EXE_NAME))
        self._set_buttons(False, False)

        def worker():
            from Utils.fnv4gb_tools import inspect_exe
            try:
                info = inspect_exe(game_root)
            except Exception as exc:
                safe_emit(self._exe_status_sig, f"Error reading exe: {exc}", RED)
                return
            state = info["state"]
            if state == "missing":
                safe_emit(self._exe_status_sig,
                          f"{EXE_NAME} not found in the game folder.", RED)
            elif state == "patched":
                safe_emit(self._exe_status_sig,
                          f"{EXE_NAME} is already 4GB patched.", GREEN)
            elif state == "patchable":
                safe_emit(self._exe_status_sig,
                          f"Unpatched {EXE_NAME} detected ({info['variant']} "
                          "version) — ready to patch.", "")
            else:
                safe_emit(self._exe_status_sig,
                          f"Unrecognised {EXE_NAME} version.\n"
                          f"SHA-1: {info['hash']}\n"
                          "It may already be modified. Verify game files in "
                          "Steam/Heroic to get a clean exe, then try again.",
                          _AMBER)
            if info["backup_exists"]:
                safe_emit(self._backup_status_sig,
                          f"Backup found: {BACKUP_NAME}", "")
            else:
                safe_emit(self._backup_status_sig, "No backup present.", "")
            safe_emit(self._buttons_sig,
                      state == "patchable", info["backup_exists"])

        threading.Thread(target=worker, daemon=True, name="fnv4gb-scan").start()

    # ---- actions ----------------------------------------------------------------
    def _on_apply(self):
        if self._busy or self._game_root is None:
            return
        self._busy = True
        self._set_buttons(False, False)
        self._set_status(self._exe_status, self.tr("Patching {0}…").format(EXE_NAME))
        game_root = self._game_root

        def worker():
            from Utils.fnv4gb_tools import apply_4gb_patch
            try:
                variant = apply_4gb_patch(game_root)
                self._log(f"4GB patch wizard: patched {EXE_NAME} ({variant} "
                          f"version), original saved as {BACKUP_NAME}.")
            except Exception as exc:
                self._log(f"4GB patch wizard: patch failed: {exc}")
                safe_emit(self._exe_status_sig, f"Patch failed: {exc}", RED)
            finally:
                self._busy = False
                safe_emit(self._refresh_sig)

        threading.Thread(target=worker, daemon=True, name="fnv4gb-apply").start()

    def _on_restore(self):
        if self._busy or self._game_root is None:
            return
        self._busy = True
        self._set_buttons(False, False)
        self._set_status(self._exe_status, self.tr("Restoring original {0}…").format(EXE_NAME))
        game_root = self._game_root

        def worker():
            from Utils.fnv4gb_tools import restore_backup
            try:
                restore_backup(game_root)
                self._log(f"4GB patch wizard: restored {EXE_NAME} from {BACKUP_NAME}.")
            except Exception as exc:
                self._log(f"4GB patch wizard: restore failed: {exc}")
                safe_emit(self._exe_status_sig, f"Restore failed: {exc}", RED)
            finally:
                self._busy = False
                safe_emit(self._refresh_sig)

        threading.Thread(target=worker, daemon=True, name="fnv4gb-restore").start()
