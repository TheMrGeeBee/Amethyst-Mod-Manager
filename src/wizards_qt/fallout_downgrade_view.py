"""Fallout 3 Downgrade wizard — Qt port of wizards/fallout_downgrade.py.

Walks through downloading the Fallout Anniversary Patcher from Nexus,
locating the archive, extracting it into the game root, running Patcher.exe
via the game's own Proton prefix, and cleaning the extracted files back out
when finished (extract_archive returns created paths deepest-first, which is
exactly the cleanup order).
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

_NEXUS_URL = "https://www.nexusmods.com/fallout3/mods/24913"
_ARCHIVE_KEYWORDS = ["fallout", "anniversary", "patcher"]

_PG_DOWNLOAD, _PG_LOCATE, _PG_RUN = range(3)


class FalloutDowngradeView(WizardViewBase):
    """Downgrade Fallout 3 for script extender compatibility."""

    _done_enable_sig = Signal()

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=f"Downgrade Fallout 3 — {game.name}")
        self._game_root = game.get_game_path()
        self._extracted_paths: list[Path] = []

        self._done_enable_sig.connect(self._guard(
            lambda: self._done_btn.setEnabled(True)))

        self._stack.addWidget(self._build_manual_download_page(
            "Step 1: Download the Patcher",
            "To downgrade Fallout 3 you need the\n"
            "Fallout Anniversary Patcher from Nexus Mods.\n\n"
            "Click the button below to open the mod page,\n"
            "then download the main file.",
            _NEXUS_URL,
            lambda: self._goto_step(_PG_LOCATE),
            button_text="Open Nexus Mods Page"))
        self._stack.addWidget(self._build_locate_page(
            "Step 2: Locate the Archive", with_next=True))
        self._stack.addWidget(self._build_run_page(
            "Step 3: Extract & Run Patcher"))
        self._stack.setCurrentIndex(_PG_DOWNLOAD)

    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_LOCATE:
            self._enter_locate(
                _ARCHIVE_KEYWORDS,
                "Select the Fallout Anniversary Patcher archive",
                "Archive not found in Downloads.\n"
                "Make sure you downloaded the mod, then press Try Again,\n"
                "or use Browse to select it manually.",
                lambda _p: self._goto_step(_PG_RUN))
        elif idx == _PG_RUN:
            self._set_status(self._run_status,
                             self.tr("Extracting archive to game folder…"))
            threading.Thread(target=self._extract_and_run, daemon=True,
                             name="fo3-downgrade").start()

    # ---- worker: extract into game root + run Patcher.exe -----------------------
    def _extract_and_run(self):
        try:
            self._do_extract()
            self._do_run_patcher()
        except Exception as exc:
            safe_emit(self._run_status_sig, f"Error: {exc}", RED)
            self._log(f"Downgrade Wizard: {exc}")

    def _do_extract(self):
        from Utils.wizard_archives import extract_archive
        game_root = self._game_root
        if game_root is None:
            raise RuntimeError("Game path is not configured.")
        archive = self._archive_path
        if archive is None or not archive.is_file():
            raise RuntimeError("Archive not found.")
        safe_emit(self._run_status_sig,
                  "Extracting archive to game folder…", "")
        self._log(f"Downgrade Wizard: extracting {archive.name} → {game_root}")
        # extract_archive returns files then dirs deepest-first — kept for
        # the reverse-depth cleanup when the wizard closes.
        self._extracted_paths = extract_archive(archive, game_root)
        n = len([p for p in self._extracted_paths if p.is_file()])
        self._log(f"Downgrade Wizard: extracted {n} file(s).")

    def _do_run_patcher(self):
        import subprocess
        from Utils.exe_launch import get_game_prefix_env
        from Utils.steam_finder import proton_run_command

        game_root = self._game_root
        patcher_exe = next(
            (p for p in self._extracted_paths
             if p.is_file() and p.name.lower() == "patcher.exe"), None)
        if patcher_exe is None:
            patcher_exe = next(game_root.rglob("Patcher.exe"), None)
        if patcher_exe is None:
            raise RuntimeError(
                "Could not find Patcher.exe after extraction.\n"
                "Make sure you downloaded the correct mod.")

        safe_emit(self._run_status_sig,
                  f"Running {patcher_exe.name} via Proton…\n"
                  "This may take a moment.", "")
        self._log(f"Downgrade Wizard: running {patcher_exe} via Proton")

        result = get_game_prefix_env(
            self._game, log_fn=lambda m: self._log(f"Downgrade Wizard: {m}"),
            allow_runner_fallback=True)
        if result is None:
            raise RuntimeError("Could not determine Proton version for this game.")
        proton_script, _compat_data, env = result

        proc = subprocess.Popen(
            proton_run_command(proton_script, "run", str(patcher_exe), env=env),
            env=env,
            cwd=str(game_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        proc.wait()
        if proc.returncode != 0:
            stderr = (proc.stderr.read() or b"").decode(errors="replace").strip()
            self._log(f"Downgrade Wizard: Patcher exited with code "
                      f"{proc.returncode}: {stderr}")

        safe_emit(self._run_status_sig,
                  "Patcher has finished.\n\n"
                  "Click Done to clean up the extracted files and close.",
                  GREEN)
        safe_emit(self._done_enable_sig)
        self._log("Downgrade Wizard: patcher complete. Waiting for Done.")

    # ---- cleanup on close ---------------------------------------------------------
    def _finish(self):
        if self._closing:
            return
        self._cleanup_extracted()
        super()._finish()

    def _cleanup_extracted(self):
        """Remove every file and directory that was extracted into game root."""
        if not self._extracted_paths:
            return
        removed = 0
        for p in self._extracted_paths:
            try:
                if p.is_file() or p.is_symlink():
                    p.unlink()
                    removed += 1
                elif p.is_dir():
                    try:
                        p.rmdir()   # only when empty — files removed above
                        removed += 1
                    except OSError:
                        pass
            except Exception:
                pass
        self._extracted_paths.clear()
        if removed:
            self._log(f"Downgrade Wizard: removed {removed} extracted item(s) "
                      "from game root.")
