"""MGE XE wizard (Morrowind) — Qt port of Games/Morrowind/mgexe_wizard.py.

MGE XE bundles MWSE. Two install paths, auto-detected from the archive name:
  * Installer — archive contains MGEXE-<version>-installer.exe; extracted to
    the game root, then run via the game's Proton prefix.
  * Manual    — loose files (d3d8.dll, MGEXEgui.exe, mge3/, MWSE-Update.exe …)
    extracted straight into the game root; no exe launched.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal

from gui_qt.safe_emit import safe_emit
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_NEXUS_URL = "https://www.nexusmods.com/morrowind/mods/41102?tab=files&file_id=1000048202"
_KEYWORDS_COMMON = ["mge"]
_KEYWORDS_INSTALLER = ["mge", "installer"]
_INSTALLER_EXE_PREFIX = "MGEXE"

_PG_DOWNLOAD, _PG_LOCATE, _PG_INSTALL = range(3)


class MGEXEView(WizardViewBase):
    """Download and install MGE XE."""

    _install_status_sig = Signal(str, str)
    _install_done_sig = Signal()

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=f"Install MGE XE — {game.name}")
        self._game_root = game.get_game_path()
        self._is_installer = False

        self._install_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._install_status, t, c)))
        self._install_done_sig.connect(self._guard(
            lambda: self._done_btn.setEnabled(True)))

        self._stack.addWidget(self._build_manual_download_page(
            "Step 1: Download MGE XE",
            "Click the button below to open the MGE XE download page on Nexus "
            "Mods.\n\nDownload either the Installer or the Manual Install "
            "archive, then click Next.",
            _NEXUS_URL,
            lambda: self._goto_step(_PG_LOCATE)))
        self._stack.addWidget(self._build_locate_page(
            "Step 2: Locate the Archive", with_next=True))
        # page 2: install (status + Done)
        page, lay = self._step_page("Step 3: Install MGE XE")
        self._install_status = self._make_status(lay)
        lay.addStretch(1)
        self._done_btn = self._green_btn("Done")
        self._done_btn.setEnabled(False)
        self._done_btn.clicked.connect(self._finish)
        from PySide6.QtCore import Qt
        lay.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        self._stack.addWidget(page)
        self._stack.setCurrentIndex(_PG_DOWNLOAD)

    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_LOCATE:
            # Prefer the installer archive; fall back to manual. _enter_locate
            # only scans one keyword set, so pre-scan for the installer, then
            # fall back inside the ready callback via _archive_found labels.
            self._enter_locate(
                _KEYWORDS_COMMON, "Select the MGE XE archive",
                "MGE XE archive not found in Downloads.\n"
                "Make sure you downloaded it, then press Try Again,\n"
                "or use Browse to select it manually.",
                self._on_archive_ready)
        elif idx == _PG_INSTALL:
            self._set_status(self._install_status,
                             "Extracting archive to game folder…")
            threading.Thread(target=self._do_install, daemon=True,
                             name="mgexe-install").start()

    def _on_archive_ready(self, path):
        self._is_installer = "installer" in Path(path).name.lower()
        self._goto_step(_PG_INSTALL)

    def _do_install(self):
        from Utils.wizard_archives import extract_archive
        try:
            if self._game_root is None:
                raise RuntimeError("Game path is not configured.")
            archive = self._archive_path
            if archive is None or not archive.is_file():
                raise RuntimeError("Archive not found.")

            safe_emit(self._install_status_sig,
                      "Extracting archive to game folder…", "")
            self._log(f"MGE XE Wizard: extracting {archive.name} → {self._game_root}")
            paths = extract_archive(archive, self._game_root)
            file_count = len([p for p in paths if p.is_file()])
            self._log(f"MGE XE Wizard: extracted {file_count} file(s).")
            self._ran = True

            try:
                archive.unlink()
                self._log(f"MGE XE Wizard: deleted {archive.name} from Downloads.")
            except OSError as exc:
                self._log(f"MGE XE Wizard: could not delete archive: {exc}")

            if self._is_installer:
                installer_exe = next(
                    (p for p in self._game_root.iterdir()
                     if p.is_file()
                     and p.name.upper().startswith(_INSTALLER_EXE_PREFIX)
                     and p.suffix.lower() == ".exe"), None)
                if installer_exe is None:
                    raise RuntimeError(
                        "Installer exe not found in game folder after "
                        f"extraction.\nExpected a file starting with "
                        f"'{_INSTALLER_EXE_PREFIX}' (.exe).")
                safe_emit(self._install_status_sig,
                          f"Running {installer_exe.name} via Proton…\n"
                          "Follow the installer steps, then come back and "
                          "click Done.", "")
                self._run_exe(installer_exe)
                self._log("MGE XE Wizard: installer completed.")
                safe_emit(self._install_status_sig,
                          "MGE XE installer finished.\n\nClick Done to close.",
                          GREEN)
            else:
                safe_emit(self._install_status_sig,
                          "MGE XE installed successfully!\n"
                          f"{file_count} file(s) extracted to the game "
                          "folder.\n\nClick Done to close.", GREEN)
            safe_emit(self._install_done_sig)
        except Exception as exc:
            safe_emit(self._install_status_sig, f"Error: {exc}", RED)
            self._log(f"MGE XE Wizard error: {exc}")
            safe_emit(self._install_done_sig)

    def _run_exe(self, exe: Path):
        import subprocess
        from Utils.exe_launch import get_game_prefix_env
        from Utils.steam_finder import proton_run_command
        result = get_game_prefix_env(
            self._game, log_fn=lambda m: self._log(f"MGE XE Wizard: {m}"),
            allow_runner_fallback=True)
        if result is None:
            raise RuntimeError("Could not determine Proton version for this game.")
        proton_script, _compat_data, env = result
        self._log(f"MGE XE Wizard: launching {exe} via Proton")
        proc = subprocess.Popen(
            proton_run_command(proton_script, "run", str(exe), env=env),
            env=env,
            cwd=str(self._game_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        proc.wait()
        if proc.returncode != 0:
            stderr = (proc.stderr.read() or b"").decode(errors="replace").strip()
            raise RuntimeError(
                f"{exe.name} exited with code {proc.returncode}.\n{stderr}")
