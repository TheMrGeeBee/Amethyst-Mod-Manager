"""Shared Qt view base for "download → locate → extract → run installer via
Proton → clean up" mod-loader wizards.

The Slime Rancher (SRML) and My Summer Car (MSCLoader) plugins follow the same
shape: the user manually downloads an archive from Nexus, the wizard extracts it
into the game folder, runs a bootstrap ``*Installer.exe`` inside the game's
Proton prefix, waits for the user to close it, then deletes the archive and the
extracted installer files.  A subclass only supplies the tool's constants (Nexus
URL, archive keywords, installer exe name) and may inject an extra pre-run step
(MSCLoader writes ``MSCFolder.txt``).

Ports of the Tk ``sr_srml`` / ``msc_mscloader`` plugins; mirrors the extract +
Proton-run pattern of ``Games/Morrowind/mcp_wizard_qt.py``.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal

from gui_qt.safe_emit import safe_emit
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame


class ModLoaderInstallerView(WizardViewBase):
    """Base view; subclasses set the class constants below."""

    # -- subclass configuration ---------------------------------------------------
    TOOL_LABEL: str = "Mod Loader"
    NEXUS_URL: str = ""
    ARCHIVE_KEYWORDS: list[str] = []
    INSTALLER_EXE: str = ""
    PICK_TITLE: str = "Select the archive"

    _extract_status_sig2 = Signal(str, str)
    _extract_next_sig = Signal()

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=f"Install {self.TOOL_LABEL} — {game.name}")
        self._game_root = game.get_game_path()
        self._extracted_paths: list[Path] = []

        self._extract_status_sig2.connect(self._guard(
            lambda t, c: self._set_status(self._extract_status, t, c)))
        self._extract_next_sig.connect(self._guard(
            lambda: self._extract_next_btn.setEnabled(True)))

        # Steps: download, locate, extract, [extra], run.  The extra step is
        # optional; subclasses that need it override _has_extra_step().
        self._steps: list[str] = ["download", "locate", "extract"]
        if self._has_extra_step():
            self._steps.append("extra")
        self._steps.append("run")

        self._stack.addWidget(self._build_manual_download_page(
            f"Step 1: Download {self.TOOL_LABEL}",
            f"Click the button below to open the {self.TOOL_LABEL}\n"
            "download page on Nexus Mods.\n\n"
            "Download the archive manually (do NOT use the Mod Manager\n"
            "download button), then click Next.",
            self.NEXUS_URL,
            lambda: self._goto_named("locate")))
        self._stack.addWidget(self._build_locate_page(
            "Step 2: Locate the Archive", with_next=True))

        # extract page (status + Next)
        page, lay = self._step_page(self.tr("Step 3: Extract to Game Folder"))
        self._extract_status = self._make_status(lay)
        lay.addStretch(1)
        self._extract_next_btn = self._accent_btn(self.tr("Next →"))
        self._extract_next_btn.setEnabled(False)
        self._extract_next_btn.clicked.connect(
            lambda: self._goto_named(self._after_extract_step()))
        lay.addWidget(self._extract_next_btn, 0, Qt.AlignHCenter)
        self._stack.addWidget(page)

        if self._has_extra_step():
            self._stack.addWidget(self._build_extra_page())

        self._stack.addWidget(self._build_run_page(
            f"Step {len(self._steps)}: Run {self.INSTALLER_EXE}"))

        self._stack.setCurrentIndex(self._idx("download"))

    # -- subclass hooks -----------------------------------------------------------
    def _has_extra_step(self) -> bool:
        return False

    def _build_extra_page(self):
        raise NotImplementedError

    def _run_extra_step(self):
        """Runs when entering the extra step; must advance to 'run' itself."""
        raise NotImplementedError

    def _extra_cleanup(self) -> int:
        """Extra files to remove on Done. Return count removed."""
        return 0

    # -- step routing -------------------------------------------------------------
    def _idx(self, name: str) -> int:
        return self._steps.index(name)

    def _after_extract_step(self) -> str:
        return "extra" if self._has_extra_step() else "run"

    def _goto_named(self, name: str):
        self._stack.setCurrentIndex(self._idx(name))
        if name == "locate":
            self._enter_locate(
                self.ARCHIVE_KEYWORDS, self.PICK_TITLE,
                f"{self.TOOL_LABEL} archive not found in Downloads.\n"
                "Make sure you downloaded it, then press Try Again,\n"
                "or use Browse to select it manually.",
                lambda _p: self._goto_named("extract"))
        elif name == "extract":
            self._set_status(self._extract_status,
                             self.tr("Extracting archive to game folder…"))
            threading.Thread(target=self._do_extract, daemon=True,
                             name="modloader-extract").start()
        elif name == "extra":
            self._run_extra_step()
        elif name == "run":
            self._set_status(self._run_status,
                             self.tr("Launching {0} via Proton…").format(self.INSTALLER_EXE))
            threading.Thread(target=self._do_run, daemon=True,
                             name="modloader-run").start()

    # -- extract ------------------------------------------------------------------
    def _do_extract(self):
        from Utils.wizard_archives import extract_archive
        try:
            if self._game_root is None:
                raise RuntimeError("Game path is not configured.")
            archive = self._archive_path
            if archive is None or not archive.is_file():
                raise RuntimeError("Archive not found.")

            self._log(f"{self.TOOL_LABEL} Wizard: extracting {archive.name} "
                      f"→ {self._game_root}")
            paths = extract_archive(archive, self._game_root)
            self._extracted_paths = paths
            file_count = len([p for p in paths if p.is_file()])
            self._log(f"{self.TOOL_LABEL} Wizard: extracted {file_count} file(s).")
            safe_emit(self._extract_status_sig2,
                      f"Extracted {file_count} file(s).\n\nClick Next to continue.",
                      GREEN)
        except Exception as exc:
            safe_emit(self._extract_status_sig2, f"Error: {exc}", RED)
            self._log(f"{self.TOOL_LABEL} Wizard extract error: {exc}")
        finally:
            safe_emit(self._extract_next_sig)

    # -- run installer ------------------------------------------------------------
    def _do_run(self):
        import subprocess
        from Utils.exe_launch import get_game_prefix_env
        from Utils.steam_finder import proton_run_command
        try:
            if self._game_root is None:
                raise RuntimeError("Game path is not configured.")
            exe = self._game_root / self.INSTALLER_EXE
            if not exe.is_file():
                raise RuntimeError(
                    f"{self.INSTALLER_EXE} was not found in the game folder.\n"
                    "Check that the archive extracted correctly.")

            result = get_game_prefix_env(
                self._game,
                log_fn=lambda m: self._log(f"{self.TOOL_LABEL} Wizard: {m}"),
                allow_runner_fallback=True)
            if result is None:
                raise RuntimeError(
                    "Could not find Proton — check that the prefix is configured.")
            proton_script, _compat_data, env = result

            self._log(f"{self.TOOL_LABEL} Wizard: launching {exe} via Proton")
            proc = subprocess.Popen(
                proton_run_command(proton_script, "run", str(exe), env=env),
                env=env, cwd=str(exe.parent),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            safe_emit(self._run_status_sig,
                      f"{self.INSTALLER_EXE} is running.\n"
                      "Close it when you are done, then click Done.", GREEN)
            safe_emit(self._run_started_sig)   # enable Done
            proc.wait()
            self._log(f"{self.TOOL_LABEL} Wizard: {self.INSTALLER_EXE} closed.")
            safe_emit(self._run_status_sig,
                      f"{self.INSTALLER_EXE} finished.\n\nClick Done to close.",
                      GREEN)
        except Exception as exc:
            safe_emit(self._run_status_sig, f"Error: {exc}", RED)
            self._log(f"{self.TOOL_LABEL} Wizard launch error: {exc}")
            safe_emit(self._run_started_sig)   # enable Done to close anyway

    def _on_run_started(self):
        self._ran = True
        self._done_btn.setEnabled(True)

    # -- Done → cleanup + close ---------------------------------------------------
    def _finish(self):
        if self._closing:
            return
        # Remove the downloaded archive + extracted installer files (the
        # installer is a temporary bootstrapper) before the base teardown.
        removed = 0
        if self._archive_path and self._archive_path.is_file():
            try:
                self._archive_path.unlink()
                removed += 1
            except OSError:
                pass
        for p in self._extracted_paths:
            try:
                if p.is_file():
                    p.unlink()
                    removed += 1
                elif p.is_dir() and not any(p.iterdir()):
                    p.rmdir()
                    removed += 1
            except OSError:
                pass
        removed += self._extra_cleanup()
        if removed:
            self._log(f"{self.TOOL_LABEL} Wizard: cleaned up {removed} file(s).")
        super()._finish()
