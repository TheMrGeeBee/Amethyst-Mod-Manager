"""Curated-profile wizard — installs a prebuilt .amethyst modlist hosted on the
GitHub ``Resources`` branch (e.g. "Install Viva New Vegas" for Fallout New
Vegas), reusing the normal Profile ▸ Import pipeline for the actual install.

Flow: intro (with an optional "also install Ultimate Edition ESM Fixes"
checkbox) → download the .amethyst into the curated-profiles cache and open
the Import tab via ctx.import_manifest → wait for the user to finish the
import there (the app switches to the new profile when it completes) →
optionally run the embedded ESM Fixes wizard into that new profile → done.

The ESM Fixes step must come AFTER the import: the curated profiles use
profile-specific mods, so the ESM Fixes output (registered into the ACTIVE
profile's effective mods dir) is only visible once the imported profile is
active. The step is parameterized (``esm_fixes_step`` in WizardTool.extra)
because its ~200 MB output cannot be bundled into the .amethyst.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QPushButton, QWidget

from gui_qt.safe_emit import safe_emit
from wizards_qt._view_base import RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_INTRO, _PG_FETCH, _PG_WAIT, _PG_ESM, _PG_DONE = range(5)


class CuratedProfileView(WizardViewBase):
    """Guided install of a prebuilt .amethyst profile from the Resources branch."""

    _fetch_status_sig = Signal(str, str)
    _fetch_done_sig = Signal(object)      # Path | None

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 *, profile_repo_path: str, display_name: str,
                 esm_fixes_step: bool = False, info_url: str = "", **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Install {0} — {1}").format(
                             display_name, game.name))
        self._repo_path = profile_repo_path
        self._display_name = display_name
        self._esm_fixes_step = esm_fixes_step
        self._info_url = info_url
        self._bundle_path: "Path | None" = None
        self._manifest: dict | None = None
        # Profile at open — the import completing switches the active profile,
        # which is how the Continue gate spots an unfinished import.
        self._profile_at_open = getattr(ctx, "profile_name", None) or "default"
        self._continue_warned = False
        self._esm_view = None

        self._fetch_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._fetch_status, t, c)))
        self._fetch_done_sig.connect(self._guard(self._on_fetch_done))

        self._stack.addWidget(self._build_intro_page())   # 0
        self._stack.addWidget(self._build_fetch_page())   # 1
        self._stack.addWidget(self._build_wait_page())    # 2
        self._stack.addWidget(QWidget())                  # 3 (ESM, built lazily)
        self._stack.addWidget(self._build_done_page())    # 4
        self._stack.setCurrentIndex(_PG_INTRO)

    # ---- page 0: intro + options --------------------------------------------------
    def _build_intro_page(self) -> QWidget:
        page, lay = self._step_page(
            self.tr("Install the {0} modlist").format(self._display_name))
        self._make_note(lay, (
            self.tr("This wizard downloads the curated '{0}' profile and opens "
                    "the profile importer, which installs the modlist into a "
                    "NEW profile.\n\nThe mods are downloaded from Nexus Mods — "
                    "log in first (Nexus ▸ Login to Nexus) if you haven't.")
            .format(self._display_name)))
        if self._info_url:
            guide = QPushButton(self.tr("Open guide website"))
            guide.setCursor(Qt.PointingHandCursor)
            guide.clicked.connect(lambda: self._open_url(self._info_url))
            lay.addWidget(guide, 0, Qt.AlignHCenter)
        self._esm_chk = None
        if self._esm_fixes_step:
            lay.addSpacing(8)
            self._esm_chk = QCheckBox(
                self.tr("Also install Ultimate Edition ESM Fixes (recommended)"))
            self._esm_chk.setChecked(True)
            self._esm_chk.setCursor(Qt.PointingHandCursor)
            lay.addWidget(self._esm_chk, 0, Qt.AlignHCenter)
            self._make_note(lay, (
                self.tr("Patches the vanilla .esm masters with community "
                        "bugfixes after the modlist is installed. It is too "
                        "large to bundle, so it runs as an extra step — needs "
                        "the 'Ultimate Edition ESM Fixes Remastered' download "
                        "from Nexus.")))
        lay.addStretch(1)
        start = self._accent_btn(self.tr("Start"))
        start.clicked.connect(self._start_fetch)
        lay.addWidget(start, 0, Qt.AlignHCenter)
        return page

    # ---- page 1: download the .amethyst --------------------------------------------
    def _build_fetch_page(self) -> QWidget:
        page, lay = self._step_page(
            self.tr("Step 1: Download the modlist profile"))
        self._make_note(lay, (
            self.tr("Downloading '{0}' from GitHub…").format(
                Path(self._repo_path).name)))
        self._fetch_status = self._make_status(lay)
        lay.addStretch(1)
        self._retry_btn = self._accent_btn(self.tr("Retry"))
        self._retry_btn.setVisible(False)
        self._retry_btn.clicked.connect(self._start_fetch)
        lay.addWidget(self._retry_btn, 0, Qt.AlignHCenter)
        return page

    def _start_fetch(self):
        self._stack.setCurrentIndex(_PG_FETCH)
        self._retry_btn.setVisible(False)
        self._set_status(self._fetch_status, self.tr("Contacting GitHub…"))
        repo_path = self._repo_path

        def worker():
            from Utils.curated_profiles import download_curated_profile
            _wlog = lambda m: self._log(f"Curated Profile Wizard: {m}")
            try:
                path = download_curated_profile(repo_path, log_fn=_wlog)
                safe_emit(self._fetch_done_sig, path)
            except Exception as exc:
                _wlog(f"download error: {exc}")
                safe_emit(self._fetch_status_sig,
                          self.tr("Download failed: {0}").format(exc), RED)
                safe_emit(self._fetch_done_sig, None)

        threading.Thread(target=worker, daemon=True,
                         name="curated-profile-fetch").start()

    def _on_fetch_done(self, path):
        if path is None:
            self._retry_btn.setVisible(True)
            return
        self._bundle_path = Path(path)
        try:
            from Utils.profile_export import read_manifest
            self._manifest = read_manifest(self._bundle_path)
        except Exception as exc:
            self._set_status(self._fetch_status,
                             self.tr("Could not read manifest: {0}").format(exc),
                             RED)
            self._retry_btn.setVisible(True)
            return
        self._ran = True
        self._open_import_tab()
        self._stack.setCurrentIndex(_PG_WAIT)

    def _open_import_tab(self):
        import_manifest = getattr(self._ctx, "import_manifest", None)
        if import_manifest is None or self._manifest is None:
            self._set_status(self._wait_status,
                             self.tr("Import is unavailable here."), RED)
            return
        # The app validates the game domain + Nexus login and opens the Import
        # tab (collection detail + install pipeline); it notifies on failure.
        import_manifest(self._manifest, self._bundle_path.stem,
                        str(self._bundle_path))

    # ---- page 2: wait for the import to finish --------------------------------------
    def _build_wait_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 2: Install the modlist"))
        self._make_note(lay, (
            self.tr("Finish the install in the Import tab: choose the profile "
                    "name and press Install. The mods are downloaded from "
                    "Nexus, which can take a while.\n\nWhen it completes, the "
                    "app switches to the new profile — then come back here and "
                    "press Continue.")))
        self._wait_status = self._make_status(lay)
        lay.addStretch(1)
        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 0, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        reopen = QPushButton(self.tr("Reopen import tab"))
        reopen.setCursor(Qt.PointingHandCursor)
        reopen.clicked.connect(self._open_import_tab)
        rh.addWidget(reopen)
        cont = self._accent_btn(self.tr("Continue"))
        cont.clicked.connect(self._on_wait_continue)
        rh.addWidget(cont)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _current_profile(self) -> str:
        cur = getattr(self._ctx, "current_profile", None)
        return cur() if cur is not None else self._profile_at_open

    def _on_wait_continue(self):
        if (self._current_profile() == self._profile_at_open
                and not self._continue_warned):
            self._continue_warned = True
            self._set_status(self._wait_status,
                             self.tr("The active profile hasn't changed — the "
                                     "import doesn't look finished. Complete it "
                                     "in the Import tab first, or press "
                                     "Continue again to proceed anyway."), RED)
            return
        if self._esm_chk is not None and self._esm_chk.isChecked():
            self._enter_esm_step()
        else:
            self._stack.setCurrentIndex(_PG_DONE)

    # ---- page 3: embedded ESM Fixes wizard ------------------------------------------
    def _enter_esm_step(self):
        # Built lazily so the embedded view captures the NEW profile (its ctor
        # syncs the game's active-profile context to ctx.profile_name).
        if self._esm_view is None:
            import dataclasses
            from wizards_qt.esm_fixes_view import ESMFixesView
            ctx = self._ctx
            if ctx is not None:
                ctx = dataclasses.replace(
                    ctx, profile_name=self._current_profile())
            self._esm_view = ESMFixesView(
                self._game, log_fn=self._log,
                on_close=lambda: self._guard(self._on_esm_done)(),
                ctx=ctx, show_header=False)
            old = self._stack.widget(_PG_ESM)
            self._stack.removeWidget(old)
            old.deleteLater()
            self._stack.insertWidget(_PG_ESM, self._esm_view)
        self._stack.setCurrentIndex(_PG_ESM)

    def _on_esm_done(self):
        self._stack.setCurrentIndex(_PG_DONE)

    # ---- page 4: done ----------------------------------------------------------------
    def _build_done_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("All done"))
        self._make_note(lay, (
            self.tr("The {0} profile is set up. Review the mod list, then "
                    "Deploy and play.").format(self._display_name)))
        lay.addStretch(1)
        done = self._green_btn(self.tr("Done"))
        done.clicked.connect(self._finish)
        lay.addWidget(done, 0, Qt.AlignHCenter)
        return page
