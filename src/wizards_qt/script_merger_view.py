"""Witcher 3 Script Merger wizard — Qt port of wizards/script_merger_tw3.py.

Deploy → manual Nexus download → locate (sm-fae archive) → extract to
Applications/ScriptMerger/ → choose Proton version/prefix (shared wizard
step) → install .NET 8 into the chosen prefix (dep-marked in
amethyst_deps.json) → run WitcherScriptMerger.exe.  On Done the game is
restored so merged files are rescued into staging, then the modlist
refreshes.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal

from gui_qt.safe_emit import safe_emit
from wizards_qt._view_base import GREEN, RED, WizardViewBase
from Utils.xedit_tools import tool_exe_path

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_NEXUS_URL = "https://www.nexusmods.com/witcher3/mods/8405?tab=files&file_id=59566"
_MERGER_EXE = "WitcherScriptMerger.exe"
_MERGER_DIR = "ScriptMerger"
# .NET 8 install runs through Utils.proton_tools.install_dotnet_runtime.

(_PG_DEPLOY, _PG_DOWNLOAD, _PG_LOCATE, _PG_EXTRACT, _PG_PROTON, _PG_NET8,
 _PG_RUN) = range(7)


class ScriptMergerView(WizardViewBase):
    """Deploy mods and run WitcherScriptMerger."""

    _net8_status_sig = Signal(str, str)
    _net8_done_sig = Signal(bool)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=f"Run Script Merger — {game.name}")
        self._proton_name = ""
        self._prefix_mode = ""
        self._prefix_env = None

        self._net8_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._net8_status, t, c)))
        self._net8_done_sig.connect(self._guard(self._on_net8_done))

        # page 0: deploy (auto-start + Skip — Tk parity)
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QPushButton
        page, lay = self._step_page("Step 1: Deploy Modlist")
        self._deploy_status = self._make_status(lay)
        lay.addStretch(1)
        skip = QPushButton("Skip")
        skip.setCursor(Qt.PointingHandCursor)
        skip.clicked.connect(self._advance_from_deploy)
        lay.addWidget(skip, 0, Qt.AlignHCenter)
        self._stack.addWidget(page)
        # page 1: manual download
        self._stack.addWidget(self._build_manual_download_page(
            "Step 2: Download Script Merger",
            "Click the button below to open the Script Merger download page\n"
            "on Nexus Mods, then download the archive.\n\n"
            "Once downloaded, click Next.",
            _NEXUS_URL,
            lambda: self._goto_step(_PG_LOCATE)))
        # page 2: locate
        self._stack.addWidget(self._build_locate_page(
            "Step 3: Locate the Archive"))
        # page 3: extract
        self._stack.addWidget(self._build_extract_page(
            "Step 4: Extract Script Merger"))
        # page 4: proton
        self._stack.addWidget(self._build_proton_holder())
        # page 5: .NET 8
        page, lay = self._step_page("Step 6: Install .NET 8")
        self._net8_status = self._make_status(lay)
        lay.addStretch(1)
        self._stack.addWidget(page)
        # page 6: run
        self._stack.addWidget(self._build_run_page("Step 7: Run Script Merger"))

        self._goto_step(_PG_DEPLOY)

    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_DEPLOY:
            self._run_ctx_deploy(self._deploy_status, self._advance_from_deploy)
        elif idx == _PG_LOCATE:
            self._enter_locate(
                ["sm-fae"], "Select the Script Merger archive",
                "Script Merger archive not found in Downloads.\n"
                "Make sure you downloaded it, then press Try Again,\n"
                "or use Browse to select it manually.",
                lambda _p: self._goto_step(_PG_EXTRACT))
        elif idx == _PG_EXTRACT:
            self._extract_to_applications(_MERGER_DIR, _MERGER_EXE,
                                          "Script Merger")
        elif idx == _PG_PROTON:
            self._exe = tool_exe_path(self._game, _MERGER_EXE, _MERGER_DIR)
            self._enter_proton(
                self._exe, _MERGER_EXE, "Script Merger",
                self._on_proton_chosen,
                title="Step 5: Choose Proton Version",
                missing_text=f"{_MERGER_EXE} was not found.\n"
                             "Please restart the wizard and install "
                             "Script Merger first.")
        elif idx == _PG_NET8:
            self._set_status(self._net8_status, "Checking .NET 8…")
            self._start_net8()
        elif idx == _PG_RUN:
            self._set_status(self._run_status,
                             "Launching WitcherScriptMerger…")
            self._start_run()

    def _advance_from_deploy(self):
        # Skip download step if WitcherScriptMerger.exe is already present.
        if tool_exe_path(self._game, _MERGER_EXE, _MERGER_DIR) is not None:
            self._goto_step(_PG_PROTON)
        else:
            self._goto_step(_PG_DOWNLOAD)

    def _on_extract_done(self, ok: bool):
        if ok:
            self._goto_step(_PG_PROTON)

    def _on_proton_chosen(self, proton_name: str, prefix_mode: str):
        self._proton_name = proton_name
        self._prefix_mode = prefix_mode
        self._goto_step(_PG_NET8)

    # ---- .NET 8 into the chosen tool prefix ------------------------------------------
    def _start_net8(self):
        game = self._game
        exe = self._exe
        proton_name, prefix_mode = self._proton_name, self._prefix_mode

        def worker():
            from Utils.exe_launch import resolve_tool_prefix
            from Utils.protontricks import dotnet_dep_key, is_dep_installed
            _wlog = lambda m: self._log(f"Script Merger Wizard: {m}")
            try:
                safe_emit(self._net8_status_sig,
                          "Preparing Script Merger's Wine prefix…", "")
                result = resolve_tool_prefix(
                    exe, game, proton_name, prefix_mode, log_fn=_wlog)
                if result is None:
                    safe_emit(self._net8_status_sig,
                              f"Could not find Proton '{proton_name}' — check "
                              "that it is installed in Steam, then reopen this "
                              "wizard.", RED)
                    safe_emit(self._net8_done_sig, False)
                    return
                self._prefix_env = result
                proton_script, compat_data, env = result
                prefix_path = compat_data / "pfx"

                net8_key = dotnet_dep_key("8")
                if is_dep_installed(prefix_path, net8_key):
                    safe_emit(self._net8_status_sig,
                              ".NET 8 already installed — skipping.", GREEN)
                    safe_emit(self._net8_done_sig, True)
                    return

                from Utils.proton_tools import install_dotnet_runtime
                ok = install_dotnet_runtime(
                    "8", proton_script, env, prefix_path,
                    log_fn=_wlog,
                    status_fn=lambda m: safe_emit(self._net8_status_sig, m, ""))
                if not ok:
                    raise RuntimeError(".NET 8 install failed (see log).")
                safe_emit(self._net8_status_sig,
                          ".NET 8 ready.", GREEN)
                safe_emit(self._net8_done_sig, True)
            except Exception as exc:
                safe_emit(self._net8_status_sig, f"Error: {exc}", RED)
                self._log(f"Script Merger Wizard: .NET 8 install error: {exc}")
                safe_emit(self._net8_done_sig, False)

        threading.Thread(target=worker, daemon=True, name="tw3sm-net8").start()

    def _on_net8_done(self, ok: bool):
        if ok:
            self._goto_step(_PG_RUN)

    # ---- run ----------------------------------------------------------------------
    def _start_run(self):
        game = self._game
        exe = tool_exe_path(game, _MERGER_EXE, _MERGER_DIR)
        if exe is None:
            self._set_status(
                self._run_status,
                f"{_MERGER_EXE} was not found.\n"
                "Please restart the wizard and install Script Merger first.",
                RED)
            return

        proton_name, prefix_mode = self._proton_name, self._prefix_mode
        prefix_env = self._prefix_env

        def worker():
            import subprocess
            from Utils.exe_launch import (
                link_game_documents, resolve_tool_prefix,
                shutdown_prefix_wineserver,
            )
            from Utils.steam_finder import proton_run_command
            _wlog = lambda m: self._log(f"Script Merger Wizard: {m}")
            try:
                result = prefix_env or resolve_tool_prefix(
                    exe, game, proton_name, prefix_mode, log_fn=_wlog)
                if result is None:
                    safe_emit(self._run_status_sig,
                              f"Could not find Proton '{proton_name}' — check "
                              "that it is installed in Steam.", RED)
                    return
                proton_script, compat_data, env = result

                # Script Merger watches Documents\The Witcher 3 (the mod load
                # order) and crashes if it's missing — a fresh tool prefix has
                # no such folder, so link the game prefix's real one in.
                link_game_documents(game, compat_data / "pfx",
                                    game.name, log_fn=_wlog)

                game_path = game.get_game_path()
                if game_path:
                    env["STEAM_COMPAT_INSTALL_PATH"] = str(game_path)

                # Update Script Merger config to point at the game folder.
                if game_path:
                    try:
                        from Utils.exe_args_builder import (
                            update_witcher3_script_merger_config,
                        )
                        update_witcher3_script_merger_config(game_path, exe)
                        _wlog("updated Script Merger config with game path.")
                    except Exception as cfg_exc:
                        _wlog(f"config update warning: {cfg_exc}")

                _wlog(f"launching {exe} via Proton")
                proc = subprocess.Popen(
                    proton_run_command(proton_script, "run", str(exe)),
                    env=env,
                    cwd=str(exe.parent),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                safe_emit(self._run_status_sig,
                          "WitcherScriptMerger is running.\nMerge your "
                          "conflicts, then close it and click Done.", GREEN)
                safe_emit(self._run_started_sig)
                proc.wait()
                shutdown_prefix_wineserver(proton_script, compat_data,
                                           log_fn=_wlog)
                _wlog("WitcherScriptMerger closed.")
                safe_emit(self._run_status_sig,
                          "WitcherScriptMerger closed.", GREEN)
                safe_emit(self._run_finished_sig)
            except Exception as exc:
                safe_emit(self._run_status_sig, f"Launch error: {exc}", RED)
                self._log(f"Script Merger Wizard: launch error: {exc}")

        threading.Thread(target=worker, daemon=True, name="tw3sm-run").start()

    def _on_run_started(self):
        self._ran = True
        self._done_btn.setEnabled(True)

    # ---- close: restore-before-close (Tk parity) --------------------------------
    # Merged files land in the deployed game folder; game.restore() rescues
    # them into staging.  It must complete BEFORE the view closes so the
    # follow-up ctx.refresh_modlist (GUI thread, from the base _finish) sees
    # them — marshalling after teardown would target a deleted view.
    _restore_done_sig = Signal()

    def _finish(self):
        if self._closing:
            return
        if self._ran and not getattr(self, "_restored", False):
            if getattr(self, "_restoring", False):
                return  # restore in flight — closes itself when done
            self._restoring = True
            self._done_btn.setEnabled(False)
            self._set_status(self._run_status,
                             "Restoring game files (rescuing merges)…")
            self._restore_done_sig.connect(self._guard(self._on_restore_done))
            game, log = self._game, self._log

            def worker():
                try:
                    game.restore(log_fn=log)
                except Exception as exc:
                    log(f"Script Merger Wizard: restore warning: {exc}")
                safe_emit(self._restore_done_sig)

            threading.Thread(target=worker, daemon=True,
                             name="tw3sm-restore").start()
            return
        super()._finish()

    def _on_restore_done(self):
        self._restored = True
        self._restoring = False
        self._finish()
