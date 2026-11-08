"""Start with Windows via the current user's Run key (no admin rights needed)."""

import sys
import winreg
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "PC Agent"


def command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    # Development: pythonw runs without a console window.
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    run_py = Path(__file__).resolve().parent.parent / "run.py"
    return f'"{pythonw}" "{run_py}" --minimized'


def set_enabled(enabled: bool):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(key, VALUE)
            except FileNotFoundError:
                pass
