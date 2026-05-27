"""
Refresh modification times of all files in the Fallout New Vegas staging folder.

Test harness for the "option 1" archive-invalidation fix: FONV's
bInvalidateOlderFiles=1 only lets a loose file override a vanilla BSA asset when
the loose file's mtime is NEWER than the BSA. Mods copied from an old drive keep
their old mtimes and silently lose to the vanilla BSAs. This tool stamps every
staging file to "now" so they win the comparison.

Hardlinked deployments share the staging inode, so bumping staging mtimes
updates the deployed files in place — no re-deploy needed. Symlink/copy
deployments need a re-deploy afterwards for the new mtime to reach the game dir.
"""

import os
import threading

import customtkinter as ctk

PLUGIN_INFO = {
    "id":           "fonv_refresh_mtimes",
    "label":        "Refresh Staging File Mtimes",
    "description":  "Set every staging file's modification time to now so loose "
                    "files reliably override vanilla BSAs under bInvalidateOlderFiles.",
    "game_ids":     ["FalloutNV"],
    "all_games":    False,
    "dialog_class": "FonvRefreshMtimesDialog",
}


class FonvRefreshMtimesDialog(ctk.CTkFrame):

    def __init__(self, parent, game, log_fn=None, *, on_close=None, **extra):
        super().__init__(parent, fg_color="#1a1a2e", corner_radius=0)
        self._log = log_fn or (lambda msg: None)
        self._game = game
        self._on_close = on_close
        self._running = False

        staging = game.get_effective_mod_staging_path()
        self._staging = staging

        ctk.CTkLabel(
            self,
            text="Refresh Staging File Times",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="#e0e0ff",
        ).pack(padx=24, pady=(24, 8), anchor="w")

        if staging is None or not staging.is_dir():
            ctk.CTkLabel(
                self,
                text=f"Staging folder not found:\n{staging}",
                justify="left",
                text_color="#ff8080",
            ).pack(padx=24, pady=8, anchor="w")
            ctk.CTkButton(self, text="Close", command=self._close).pack(
                padx=24, pady=(8, 24), anchor="e")
            return

        ctk.CTkLabel(
            self,
            text=(
                "Sets the modification time of every file in the mods staging "
                "folder to the current time.\n\n"
                f"Folder: {staging}\n\n"
                "Hardlinked deploys update in place. After a symlink or copy "
                "deploy, re-deploy for the change to reach the game folder."
            ),
            justify="left",
            wraplength=460,
            text_color="#c8c8e0",
        ).pack(padx=24, pady=8, anchor="w")

        self._status = ctk.CTkLabel(self, text="", justify="left",
                                    text_color="#a0ffa0")
        self._status.pack(padx=24, pady=(4, 8), anchor="w")

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(padx=24, pady=(8, 24), fill="x")
        self._run_btn = ctk.CTkButton(btn_row, text="Refresh Times",
                                      command=self._start)
        self._run_btn.pack(side="left")
        ctk.CTkButton(btn_row, text="Close", fg_color="#444466",
                      command=self._close).pack(side="right")

    def _start(self) -> None:
        if self._running:
            return
        self._running = True
        self._run_btn.configure(state="disabled", text="Working…")
        self._status.configure(text="Refreshing…", text_color="#ffe0a0")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        touched = 0
        errors = 0
        for root, _dirs, files in os.walk(self._staging):
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    # follow_symlinks=False: stamp the link itself, not its
                    # target, so we never reach outside the staging tree.
                    os.utime(fpath, None, follow_symlinks=False)
                    touched += 1
                except OSError as exc:
                    errors += 1
                    self._log(f"  Could not touch {fpath}: {exc}")

        msg = f"Refreshed {touched} file(s)."
        if errors:
            msg += f" {errors} error(s) — see log."
        self._log(f"[fonv_refresh_mtimes] {msg}")
        self.after(0, lambda: self._finish(msg, bool(errors)))

    def _finish(self, msg: str, had_errors: bool) -> None:
        self._running = False
        self._status.configure(
            text=msg, text_color="#ff8080" if had_errors else "#a0ffa0")
        self._run_btn.configure(state="normal", text="Refresh Times")

    def _close(self) -> None:
        if callable(self._on_close):
            self._on_close()
        else:
            self.destroy()
