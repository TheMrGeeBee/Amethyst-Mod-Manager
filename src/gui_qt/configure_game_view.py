"""Configure-Game view — Qt port of gui/add_game_dialog.ReconfigureGamePanel.

Opens as a (detachable) tab. Reads/writes LIVE game config via the toolkit-neutral
backend setters/getters on BaseGame (set_game_path / set_prefix_path /
set_deploy_mode / set_staging_path / set_*; auto_deploy/archive_invalidation/
prefix_numbering attrs). Game-dependent options are gated by hasattr(), exactly
like the Tk panel. Auto-detection (Steam/Heroic) runs on a worker thread and is
delivered to the UI thread via Qt signals.

Handles both cases in one view: "Add" framing when the game is unconfigured,
"Reconfigure" + Remove/Clean when configured.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QFrame, QRadioButton, QCheckBox, QButtonGroup, QFileDialog,
    QMessageBox,
)

from gui_qt.theme_qt import active_palette, _c
from Utils.deploy import LinkMode


def _heroic_app_names(game) -> list[str]:
    names = list(getattr(game, "heroic_app_names", []) or [])
    if not names and getattr(game, "name", None):
        try:
            import json
            from Utils.config_paths import get_game_config_path
            p = get_game_config_path(game.name)
            if p.is_file():
                data = json.loads(p.read_text(encoding="utf-8"))
                saved = (data.get("heroic_app_name", "") or "").strip()
                if saved:
                    names = [saved]
        except Exception:
            pass
    return names


class _ScanSignals(QObject):
    game_found = Signal(object, str)        # (path|None, source)
    prefix_found = Signal(object)           # (path|None)


class ConfigureGameView(QWidget):
    """*on_done(saved: bool, removed: bool)* is called after Save/Remove so the
    window can refresh the game list and close the tab."""

    def __init__(self, game, on_done, parent=None):
        super().__init__(parent)
        self._game = game
        self._on_done = on_done or (lambda saved, removed: None)
        self._p = active_palette()

        self._found_path: Path | None = None
        self._found_prefix: Path | None = None
        self._custom_staging: Path | None = None

        self._sig = _ScanSignals()
        self._sig.game_found.connect(self._on_game_found)
        self._sig.prefix_found.connect(self._on_prefix_found)

        self._build()
        self._prepopulate()

    # ---- styling helpers --------------------------------------------------
    def _c(self, k):
        return _c(self._p, k)

    def _section_header(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"font-size:14px; font-weight:600; color:{self._c('TEXT_SEP')};")
        return lbl

    def _status(self, text: str, tone: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color:{self._c(tone)};")
        return lbl

    def _path_edit(self) -> QLineEdit:
        e = QLineEdit()
        e.setObjectName("PathEdit")
        f = QFont("monospace"); f.setStyleHint(QFont.Monospace); e.setFont(f)
        return e

    def _small_btn(self, text, slot) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("FormButton")
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(slot)
        return b

    # ---- build ------------------------------------------------------------
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        configured = self._game.is_configured()

        # Title bar.
        header = QWidget(); header.setObjectName("HeaderBar")
        hb = QHBoxLayout(header); hb.setContentsMargins(12, 8, 12, 8)
        verb = "Reconfigure" if configured else "Add"
        title = QLabel(f"{verb} Game — {self._game.name}")
        title.setStyleSheet("font-size:15px; font-weight:600;")
        hb.addWidget(title)
        hb.addStretch(1)
        active = getattr(self._game, "_active_profile_dir", None)
        if active is not None and active.name != "default":
            scope = f"Settings saved to profile: {active.name} (this profile only)"
        else:
            scope = "Editing shared settings (default profile)"
        scope_lbl = QLabel(scope)
        scope_lbl.setStyleSheet(f"color:{self._c('TEXT_WARN')};")
        hb.addWidget(scope_lbl)
        outer.addWidget(header)

        # Scrollable body.
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget(); body.setObjectName("FormBody")
        v = QVBoxLayout(body); v.setContentsMargins(16, 14, 16, 14); v.setSpacing(6)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # --- Game install folder ---
        v.addWidget(self._section_header("Game Installation Folder"))
        self._game_status = self._status("Scanning Steam libraries…", "TEXT_WARN")
        v.addWidget(self._game_status)
        self._game_edit = self._path_edit()
        self._game_edit.editingFinished.connect(self._on_game_typed)
        v.addWidget(self._game_edit)
        row = QHBoxLayout()
        row.addWidget(self._small_btn("Browse manually…", self._browse_game))
        self._game_open = self._small_btn("Open", lambda: self._open_path(self._found_path))
        row.addWidget(self._game_open)
        row.addWidget(self._small_btn("Scan", self._start_game_scan))
        row.addStretch(1)
        v.addLayout(row)
        v.addWidget(self._divider())

        # --- Proton prefix ---
        v.addWidget(self._section_header("Proton Prefix (compatdata/pfx)"))
        has_prefix_src = bool(getattr(self._game, "steam_id", None)
                              or _heroic_app_names(self._game))
        self._prefix_status = self._status(
            "Scanning for prefix…" if has_prefix_src
            else "No launcher ID — prefix not applicable.",
            "TEXT_WARN" if has_prefix_src else "TEXT_DIM")
        v.addWidget(self._prefix_status)
        self._prefix_edit = self._path_edit()
        self._prefix_edit.setEnabled(has_prefix_src)
        self._prefix_edit.editingFinished.connect(self._on_prefix_typed)
        v.addWidget(self._prefix_edit)
        row = QHBoxLayout()
        self._prefix_browse = self._small_btn("Browse manually…", self._browse_prefix)
        self._prefix_browse.setEnabled(has_prefix_src)
        row.addWidget(self._prefix_browse)
        self._prefix_open = self._small_btn("Open", lambda: self._open_path(self._found_prefix))
        row.addWidget(self._prefix_open)
        row.addStretch(1)
        v.addLayout(row)
        self._has_prefix_src = has_prefix_src
        v.addWidget(self._divider())

        # --- Mod staging folder ---
        v.addWidget(self._section_header("Mod Staging Folder"))
        self._staging_status = self._status("Default location will be used.", "TEXT_DIM")
        v.addWidget(self._staging_status)
        self._staging_edit = self._path_edit()
        self._staging_edit.editingFinished.connect(self._on_staging_typed)
        v.addWidget(self._staging_edit)
        row = QHBoxLayout()
        row.addWidget(self._small_btn("Browse manually…", self._browse_staging))
        row.addWidget(self._small_btn("Open", lambda: self._open_path(
            Path(self._staging_edit.text()) if self._staging_edit.text() else None)))
        row.addWidget(self._small_btn("Reset to default", self._reset_staging))
        row.addStretch(1)
        v.addLayout(row)
        v.addWidget(self._divider())

        # --- Deploy method + game-dependent options ---
        v.addWidget(self._section_header("Deploy Method"))
        rec = getattr(self._game, "default_deploy_mode", "symlink")
        drow = QHBoxLayout()
        self._deploy_group = QButtonGroup(self)
        self._rb_symlink = QRadioButton(
            "Symlink (Recommended)" if rec == "symlink" else "Symlink")
        self._rb_hardlink = QRadioButton(
            "Hardlink (Recommended)" if rec == "hardlink" else "Hardlink")
        self._deploy_group.addButton(self._rb_symlink)
        self._deploy_group.addButton(self._rb_hardlink)
        drow.addWidget(self._rb_symlink); drow.addWidget(self._rb_hardlink)
        drow.addStretch(1)
        v.addLayout(drow)

        # hasattr-gated option checkboxes (mirrors the Tk panel).
        self._opt_checks: dict[str, QCheckBox] = {}

        def add_check(key: str, text: str, gate: bool):
            if not gate:
                return
            cb = QCheckBox(text)
            cb.setStyleSheet("padding:2px 0;")
            v.addWidget(cb)
            self._opt_checks[key] = cb

        add_check("script_extender_swap",
                  "Swap launcher with script extender on deploy",
                  hasattr(self._game, "script_extender_swap"))
        add_check("auto_deploy",
                  "Auto deploy (deploy automatically on enable/disable/reorder)",
                  True)
        add_check("archive_invalidation",
                  "Automatic archive invalidation (prefer loose files over BSAs)",
                  hasattr(self._game, "archive_invalidation_enabled"))
        add_check("profile_ini_files",
                  "Use profile-specific INI files",
                  hasattr(self._game, "profile_ini_files"))
        add_check("profile_saves",
                  "Use profile-specific saves",
                  hasattr(self._game, "profile_saves")
                  and getattr(self._game, "supports_profile_saves", True))
        add_check("prefix_numbering",
                  "Prepend load-order numbers to mod folders",
                  hasattr(self._game, "prefix_numbering"))

        # BG3 patch-version radios.
        self._patch_group = None
        if hasattr(self._game, "get_patch_version"):
            v.addWidget(self._section_header("Game Patch Version"))
            prow = QHBoxLayout()
            self._patch_group = QButtonGroup(self)
            self._patch_buttons = {}
            for label, val in (("Patch 8", 8), ("Patch 7", 7), ("Patch 6", 6)):
                rb = QRadioButton(label)
                self._patch_group.addButton(rb)
                self._patch_buttons[val] = rb
                prow.addWidget(rb)
            prow.addStretch(1)
            v.addLayout(prow)

        v.addStretch(1)

        # --- Button bar ---
        bar = QWidget(); bar.setObjectName("BottomBar")
        bb = QHBoxLayout(bar); bb.setContentsMargins(12, 8, 12, 8)
        if configured:
            self._remove_btn = QPushButton("Remove Instance")
            self._remove_btn.setObjectName("DangerButton")
            self._remove_btn.setCursor(Qt.PointingHandCursor)
            self._remove_btn.clicked.connect(self._on_remove)
            bb.addWidget(self._remove_btn)
            self._clean_btn = QPushButton("Clean Game Folder")
            self._clean_btn.setObjectName("DangerButton")
            self._clean_btn.setCursor(Qt.PointingHandCursor)
            self._clean_btn.clicked.connect(self._on_clean)
            bb.addWidget(self._clean_btn)
        bb.addStretch(1)
        if configured:
            reset = self._small_btn("Reset Locations", self._reset_locations)
            bb.addWidget(reset)
        self._save_btn = QPushButton("Save")
        self._save_btn.setObjectName("PrimaryButton")
        self._save_btn.setCursor(Qt.PointingHandCursor)
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._on_save)
        bb.addWidget(self._save_btn)
        cancel = self._small_btn("Cancel", lambda: self._on_done(False, False))
        bb.addWidget(cancel)
        outer.addWidget(bar)

    def _divider(self) -> QFrame:
        f = QFrame(); f.setFrameShape(QFrame.HLine)
        f.setStyleSheet(f"color:{self._c('BORDER')};")
        return f

    # ---- prepopulate ------------------------------------------------------
    def _prepopulate(self):
        g = self._game
        if g.is_configured():
            gp = g.get_game_path()
            if gp:
                self._set_game(Path(gp), configured=True)
            pfx = g.get_prefix_path() if hasattr(g, "get_prefix_path") else None
            if pfx and Path(pfx).is_dir():
                self._set_prefix(Path(pfx), configured=True)
            elif self._has_prefix_src:
                self._start_prefix_scan()
            # deploy mode
            if hasattr(g, "get_deploy_mode"):
                mode = g.get_deploy_mode()
                if mode == LinkMode.HARDLINK:
                    self._rb_hardlink.setChecked(True)
                else:
                    self._rb_symlink.setChecked(True)
            else:
                self._rb_symlink.setChecked(True)
            # staging
            if getattr(g, "_staging_path", None) is not None:
                self._custom_staging = g._staging_path
                self._staging_edit.setText(str(g._staging_path))
                self._staging_status.setText("Custom staging folder configured.")
            else:
                self._staging_edit.setText(str(g.get_mod_staging_path()))
            # option values
            self._set_check("script_extender_swap",
                            getattr(g, "script_extender_swap", True))
            self._set_check("auto_deploy", getattr(g, "auto_deploy", False))
            self._set_check("archive_invalidation",
                            getattr(g, "archive_invalidation", True))
            self._set_check("profile_ini_files", getattr(g, "profile_ini_files", False))
            self._set_check("profile_saves", getattr(g, "profile_saves", False))
            self._set_check("prefix_numbering", getattr(g, "prefix_numbering", True))
            if self._patch_group is not None and hasattr(g, "get_patch_version"):
                rb = self._patch_buttons.get(int(g.get_patch_version()))
                if rb:
                    rb.setChecked(True)
            self._save_btn.setEnabled(True)
        else:
            self._rb_symlink.setChecked(rec_is_symlink := (
                getattr(g, "default_deploy_mode", "symlink") == "symlink"))
            if not rec_is_symlink:
                self._rb_hardlink.setChecked(True)
            self._set_check("auto_deploy", False)
            try:
                from Utils.ui_config import load_default_staging_path
                root = load_default_staging_path()
                if root:
                    self._staging_edit.setText(str(Path(root) / g.name))
                else:
                    self._staging_edit.setText(str(g.get_mod_staging_path()))
            except Exception:
                self._staging_edit.setText(str(g.get_mod_staging_path()))
            self._start_game_scan()

    def _set_check(self, key, value):
        cb = self._opt_checks.get(key)
        if cb is not None:
            cb.setChecked(bool(value))

    # ---- setters / status -------------------------------------------------
    def _set_game(self, path: Path, configured=False, source="steam"):
        self._found_path = path
        self._game_edit.setText(str(path))
        if configured:
            msg, tone = "Game already configured. You can update the path below.", "TEXT_OK"
        elif source == "heroic":
            msg, tone = "Found via Heroic Games Launcher.", "TEXT_OK"
        else:
            msg, tone = "Found via Steam libraries.", "TEXT_OK"
        self._game_status.setText(msg)
        self._game_status.setStyleSheet(f"color:{self._c(tone)};")
        self._save_btn.setEnabled(True)

    def _set_prefix(self, path: Path, configured=False):
        self._found_prefix = path
        self._prefix_edit.setText(str(path))
        msg = ("Prefix already configured. You can update the path below."
               if configured else "Found via Steam compatdata.")
        self._prefix_status.setText(msg)
        self._prefix_status.setStyleSheet(f"color:{self._c('TEXT_OK')};")

    # ---- typed-path handlers ----------------------------------------------
    def _on_game_typed(self):
        text = self._game_edit.text().strip()
        if text:
            self._found_path = Path(text)
            self._save_btn.setEnabled(True)

    def _on_prefix_typed(self):
        text = self._prefix_edit.text().strip()
        self._found_prefix = Path(text) if text else None

    def _on_staging_typed(self):
        text = self._staging_edit.text().strip()
        self._custom_staging = Path(text) if text else None

    # ---- browse / open ----------------------------------------------------
    def _browse_game(self):
        d = QFileDialog.getExistingDirectory(self, "Select game install folder",
                                             self._game_edit.text() or str(Path.home()))
        if d:
            self._set_game(Path(d), source="manual")

    def _browse_prefix(self):
        d = QFileDialog.getExistingDirectory(self, "Select Proton/Wine prefix (pfx)",
                                             self._prefix_edit.text() or str(Path.home()))
        if d:
            self._set_prefix(Path(d))

    def _browse_staging(self):
        d = QFileDialog.getExistingDirectory(self, "Select mod staging folder",
                                             self._staging_edit.text() or str(Path.home()))
        if d:
            self._custom_staging = Path(d)
            self._staging_edit.setText(d)
            self._staging_status.setText("Custom staging folder selected.")
            self._staging_status.setStyleSheet(f"color:{self._c('TEXT_OK')};")

    def _open_path(self, path):
        if path and Path(path).exists():
            import subprocess
            try:
                subprocess.Popen(["xdg-open", str(path)])
            except Exception:
                pass

    def _reset_staging(self):
        self._custom_staging = None
        try:
            # Reset clears any custom override → default location.
            if hasattr(self._game, "_staging_path"):
                default = self._game.get_mod_staging_path()
            else:
                default = self._game.get_mod_staging_path()
        except Exception:
            default = self._game.get_mod_staging_path()
        self._staging_edit.setText(str(default))
        self._staging_status.setText("Default location will be used.")
        self._staging_status.setStyleSheet(f"color:{self._c('TEXT_DIM')};")

    def _reset_locations(self):
        self._found_path = None
        self._found_prefix = None
        self._game_edit.clear()
        self._prefix_edit.clear()
        self._start_game_scan()

    # ---- auto-detection (worker thread → signals) -------------------------
    def _start_game_scan(self):
        self._game_status.setText("Scanning Steam libraries…")
        self._game_status.setStyleSheet(f"color:{self._c('TEXT_WARN')};")
        threading.Thread(target=self._game_scan_worker, daemon=True).start()

    def _game_scan_worker(self):
        g = self._game
        found = None
        source = "steam"
        try:
            from Utils.steam_finder import (
                find_steam_libraries, find_game_by_steam_id, find_game_in_libraries)
            from Utils.heroic_finder import (
                find_heroic_game, find_heroic_game_info_by_exe)
            exe_names = [getattr(g, "exe_name", None)] + list(
                getattr(g, "exe_name_alts", []) or [])
            exe_names = [e for e in exe_names if e]
            for exe in exe_names:
                info = find_heroic_game_info_by_exe(exe)
                if info:
                    found, fpfx, _app = info
                    source = "heroic"
                    if fpfx is not None:
                        self._found_prefix = fpfx
                    break
            if not found and _heroic_app_names(g):
                found = find_heroic_game(_heroic_app_names(g))
                if found:
                    source = "heroic"
            if not found:
                libs = find_steam_libraries()
                sid = getattr(g, "steam_id", None)
                if sid:
                    for exe in exe_names:
                        found = find_game_by_steam_id(libs, sid, exe)
                        if found:
                            break
                if not found:
                    for exe in exe_names:
                        found = find_game_in_libraries(libs, exe)
                        if found:
                            break
        except Exception:
            found = None
        self._sig.game_found.emit(found, source)

    def _on_game_found(self, found, source):
        if found:
            self._set_game(Path(found), source=source)
            if self._found_prefix is not None:
                self._set_prefix(Path(self._found_prefix))
            elif self._has_prefix_src:
                self._start_prefix_scan()
        else:
            self._game_status.setText(
                "Not found automatically. Browse manually to locate the game folder.")
            self._game_status.setStyleSheet(f"color:{self._c('TEXT_ERR')};")

    def _start_prefix_scan(self):
        self._prefix_status.setText("Scanning for Proton prefix…")
        self._prefix_status.setStyleSheet(f"color:{self._c('TEXT_WARN')};")
        threading.Thread(target=self._prefix_scan_worker, daemon=True).start()

    def _prefix_scan_worker(self):
        g = self._game
        found = None
        try:
            from Utils.steam_finder import find_prefix
            from Utils.heroic_finder import find_heroic_prefix
            sid = getattr(g, "steam_id", None)
            ids = [sid] + [str(s) for s in getattr(g, "alt_steam_ids", []) or [] if s]
            for s in [x for x in ids if x]:
                found = find_prefix(s)
                if found:
                    break
            if not found and _heroic_app_names(g):
                found = find_heroic_prefix(_heroic_app_names(g))
        except Exception:
            found = None
        self._sig.prefix_found.emit(found)

    def _on_prefix_found(self, found):
        if found:
            self._set_prefix(Path(found))
        else:
            self._prefix_status.setText(
                "Prefix not found automatically. Not needed if game is Linux native.")
            self._prefix_status.setStyleSheet(f"color:{self._c('TEXT_WARN')};")

    # ---- save (live write) ------------------------------------------------
    def _on_save(self):
        g = self._game
        if self._found_path is None and self._game_edit.text().strip():
            self._found_path = Path(self._game_edit.text().strip())
        if self._found_path is None:
            self._game_status.setText("Set the game installation folder first.")
            self._game_status.setStyleSheet(f"color:{self._c('TEXT_ERR')};")
            return

        # Block path changes while deployed (would strand deployed files).
        if g.is_configured() and g.get_deploy_active():
            def _changed(old, new):
                if old and new:
                    try:
                        return Path(old).resolve() != Path(new).resolve()
                    except Exception:
                        return str(old) != str(new)
                return bool(old) != bool(new)
            if _changed(g.get_game_path(), self._found_path) or (
                    self._found_prefix is not None
                    and _changed(g.get_prefix_path(), self._found_prefix)):
                self._game_status.setText(
                    "Cannot change the game/prefix path while mods are deployed. "
                    "Restore the game first.")
                self._game_status.setStyleSheet(f"color:{self._c('TEXT_ERR')};")
                return

        mode = (LinkMode.HARDLINK if self._rb_hardlink.isChecked()
                else LinkMode.SYMLINK)

        # Persist via the backend setters (live write to paths.json / overrides).
        g.set_game_path(self._found_path)
        if self._found_prefix is not None:
            g.set_prefix_path(self._found_prefix)
        if hasattr(g, "set_deploy_mode"):
            g.set_deploy_mode(mode)
        if hasattr(g, "set_staging_path"):
            g.set_staging_path(self._custom_staging)
        if hasattr(g, "set_script_extender_swap") and "script_extender_swap" in self._opt_checks:
            g.set_script_extender_swap(self._opt_checks["script_extender_swap"].isChecked())
        if "auto_deploy" in self._opt_checks:
            g.auto_deploy = self._opt_checks["auto_deploy"].isChecked()
        if "archive_invalidation" in self._opt_checks:
            g.archive_invalidation = self._opt_checks["archive_invalidation"].isChecked()
        if hasattr(g, "set_profile_ini_files") and "profile_ini_files" in self._opt_checks:
            g.set_profile_ini_files(self._opt_checks["profile_ini_files"].isChecked())
        if hasattr(g, "set_profile_saves") and "profile_saves" in self._opt_checks:
            g.set_profile_saves(self._opt_checks["profile_saves"].isChecked())
        if hasattr(g, "prefix_numbering") and "prefix_numbering" in self._opt_checks:
            g.prefix_numbering = self._opt_checks["prefix_numbering"].isChecked()
        if hasattr(g, "set_patch_version") and self._patch_group is not None:
            for val, rb in self._patch_buttons.items():
                if rb.isChecked():
                    g.set_patch_version(val)
                    break

        # Ensure the profile structure exists (mods/profiles/overwrite + default).
        try:
            from gui.add_game_dialog import _create_profile_structure
            _create_profile_structure(g)
        except Exception as exc:
            print(f"[gui_qt] profile structure create failed: {exc}", flush=True)

        self._on_done(True, False)

    # ---- destructive actions ----------------------------------------------
    def _confirm(self, title, text) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(title)
        box.setText(text)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        return box.exec() == QMessageBox.Yes

    def _on_remove(self):
        g = self._game
        from Utils.config_paths import get_game_config_path
        profile_root = g.get_profile_root()
        paths_json = get_game_config_path(g.name)
        msg = (f"Remove the instance configuration for {g.name}?\n\n"
               "Deleted: game config + generated caches; the game is restored to "
               "vanilla.\nKept: your mods, profiles, and overwrite folders.\n\n"
               "This cannot be undone.")
        if not self._confirm(f"Remove Instance — {g.name}", msg):
            return
        try:
            if hasattr(g, "restore"):
                g.restore()
        except Exception:
            pass
        try:
            from Utils.deploy import restore_root_folder
            rf = profile_root / "Root_Folder"
            game_root = g.get_game_path()
            if rf.is_dir() and game_root:
                restore_root_folder(
                    rf, game_root,
                    data_deploy_dirs=(g.root_restore_protect_dirs()
                                      if hasattr(g, "root_restore_protect_dirs") else None))
        except Exception:
            pass
        keep = {"mods", "profiles", "overwrite"}
        if profile_root.is_dir():
            for child in profile_root.iterdir():
                if child.name in keep:
                    continue
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
        cfg_dir = paths_json.parent
        if cfg_dir.is_dir():
            shutil.rmtree(cfg_dir, ignore_errors=True)
        self._on_done(False, True)

    def _on_clean(self):
        g = self._game
        game_path = g.get_game_path()
        if not game_path:
            return
        target = game_path
        if hasattr(g, "get_mod_data_path"):
            dp = g.get_mod_data_path()
            if dp and dp != game_path:
                target = dp
        if not target or not Path(target).is_dir():
            return
        msg = (f"Scan {target} and remove leftover deployed mod files (hardlinks/"
               "symlinks/copies) that weren't restored?\n\nVanilla game files are "
               "kept. This cannot be undone.")
        if not self._confirm(f"Clean Game Folder — {g.name}", msg):
            return
        try:
            from Utils.deploy import remove_deployed_files, restore_filemap_from_root
            target = Path(target)
            removed = 0
            if hasattr(g, "get_effective_filemap_path"):
                try:
                    fm = g.get_effective_filemap_path()
                    removed += restore_filemap_from_root(fm, target, move_runtime_files=False)
                except Exception:
                    pass
            removed += remove_deployed_files(target)
            if hasattr(g, "post_clean_game_folder"):
                try:
                    g.post_clean_game_folder()
                except Exception:
                    pass
            self._game_status.setText(f"Clean complete — {removed} deployed file(s) removed.")
            self._game_status.setStyleSheet(f"color:{self._c('TEXT_OK')};")
        except Exception as exc:
            self._game_status.setText(f"Clean failed: {exc}")
            self._game_status.setStyleSheet(f"color:{self._c('TEXT_ERR')};")
