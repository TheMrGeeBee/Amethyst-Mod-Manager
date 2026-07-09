#!/usr/bin/env python3
"""Entry point for the Qt (PySide6) UI.

Run from src/ so the gui_qt / Utils / Games packages import cleanly:

    ../.venv/bin/python3 run_qt.py
"""

import sys

import app_bootstrap

app_bootstrap.setup_environment()

from gui_qt.app import run

if __name__ == "__main__":
    sys.exit(run())
