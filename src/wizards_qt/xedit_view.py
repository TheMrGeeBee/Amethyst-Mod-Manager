"""xEdit wizard (SSEEdit / FO4Edit / FNVEdit / … + QuickAutoClean) — Qt port
of wizards/sseedit.py.

One parametrised view serves every Bethesda game via the WizardTool.extra
kwargs (xedit_exe / nexus_url / app_dir / display_name), with SSEEdit
defaults for Skyrim SE's plain registrations; ``qac=True`` runs the
QuickAutoClean build instead and shows the LOOT dirty-plugins list on the
run page.

Steps (plugins-panel-scoped tab; 1-3 are skipped when the exe is already
extracted under Profiles/<game>/Applications/<app_dir>/):
  1. Open the Nexus download page (manual download only).
  2. Locate the archive in ~/Downloads (Try Again / Browse).
  3. Extract to Applications/<app_dir>/ and flatten wrapper dirs.
  4. Deploy the modlist (auto-starts through QtWizardContext.run_deploy).
  5. Choose Proton version + prefix placement (shared ProtonStepWidget).
  6. Run <xEdit>.exe via Proton with -d:<game>/Data after prefix prep
     (registry seed, plugins.txt + My Games links, viewsettings seed, WinXP
     compat flag).  When the tool exits: wineserver shutdown → finalize
     pending ``<plugin>.save.<timestamp>`` renames → restore_after_xedit
     (full un-deploy so edited plugins land back in their mod folders) →
     close + modlist refresh.

All blocking work happens on daemon threads with Signals marshalling back to
the UI thread; the portal file picker's callback also fires on a worker
thread and is marshalled via Signal.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QStackedWidget,
)

from gui_qt.theme_qt import active_palette, _c
from gui_qt.safe_emit import safe_emit
from Utils.xedit_tools import tool_exe_path

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_GREEN = "#6bc76b"
_RED = "#e06c6c"

_NEXUS_URL = "https://www.nexusmods.com/skyrimspecialedition/mods/164?tab=files&file_id=495506"
_EXE_NAME = "SSEEdit.exe"

_PG_DOWNLOAD, _PG_LOCATE, _PG_EXTRACT, _PG_DEPLOY, _PG_PROTON, _PG_RUN = range(6)


class XEditView(QWidget):
    """Install (if needed), deploy and run an xEdit build for a Bethesda game."""

    _locate_status_sig = Signal(str, str)
    _extract_status_sig = Signal(str, str)
    _run_status_sig = Signal(str, str)
    _picked_sig = Signal(object)          # portal picker → UI thread
    _extract_done_sig = Signal(bool)
    _run_started_sig = Signal()           # xEdit launched → enable Done
    _run_finished_sig = Signal()          # xEdit exited + restore done → close

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 *, xedit_exe: str | None = None, nexus_url: str | None = None,
                 app_dir: str | None = None, display_name: str | None = None,
                 qac: bool = False, **_extra):
        super().__init__()
        self._game = game
        self._log = log_fn or (lambda _m: None)
        self._on_close_cb = on_close or (lambda: None)
        self._ctx = ctx
        self._qac = qac

        # Resolve per-game config from kwargs, falling back to SSEEdit
        # defaults (mirrors the Tk wizard's __init__).
        base_exe = xedit_exe or _EXE_NAME
        self._exe_name = (
            base_exe[: -len(".exe")] + "QuickAutoClean.exe"
            if qac and base_exe.lower().endswith(".exe")
            else base_exe
        )
        self._nexus_url = nexus_url or _NEXUS_URL
        self._app_dir = app_dir or base_exe.removesuffix(".exe")
        short = display_name or base_exe.removesuffix(".exe")
        self._xedit_name = short  # build name w/o QAC, e.g. "FO4Edit"
        self._name = short + (" QAC" if qac else "")

        self._exe = tool_exe_path(game, self._exe_name, self._app_dir)
        self._archive_path: Path | None = None
        self._proton_name = ""
        self._prefix_mode = ""
        self._ran = False
        self._closing = False

        def _guard(fn):
            return lambda *a: None if self._closing else fn(*a)

        self._locate_status_sig.connect(
            _guard(lambda t, c: self._set_status(self._locate_status, t, c)))
        self._extract_status_sig.connect(
            _guard(lambda t, c: self._set_status(self._extract_status, t, c)))
        self._run_status_sig.connect(
            _guard(lambda t, c: self._set_status(self._run_status, t, c)))
        self._picked_sig.connect(_guard(self._on_picked))
        self._extract_done_sig.connect(_guard(self._on_extract_done))
        self._run_started_sig.connect(_guard(self._on_run_started))
        self._run_finished_sig.connect(_guard(self._finish))

        self.setObjectName("XEditView")
        self._build()

    # ---- layout -------------------------------------------------------------
    def _build(self):
        p = active_palette()
        self._dim = f"color:{_c(p,'TEXT_DIM')};"
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8); hb.setSpacing(8)
        title = QLabel(self.tr("Run {0} — {1}").format(self._name, self._game.name))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        hb.addWidget(title)
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

        self._stack.addWidget(self._build_step_download())  # 0
        self._stack.addWidget(self._build_step_locate())    # 1
        self._stack.addWidget(self._build_step_extract())   # 2
        self._stack.addWidget(self._build_step_deploy())    # 3
        self._stack.addWidget(self._build_step_proton())    # 4
        self._stack.addWidget(self._build_step_run())       # 5

        # Already extracted → straight to the deploy step (Tk behaviour).
        if self._exe is not None:
            self._goto_step(_PG_DEPLOY)
        else:
            self._stack.setCurrentIndex(_PG_DOWNLOAD)

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

    def _make_status(self, lay: QVBoxLayout) -> QLabel:
        lbl = QLabel("")
        lbl.setAlignment(Qt.AlignHCenter)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(self._dim)
        lay.addWidget(lbl)
        return lbl

    def _set_status(self, lbl: QLabel, text: str, color: str):
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

    # ---- step 1: download -----------------------------------------------------
    def _build_step_download(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Download {0}").format(self._xedit_name))
        note = QLabel(
            self.tr('Click the button below to open the {0} page on Nexus Mods.\n\nDownload the archive manually (do NOT use the Mod Manager download button), then click Next.').format(self._xedit_name))
        note.setAlignment(Qt.AlignHCenter)
        note.setWordWrap(True)
        note.setStyleSheet(self._dim)
        lay.addWidget(note)
        lay.addSpacing(8)

        open_btn = QPushButton(self.tr("Open Download Page"))
        open_btn.setCursor(Qt.PointingHandCursor)
        open_btn.setStyleSheet(
            "QPushButton{background:#da8e35; color:#fff; border:none;"
            " padding:8px 24px; border-radius:4px; font-weight:600;}"
            "QPushButton:hover{background:#e5a04a;}")
        open_btn.clicked.connect(self._open_download_page)
        lay.addWidget(open_btn, 0, Qt.AlignHCenter)

        lay.addStretch(1)
        nxt = self._accent_btn(self.tr("Next →"))
        nxt.clicked.connect(lambda: self._goto_step(_PG_LOCATE))
        lay.addWidget(nxt, 0, Qt.AlignHCenter)
        return page

    def _open_download_page(self):
        from Utils.xdg import open_url
        open_url(self._nexus_url)

    # ---- step 2: locate ---------------------------------------------------------
    def _build_step_locate(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 2: Locate the Archive"))
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
        retry.clicked.connect(self._scan_downloads)
        rh.addWidget(retry)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _scan_downloads(self):
        from Utils.wizard_archives import find_archive, get_downloads_dir
        found = find_archive(get_downloads_dir(), [self._xedit_name.lower()])
        if found:
            self._archive_path = found
            self._set_status(self._locate_status, self.tr("Found: {0}").format(found.name), _GREEN)
            QTimer.singleShot(
                300, lambda: None if self._closing
                else self._goto_step(_PG_EXTRACT))
        else:
            self._archive_path = None
            self._set_status(
                self._locate_status,
                self.tr('{0} archive not found in Downloads.\nMake sure you downloaded it, then press Try Again,\nor use Browse to select it manually.').format(self._xedit_name),
                _RED)

    def _browse_archive(self):
        from Utils.portal_filechooser import pick_file
        # Portal callback fires on a WORKER thread — marshal via Signal.
        pick_file(f"Select the {self._xedit_name} archive",
                  lambda *a: safe_emit(self._picked_sig, *a))

    def _on_picked(self, path):
        if path and Path(path).is_file():
            self._archive_path = Path(path)
            self._set_status(self._locate_status,
                             self.tr("Selected: {0}").format(self._archive_path.name), _GREEN)
            QTimer.singleShot(
                300, lambda: None if self._closing
                else self._goto_step(_PG_EXTRACT))

    # ---- step 3: extract --------------------------------------------------------
    def _build_step_extract(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 3: Extract {0}").format(self._xedit_name))
        self._extract_status = self._make_status(lay)
        lay.addStretch(1)
        return page

    def _start_extract(self):
        archive, game = self._archive_path, self._game
        exe_name, app_dir = self._exe_name, self._app_dir

        def worker():
            from Utils.wizard_archives import extract_archive
            from Utils.xedit_tools import applications_dir, flatten_subdirs
            try:
                if archive is None or not archive.is_file():
                    raise RuntimeError("Archive not found.")
                dest = applications_dir(game, app_dir)
                dest.mkdir(parents=True, exist_ok=True)

                safe_emit(self._extract_status_sig, f"Extracting {archive.name}…", "")
                self._log(f"{self._name} Wizard: extracting {archive.name} → {dest}")

                paths = extract_archive(archive, dest)
                file_count = len([p for p in paths if p.is_file()])
                self._log(f"{self._name} Wizard: extracted {file_count} file(s).")

                flatten_subdirs(dest, exe_name)

                exe = dest / exe_name
                if not exe.is_file():
                    raise RuntimeError(
                        f"{exe_name} not found after extraction.\n"
                        f"Check that the archive contains {exe_name}.")
                self._exe = exe

                safe_emit(self._extract_status_sig,
                    f"Extracted {file_count} file(s).", _GREEN)
                safe_emit(self._extract_done_sig, True)
            except Exception as exc:
                safe_emit(self._extract_status_sig, f"Error: {exc}", _RED)
                self._log(f"{self._name} Wizard: extract error: {exc}")
                safe_emit(self._extract_done_sig, False)

        threading.Thread(target=worker, daemon=True,
                         name="xedit-extract").start()

    def _on_extract_done(self, ok: bool):
        if ok:
            self._goto_step(_PG_DEPLOY)

    # ---- step 4: deploy ---------------------------------------------------------
    def _build_step_deploy(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 4: Deploy Modlist"))
        self._deploy_status = self._make_status(lay)
        lay.addStretch(1)
        self._skip_btn = QPushButton(self.tr("Skip"))
        self._skip_btn.setCursor(Qt.PointingHandCursor)
        self._skip_btn.clicked.connect(lambda: self._goto_step(_PG_PROTON))
        lay.addWidget(self._skip_btn, 0, Qt.AlignHCenter)
        return page

    def _start_deploy(self):
        run_deploy = getattr(self._ctx, "run_deploy", None)
        if run_deploy is None:
            self._set_status(self._deploy_status,
                             self.tr("Deploy is unavailable here — Skip to continue."),
                             _RED)
            return
        self._set_status(self._deploy_status, self.tr("Deploying…"), "")

        def _done(ok: bool):
            # Fired on the UI thread by the app's deploy completion handler.
            if self._closing:
                return
            if ok:
                self._set_status(self._deploy_status, self.tr("Deploy complete."), _GREEN)
                self._goto_step(_PG_PROTON)
            else:
                self._set_status(self._deploy_status,
                                 self.tr("Deploy failed — see log."), _RED)

        if not run_deploy(_done):
            self._set_status(self._deploy_status,
                             self.tr("Could not start deploy — see log."), _RED)

    # ---- step 5: proton -----------------------------------------------------------
    def _build_step_proton(self) -> QWidget:
        self._proton_holder = QWidget()
        QVBoxLayout(self._proton_holder).setContentsMargins(0, 0, 0, 0)
        return self._proton_holder

    def _enter_proton(self):
        # Built lazily on entry: the exe may only exist after the extract step.
        lay = self._proton_holder.layout()
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if self._exe is None:
            err = QLabel(
                self.tr('{0} was not found.\nPlease restart the wizard and install {1} first.').format(self._exe_name, self._xedit_name))
            err.setAlignment(Qt.AlignCenter)
            err.setWordWrap(True)
            err.setStyleSheet(f"color:{_RED};")
            lay.addWidget(err)
            return
        from wizards_qt.proton_step import ProtonStepWidget
        lay.addWidget(ProtonStepWidget(
            self._game, self._exe, self._exe_name, self._name,
            on_continue=self._on_proton_chosen,
            log_fn=self._log,
            title="Step 5: Choose Proton Version",
        ))

    def _on_proton_chosen(self, proton_name: str, prefix_mode: str):
        self._proton_name = proton_name
        self._prefix_mode = prefix_mode
        self._goto_step(_PG_RUN)

    # ---- step 6: run ----------------------------------------------------------------
    def _build_step_run(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 6: Run {0}").format(self._name))
        self._run_page_lay = lay
        self._dirty_box_added = False
        lay.addStretch(1)
        self._run_status = self._make_status(lay)
        self._done_btn = QPushButton(self.tr("Done"))
        self._done_btn.setCursor(Qt.PointingHandCursor)
        self._done_btn.setEnabled(False)
        self._done_btn.setStyleSheet(
            "QPushButton{background:#2d7a2d; color:#fff; border:none;"
            " padding:8px 24px; border-radius:4px; font-weight:600;}"
            "QPushButton:hover{background:#3a9e3a;}"
            "QPushButton:disabled{background:#44484f; color:#9aa0a6;}")
        self._done_btn.clicked.connect(self._finish)
        lay.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        return page

    def _build_dirty_plugins_panel(self):
        """QAC only: list the LOOT-flagged dirty plugins above the run status
        so the user can see what needs cleaning without closing the wizard."""
        from Utils.xedit_tools import collect_dirty_plugins
        dirty = collect_dirty_plugins(self._game)
        if not dirty or self._dirty_box_added:
            return
        self._dirty_box_added = True
        p = active_palette()

        head = QLabel(self.tr("Plugins needing cleaning ({0}):").format(len(dirty)))
        head.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")

        inner = QWidget()
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(4, 2, 4, 2)
        iv.setSpacing(2)
        for name, summary in dirty:
            nm = QLabel(name)
            nm.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
            iv.addWidget(nm)
            sm = QLabel(summary)
            sm.setStyleSheet(self._dim)
            sm.setContentsMargins(12, 0, 0, 4)
            sm.setWordWrap(True)
            iv.addWidget(sm)
        iv.addStretch(1)

        box = QScrollArea()
        box.setWidgetResizable(True)
        box.setWidget(inner)
        box.setFrameShape(QScrollArea.NoFrame)

        # Insert between the header (0) and the stretch, so the list absorbs
        # the space above the run status / Done button.
        self._run_page_lay.insertWidget(1, head)
        self._run_page_lay.insertWidget(2, box, 1)

    def _start_run(self):
        exe, game = self._exe, self._game
        if exe is None:
            self._set_status(self._run_status,
                             self.tr("{0} was not found.").format(self._exe_name), _RED)
            return
        self._set_status(self._run_status, self.tr("Launching {0}…").format(self._name), "")
        name = self._name
        proton_name, prefix_mode = self._proton_name, self._prefix_mode

        def worker():
            import subprocess
            from Utils.exe_launch import (
                resolve_tool_prefix, shutdown_prefix_wineserver,
            )
            from Utils.steam_finder import proton_run_command
            from Utils.wine_paths import to_wine_path
            from Utils.xedit_tools import (
                finalize_xedit_saves, prepare_xedit_prefix, restore_after_xedit,
            )
            _wlog = lambda m: self._log(f"{name} Wizard: {m}")
            try:
                result = resolve_tool_prefix(
                    exe, game, proton_name, prefix_mode, log_fn=_wlog)
                if result is None:
                    safe_emit(self._run_status_sig,
                        f"Could not find Proton '{proton_name}' — "
                        "check that it is installed in Steam.", _RED)
                    return
                proton_script, compat_data, env = result

                game_path = game.get_game_path()
                if game_path is None:
                    safe_emit(self._run_status_sig, "Game path not configured.", _RED)
                    return

                pfx = compat_data / "pfx"
                data_arg = f'-d:{to_wine_path(game_path / "Data", pfx)}'

                # Registry seed + plugins.txt / My Games links + viewsettings
                # seed + WinXP compat flag (see Utils.xedit_tools).
                prepare_xedit_prefix(
                    game, compat_data, proton_script, env,
                    xedit_name=self._xedit_name, exe=exe, log_fn=_wlog)

                self._log(f"{name} Wizard: launching {exe} via Proton with {data_arg}")
                proc = subprocess.Popen(
                    proton_run_command(proton_script, "run", str(exe), data_arg),
                    env=env,
                    cwd=str(exe.parent),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                safe_emit(self._run_started_sig)
                proc.wait()

                shutdown_prefix_wineserver(proton_script, compat_data,
                                           log_fn=_wlog)

                # xEdit can write the cleaned plugin to a temp and queue the
                # rename "on shutdown"; the wineserver is down now, so
                # finalise any temp that slipped through before the restore.
                n = finalize_xedit_saves(game_path / "Data", log_fn=_wlog)
                if n:
                    self._log(f"{name} Wizard: finalised {n} pending xEdit save(s).")

                # Move any edited plugin back into its mod folder while the
                # modindex still knows it — BEFORE the close-refresh rescans
                # staging (the core QAC-plugins-land-in-overwrite fix).
                restore_after_xedit(game, name, log_fn=self._log)
                self._log(f"{name} Wizard: {name} closed.")

                safe_emit(self._run_status_sig, f"{name} finished.", _GREEN)
                safe_emit(self._run_finished_sig)
            except Exception as exc:
                safe_emit(self._run_status_sig, f"Launch error: {exc}", _RED)
                self._log(f"{name} Wizard: launch error: {exc}")

        threading.Thread(target=worker, daemon=True, name="xedit-run").start()

    def _on_run_started(self):
        self._ran = True
        self._set_status(
            self._run_status,
            self.tr('{0} is running.\nClose it when you are done, then click Done.').format(self._name),
            _GREEN)
        self._done_btn.setEnabled(True)

    # ---- shared -------------------------------------------------------------
    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_LOCATE:
            self._set_status(self._locate_status,
                             self.tr("Searching Downloads folder…"), "")
            self._scan_downloads()
        elif idx == _PG_EXTRACT:
            self._set_status(self._extract_status, self.tr("Extracting…"), "")
            self._start_extract()
        elif idx == _PG_DEPLOY:
            self._start_deploy()
        elif idx == _PG_PROTON:
            self._enter_proton()
        elif idx == _PG_RUN:
            if self._qac:
                self._build_dirty_plugins_panel()
            self._start_run()

    def _finish(self):
        # ✕, Done, and the auto-close after the tool exits all land here.
        # Idempotent; in-flight daemon workers finish harmlessly (their late
        # signals are dropped by the _closing guards).
        if self._closing:
            return
        self._closing = True
        do_refresh = self._ran and getattr(self._ctx, "refresh_modlist", None)
        self._on_close_cb()
        if do_refresh:
            # The post-run restore un-deployed the game and may have moved
            # cleaned plugins back into their mod folders — re-sync + reload
            # panels (mirrors the Tk wizard's _reload_mod_panel; the Qt
            # refresh path also rescans the mod index, which the QAC flow
            # depends on).
            do_refresh()
