"""Shared shell + page builders for Qt wizard views.

Every wizard view repeats the same skeleton (header bar with ✕, a
QStackedWidget of step pages, worker→UI Signals guarded by `_closing`,
an idempotent `_finish` that optionally refreshes the modlist) and most of
the download-family wizards repeat the same three pages (manual-download →
locate-in-Downloads → extract).  This base captures that skeleton so each
view only writes its tool-specific pages and workers.

Standards baked in (see memory feedback notes):
  * every worker/callback emit goes through safe_emit;
  * the portal file-picker callback fires on a WORKER thread → marshalled
    via `_picked_sig`;
  * ✕ / Done / auto-close all land in the idempotent `_finish()`; late
    worker signals are dropped by the `_closing` guards;
  * `ctx.refresh_modlist` runs on the GUI thread only, and only when the
    view marked `_ran = True`;
  * no hardcoded font-size — headers use font-weight only.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QStackedWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c

if TYPE_CHECKING:
    from Games.base_game import BaseGame

GREEN = "#6bc76b"
RED = "#e06c6c"


class WizardViewBase(QWidget):
    """Skeleton for a wizard view: header bar + step stack + teardown."""

    # Generic worker→UI signals shared by the common pages. Subclasses add
    # their own as needed; connect everything through self._guard().
    _locate_status_sig = Signal(str, str)
    _extract_status_sig = Signal(str, str)
    _run_status_sig = Signal(str, str)
    _picked_sig = Signal(object)          # portal picker → UI thread
    _extract_done_sig = Signal(bool)
    _run_started_sig = Signal()           # tool launched → enable Done
    _run_finished_sig = Signal()          # worker chain complete → close

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 *, title: str):
        super().__init__()
        self._game = game
        self._log = log_fn or (lambda _m: None)
        self._on_close_cb = on_close or (lambda: None)
        self._ctx = ctx
        self._archive_path: Path | None = None
        self._ran = False          # set True when refresh-worthy work happened
        self._closing = False

        self._locate_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._locate_status, t, c)))
        self._extract_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._extract_status, t, c)))
        self._run_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._run_status, t, c)))
        self._picked_sig.connect(self._guard(self._on_picked))
        self._extract_done_sig.connect(self._guard(self._on_extract_done))
        self._run_started_sig.connect(self._guard(self._on_run_started))
        self._run_finished_sig.connect(self._guard(self._finish))

        p = active_palette()
        self._dim = f"color:{_c(p,'TEXT_DIM')};"
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8); hb.setSpacing(8)
        head = QLabel(title)
        head.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        hb.addWidget(head)
        hb.addStretch(1)
        close = QPushButton(self.tr("✕ Close"))
        close.setCursor(Qt.PointingHandCursor)
        close.setStyleSheet(
            "QPushButton{background:#6b3333; color:#fff; border:none;"
            " padding:5px 12px; border-radius:4px; font-weight:600;}"
            "QPushButton:hover{background:#8c4444;}")
        close.clicked.connect(self._finish)
        hb.addWidget(close)
        v.addWidget(bar)

        self._stack = QStackedWidget()
        v.addWidget(self._stack, 1)

    # ---- guard / teardown -----------------------------------------------------
    def _guard(self, fn):
        return lambda *a: None if self._closing else fn(*a)

    def _finish(self):
        # ✕, Done, and any auto-close all land here. Idempotent; in-flight
        # daemon workers finish harmlessly (late signals dropped by guards,
        # late emits dropped by safe_emit).
        if self._closing:
            return
        self._closing = True
        do_refresh = self._ran and getattr(self._ctx, "refresh_modlist", None)
        self._on_close_cb()
        if do_refresh:
            do_refresh()

    # ---- widget helpers ---------------------------------------------------------
    def _step_page(self, title: str) -> tuple[QWidget, QVBoxLayout]:
        p = active_palette()
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)
        head = QLabel(title)
        head.setAlignment(Qt.AlignHCenter)
        head.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:700;")
        lay.addWidget(head)
        return page, lay

    def _make_note(self, lay: QVBoxLayout, text: str) -> QLabel:
        note = QLabel(text)
        note.setAlignment(Qt.AlignHCenter)
        note.setWordWrap(True)
        note.setStyleSheet(self._dim)
        lay.addWidget(note)
        return note

    def _make_status(self, lay: QVBoxLayout) -> QLabel:
        lbl = QLabel("")
        lbl.setAlignment(Qt.AlignHCenter)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(self._dim)
        lay.addWidget(lbl)
        return lbl

    def _set_status(self, lbl: QLabel, text: str, color: str = ""):
        lbl.setStyleSheet(f"color:{color};" if color else self._dim)
        lbl.setText(text)

    def _accent_btn(self, text: str) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(
            "QPushButton{background:#2d6a9e; color:#fff; border:none;"
            " padding:8px 24px; border-radius:4px; font-weight:600;}"
            "QPushButton:hover{background:#3a7fb8;}"
            "QPushButton:disabled{background:#44484f; color:#9aa0a6;}")
        return b

    def _green_btn(self, text: str = "Done") -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(
            "QPushButton{background:#2d7a2d; color:#fff; border:none;"
            " padding:8px 24px; border-radius:4px; font-weight:600;}"
            "QPushButton:hover{background:#3a9e3a;}"
            "QPushButton:disabled{background:#44484f; color:#9aa0a6;}")
        return b

    def _orange_btn(self, text: str) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(
            "QPushButton{background:#da8e35; color:#fff; border:none;"
            " padding:8px 24px; border-radius:4px; font-weight:600;}"
            "QPushButton:hover{background:#e5a04a;}")
        return b

    # ---- common page: manual download -------------------------------------------
    def _build_manual_download_page(self, heading: str, note: str,
                                    url: str, on_next,
                                    button_text: str = "Open Download Page",
                                    next_text: str = "Next →") -> QWidget:
        page, lay = self._step_page(heading)
        self._make_note(lay, note)
        lay.addSpacing(8)
        open_btn = self._orange_btn(button_text)
        open_btn.clicked.connect(lambda: self._open_url(url))
        lay.addWidget(open_btn, 0, Qt.AlignHCenter)
        lay.addStretch(1)
        nxt = self._accent_btn(next_text)
        nxt.clicked.connect(on_next)
        lay.addWidget(nxt, 0, Qt.AlignHCenter)
        return page

    def _open_url(self, url: str):
        from Utils.xdg import open_url
        open_url(url)

    # ---- common page: locate archive in ~/Downloads ------------------------------
    def _build_locate_page(self, heading: str = "Locate the Archive",
                           *, with_next: bool = False) -> QWidget:
        """Creates self._locate_status (+ optional gated self._locate_next_btn
        instead of auto-advance). Configure the scan with _enter_locate()."""
        page, lay = self._step_page(heading)
        self._locate_status = self._make_status(lay)
        lay.addStretch(1)
        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        browse = QPushButton(self.tr("Browse…"))
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(self._browse_archive)
        rh.addWidget(browse)
        retry = QPushButton(self.tr("Try Again"))
        retry.setCursor(Qt.PointingHandCursor)
        retry.clicked.connect(self._locate_rescan)
        rh.addWidget(retry)
        self._locate_next_btn = None
        if with_next:
            self._locate_next_btn = self._accent_btn(self.tr("Next →"))
            self._locate_next_btn.setEnabled(False)
            self._locate_next_btn.clicked.connect(
                lambda: self._locate_on_ready(self._archive_path))
            rh.addWidget(self._locate_next_btn)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _enter_locate(self, keywords: list[str], pick_title: str,
                      not_found_text: str, on_ready):
        """Call when routing to the locate page: scans Downloads for an
        archive matching all *keywords*; on_ready(path) fires (auto after
        300ms, or via Next when the page was built with_next=True)."""
        self._locate_keywords = keywords
        self._locate_pick_title = pick_title
        self._locate_not_found = not_found_text
        self._locate_on_ready = on_ready
        self._set_status(self._locate_status, self.tr("Searching Downloads folder…"))
        self._locate_rescan()

    def _locate_rescan(self):
        from Utils.wizard_archives import find_archive, get_downloads_dir
        found = find_archive(get_downloads_dir(), self._locate_keywords)
        if found:
            self._archive_found(found, f"Found: {found.name}")
        else:
            self._archive_path = None
            if self._locate_next_btn is not None:
                self._locate_next_btn.setEnabled(False)
            self._set_status(self._locate_status, self._locate_not_found, RED)

    def _browse_archive(self):
        from Utils.portal_filechooser import pick_file
        # Portal callback fires on a WORKER thread — marshal via Signal.
        pick_file(self._locate_pick_title,
                  lambda p: safe_emit(self._picked_sig, p))

    def _on_picked(self, path):
        if path and Path(path).is_file():
            self._archive_found(Path(path), f"Selected: {Path(path).name}")

    def _archive_found(self, path: Path, label: str):
        self._archive_path = path
        self._set_status(self._locate_status, label, GREEN)
        if self._locate_next_btn is not None:
            self._locate_next_btn.setEnabled(True)
        else:
            QTimer.singleShot(300, self._guard(
                lambda: self._locate_on_ready(path)))

    # ---- common page: extract status ---------------------------------------------
    def _build_extract_page(self, heading: str) -> QWidget:
        page, lay = self._step_page(heading)
        self._extract_status = self._make_status(lay)
        lay.addStretch(1)
        return page

    def _extract_to_applications(self, app_dir: str, exe_name: str,
                                 tool_label: str, *, marker: str = ""):
        """Worker: extract self._archive_path into Applications/<app_dir>,
        flatten wrappers until *exe_name* (or *marker* dir when given) is
        top-level, verify it exists and store the exe as self._exe.  Emits
        _extract_status_sig + _extract_done_sig.

        marker="" verifies the exe file; a *marker* (e.g. "tools") verifies a
        top-level directory instead (VRAMr/BENDr/ParallaxR ship a tools/ dir,
        not a launcher exe)."""
        archive, game = self._archive_path, self._game
        self._set_status(self._extract_status, self.tr("Extracting…"))

        def worker():
            import shutil
            from Utils.wizard_archives import extract_archive
            from Utils.xedit_tools import applications_dir, flatten_subdirs
            try:
                if archive is None or not archive.is_file():
                    raise RuntimeError("Archive not found.")
                dest = applications_dir(game, app_dir)
                dest.mkdir(parents=True, exist_ok=True)
                safe_emit(self._extract_status_sig,
                          f"Extracting {archive.name}…", "")
                self._log(f"{tool_label} Wizard: extracting {archive.name} → {dest}")
                paths = extract_archive(archive, dest)
                file_count = len([p for p in paths if p.is_file()])
                self._log(f"{tool_label} Wizard: extracted {file_count} file(s).")
                if marker:
                    # Collapse single-subdir wrappers until the marker dir is
                    # at the top level (ignoring loose files).
                    while True:
                        entries = [e for e in dest.iterdir()
                                   if e.name != "__MACOSX"]
                        subdirs = [e for e in entries if e.is_dir()]
                        if len(subdirs) == 1 and not (dest / marker).is_dir():
                            wrapper = subdirs[0]
                            tmp = dest.parent / (dest.name + "_flatten_tmp")
                            wrapper.rename(tmp)
                            for item in tmp.iterdir():
                                shutil.move(str(item), str(dest / item.name))
                            tmp.rmdir()
                        else:
                            break
                    if not (dest / marker).is_dir():
                        raise RuntimeError(
                            f"'{marker}' folder not found after extraction.\n"
                            f"Check that the archive contains {tool_label}.")
                    self._exe = dest / exe_name if exe_name else None
                else:
                    flatten_subdirs(dest, exe_name)
                    exe = dest / exe_name
                    if not exe.is_file():
                        raise RuntimeError(
                            f"{exe_name} not found after extraction.\n"
                            f"Check that the archive contains {exe_name}.")
                    self._exe = exe
                safe_emit(self._extract_status_sig,
                          f"Extracted {file_count} file(s).", GREEN)
                safe_emit(self._extract_done_sig, True)
            except Exception as exc:
                safe_emit(self._extract_status_sig, f"Error: {exc}", RED)
                self._log(f"{tool_label} Wizard: extract error: {exc}")
                safe_emit(self._extract_done_sig, False)

        threading.Thread(target=worker, daemon=True,
                         name="wizard-extract").start()

    # ---- common page: run (status + Done) -----------------------------------------
    def _build_run_page(self, heading: str) -> QWidget:
        page, lay = self._step_page(heading)
        self._run_status = self._make_status(lay)
        lay.addStretch(1)
        self._done_btn = self._green_btn(self.tr("Done"))
        self._done_btn.setEnabled(False)
        self._done_btn.clicked.connect(self._finish)
        lay.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        return page

    # ---- worker: GitHub latest-release → Applications/<app_dir> --------------------
    def _github_install_worker(self, api_url: str, keywords: list[str],
                               app_dir: str, exe_name: str, tool_label: str,
                               status_sig, done_sig, progress_sig=None):
        """Fetch the newest matching GitHub release asset, download it,
        extract into Applications/<app_dir>, flatten wrappers and verify
        *exe_name*; stores the exe as self._exe.  Emits status_sig(str,str),
        done_sig(bool) and optional progress_sig(int 0-100)."""
        game = self._game

        def worker():
            import tempfile
            from Utils.ca_bundle import download_file
            from Utils.wizard_archives import (
                extract_archive, fetch_latest_github_asset,
            )
            from Utils.xedit_tools import applications_dir, flatten_subdirs
            try:
                safe_emit(status_sig, "Fetching latest release from GitHub…", "")
                tag, dl_url = fetch_latest_github_asset(api_url, keywords)
                safe_emit(status_sig, f"Downloading {tag}…", "")
                self._log(f"{tool_label} Wizard: downloading {tag} from {dl_url}")

                suffix = Path(dl_url).suffix or ".7z"
                with tempfile.NamedTemporaryFile(suffix=suffix,
                                                 delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                hook = None
                if progress_sig is not None:
                    def hook(block_num, block_size, total_size):
                        if total_size > 0:
                            pct = min(100, block_num * block_size * 100 / total_size)
                            safe_emit(progress_sig, int(pct))
                download_file(dl_url, tmp_path, reporthook=hook)
                if progress_sig is not None:
                    safe_emit(progress_sig, 100)
                safe_emit(status_sig, "Extracting…", "")

                dest = applications_dir(game, app_dir)
                dest.mkdir(parents=True, exist_ok=True)
                paths = extract_archive(tmp_path, dest)
                tmp_path.unlink(missing_ok=True)

                file_count = len([p for p in paths if p.is_file()])
                flatten_subdirs(dest, exe_name)
                if not (dest / exe_name).is_file():
                    raise RuntimeError(f"{exe_name} not found after extraction.")
                self._exe = dest / exe_name

                self._log(f"{tool_label} Wizard: extracted {file_count} file(s).")
                safe_emit(status_sig, f"Downloaded and extracted {tag}.", GREEN)
                safe_emit(done_sig, True)
            except Exception as exc:
                safe_emit(status_sig, f"Error: {exc}", RED)
                self._log(f"{tool_label} Wizard: download error: {exc}")
                safe_emit(done_sig, False)

        threading.Thread(target=worker, daemon=True,
                         name="wizard-github-dl").start()

    # ---- common page: Proton step (built lazily on entry) --------------------------
    def _build_proton_holder(self) -> QWidget:
        self._proton_holder = QWidget()
        QVBoxLayout(self._proton_holder).setContentsMargins(0, 0, 0, 0)
        return self._proton_holder

    def _enter_proton(self, exe, exe_name: str, display_name: str, on_chosen,
                      *, allow_game_prefix: bool = True,
                      isolated_prefix_dir_fn=None,
                      title: str = "Choose Proton Version",
                      missing_text: str = ""):
        """(Re)build the Proton step on entry — the exe may only exist after
        an earlier extract step. on_chosen(proton_name, prefix_mode)."""
        lay = self._proton_holder.layout()
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if exe is None:
            err = QLabel(missing_text or
                         f"{exe_name} was not found.\nReopen this wizard.")
            err.setAlignment(Qt.AlignCenter)
            err.setWordWrap(True)
            err.setStyleSheet(f"color:{RED};")
            lay.addWidget(err)
            return
        from wizards_qt.proton_step import ProtonStepWidget
        lay.addWidget(ProtonStepWidget(
            self._game, exe, exe_name, display_name,
            on_continue=on_chosen,
            log_fn=self._log,
            allow_game_prefix=allow_game_prefix,
            isolated_prefix_dir_fn=isolated_prefix_dir_fn,
            title=title,
        ))

    # ---- deploy through the app machinery -------------------------------------------
    def _run_ctx_deploy(self, status_lbl: QLabel, on_ok, on_fail=None):
        """Start a deploy via ctx.run_deploy; done-callback fires on the UI
        thread. Returns False when no deploy hook is available."""
        run_deploy = getattr(self._ctx, "run_deploy", None)
        if run_deploy is None:
            self._set_status(status_lbl, self.tr("Deploy is unavailable here."), RED)
            return False
        self._set_status(status_lbl, self.tr("Deploying…"))

        def _done(ok: bool):
            if self._closing:
                return
            if ok:
                self._set_status(status_lbl, self.tr("Deploy complete."), GREEN)
                on_ok()
            else:
                self._set_status(status_lbl, self.tr("Deploy failed — see log."), RED)
                if on_fail is not None:
                    on_fail()

        if not run_deploy(_done):
            self._set_status(status_lbl, self.tr("Could not start deploy — see log."), RED)
            if on_fail is not None:
                on_fail()
            return False
        return True

    # ---- common page: deploy (explicit Deploy + Skip buttons) -----------------------
    def _build_deploy_page(self, heading: str, note: str, on_next) -> QWidget:
        """Deploy step with explicit Deploy/Skip buttons; on_next() fires on
        Skip or after a successful deploy. Creates self._deploy_status."""
        page, lay = self._step_page(heading)
        if note:
            self._make_note(lay, note)
        self._deploy_status = self._make_status(lay)
        lay.addStretch(1)
        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        self._deploy_skip_btn = QPushButton(self.tr("Skip"))
        self._deploy_skip_btn.setCursor(Qt.PointingHandCursor)
        self._deploy_skip_btn.clicked.connect(lambda: on_next())
        rh.addWidget(self._deploy_skip_btn)
        self._deploy_btn = self._accent_btn(self.tr("Deploy"))
        self._deploy_btn.clicked.connect(lambda: self._start_deploy(on_next))
        rh.addWidget(self._deploy_btn)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _start_deploy(self, on_next):
        self._deploy_btn.setEnabled(False)
        self._deploy_skip_btn.setEnabled(False)

        def _re_enable():
            self._deploy_btn.setEnabled(True)
            self._deploy_skip_btn.setEnabled(True)

        if not self._run_ctx_deploy(self._deploy_status, on_next, _re_enable):
            _re_enable()

    # ---- hooks (override in subclasses) --------------------------------------------
    def _on_extract_done(self, ok: bool):
        pass

    def _on_run_started(self):
        pass
