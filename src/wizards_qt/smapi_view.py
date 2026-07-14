"""Qt view: Install SMAPI (mod loader) for Stardew Valley.

Downloads the latest SMAPI installer zip from GitHub, then extracts it and runs
"install on Linux.sh" in a terminal (all non-GUI logic in
``Utils.smapi_installer``).  Port of the Tk ``sdv_smapi`` plugin.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QProgressBar, QPushButton, QWidget

from gui_qt.safe_emit import safe_emit
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_DOWNLOAD, _PG_INSTALL = range(2)


class SmapiView(WizardViewBase):
    """Download + install SMAPI."""

    _dl_status_sig = Signal(str, str)
    _dl_progress_sig = Signal(int)          # 0-100, or -1 for indeterminate
    _dl_next_sig = Signal()
    _install_status_sig = Signal(str, str)
    _install_done_sig = Signal()

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Install SMAPI — {0}").format(game.name))

        self._dl_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._dl_status, t, c)))
        self._dl_progress_sig.connect(self._guard(self._on_dl_progress))
        self._dl_next_sig.connect(self._guard(
            lambda: self._dl_next_btn.setEnabled(True)))
        self._install_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._run_status, t, c)))
        self._install_done_sig.connect(self._guard(self._on_install_done))
        # portal picker (Browse) marshals through the base _picked_sig; we route
        # its result to _on_smapi_picked instead of the locate-page handler.
        self._picked_sig.disconnect()
        self._picked_sig.connect(self._guard(self._on_smapi_picked))

        self._stack.addWidget(self._build_download_page())
        self._stack.addWidget(self._build_install_page())
        self._stack.setCurrentIndex(_PG_DOWNLOAD)

        threading.Thread(target=self._do_fetch_and_download, daemon=True,
                         name="smapi-download").start()

    # ---- page 1: download -------------------------------------------------------
    def _build_download_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Download SMAPI"))
        self._dl_status = self._make_status(lay)
        self._set_status(self._dl_status,
                         self.tr("Checking for the latest SMAPI release…"))
        self._dl_bar = QProgressBar()
        self._dl_bar.setRange(0, 0)     # indeterminate until first progress
        lay.addWidget(self._dl_bar)
        self._make_note(lay,
                        self.tr("A terminal window will open to run the installer.\n"
                        "Follow its prompts, then press a key to close it."))
        lay.addStretch(1)
        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        browse = QPushButton(self.tr("Browse…"))
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(self._browse_smapi)
        rh.addWidget(browse)
        self._dl_next_btn = self._accent_btn(self.tr("Next →"))
        self._dl_next_btn.setEnabled(False)
        self._dl_next_btn.clicked.connect(lambda: self._goto_install())
        rh.addWidget(self._dl_next_btn)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _on_dl_progress(self, pct: int):
        if pct < 0:
            self._dl_bar.setRange(0, 0)
            return
        if self._dl_bar.maximum() == 0:
            self._dl_bar.setRange(0, 100)
        self._dl_bar.setValue(pct)

    def _do_fetch_and_download(self):
        from Utils.smapi_installer import (
            download_smapi, fetch_latest_smapi_asset,
        )
        from Utils.wizard_archives import get_downloads_dir
        try:
            safe_emit(self._dl_status_sig,
                      self.tr("Fetching latest SMAPI release from GitHub…"), "")
            tag, url = fetch_latest_smapi_asset()
            filename = url.split("/")[-1]
            dest = get_downloads_dir() / filename
            safe_emit(self._dl_status_sig,
                      self.tr("Downloading SMAPI {0}…").format(tag), "")
            self._log(f"SMAPI Wizard: downloading {url} → {dest}")

            def hook(block_num, block_size, total_size):
                if total_size > 0:
                    pct = min(100, block_num * block_size * 100 / total_size)
                    safe_emit(self._dl_progress_sig, int(pct))

            download_smapi(url, dest, reporthook=hook)
            safe_emit(self._dl_progress_sig, 100)
            self._archive_path = dest
            self._log(f"SMAPI Wizard: downloaded {filename}")
            safe_emit(self._dl_status_sig,
                      self.tr("Downloaded SMAPI {0}: {1}").format(tag, filename),
                      GREEN)
            safe_emit(self._dl_next_sig)
        except Exception as exc:
            self._log(f"SMAPI Wizard: download error: {exc}")
            safe_emit(self._dl_progress_sig, -1)
            safe_emit(self._dl_status_sig,
                      self.tr("Download failed: {0}\n\n"
                      "Use Browse to select a manually downloaded archive.")
                      .format(exc), RED)
            safe_emit(self._dl_next_sig)

    def _browse_smapi(self):
        from Utils.portal_filechooser import pick_file
        pick_file(self.tr("Select the SMAPI archive"),
                  lambda p: safe_emit(self._picked_sig, p))

    def _on_smapi_picked(self, path):
        if path and Path(path).is_file():
            self._archive_path = Path(path)
            self._set_status(self._dl_status,
                             self.tr("Selected: {0}").format(Path(path).name), GREEN)
            self._dl_next_btn.setEnabled(True)

    # ---- page 2: install --------------------------------------------------------
    def _build_install_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 2: Install SMAPI"))
        self._run_status = self._make_status(lay)
        lay.addStretch(1)
        self._done_btn = self._green_btn(self.tr("Done"))
        self._done_btn.setEnabled(False)
        self._done_btn.clicked.connect(self._finish)
        lay.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        return page

    def _goto_install(self):
        self._stack.setCurrentIndex(_PG_INSTALL)
        self._set_status(self._run_status, self.tr("Extracting SMAPI archive…"))
        threading.Thread(target=self._do_install, daemon=True,
                         name="smapi-install").start()

    def _do_install(self):
        from Utils.smapi_installer import run_smapi_installer
        try:
            safe_emit(self._install_status_sig,
                      self.tr("Launching the SMAPI installer in a terminal.\n\n"
                      "Follow the on-screen prompts, then press a key to close "
                      "the terminal and click Done here."), "")
            run_smapi_installer(self._archive_path, log_fn=self._log)
            safe_emit(self._install_status_sig,
                      self.tr("SMAPI installer finished.\n\n"
                      "If it completed successfully, SMAPI is now installed.\n"
                      "Click Done to close."), GREEN)
        except Exception as exc:
            safe_emit(self._install_status_sig,
                      self.tr("Error: {0}").format(exc), RED)
            self._log(f"SMAPI Wizard error: {exc}")
        finally:
            safe_emit(self._install_done_sig)

    def _on_install_done(self):
        self._ran = True
        self._done_btn.setEnabled(True)
