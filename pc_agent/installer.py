"""Install the packaged app on this PC: copy it to the user's Programs folder and add
Start menu and Desktop shortcuts (no admin rights needed)."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import PORTABLE

INSTALL_DIR = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "PC Agent"


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def is_installed() -> bool:
    return is_frozen() and Path(sys.executable).parent.resolve() == INSTALL_DIR.resolve()


def can_install() -> bool:
    """The portable copy (portable.txt next to the exe) runs where it was unzipped and never installs."""
    return is_frozen() and not is_installed() and not PORTABLE


def _shortcut(folder: str, target: Path):
    """folder = a Windows special folder name ("Desktop", "Programs"); OneDrive-moved folders work too."""
    # WScript.Shell is the standard way to create .lnk files without extra libraries.
    script = (
        f"$d=[Environment]::GetFolderPath('{folder}');"
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'PC Agent.lnk'));"
        f"$s.TargetPath='{target}';$s.WorkingDirectory='{target.parent}';$s.IconLocation='{target},0';$s.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True, timeout=30,
                   creationflags=subprocess.CREATE_NO_WINDOW)


def install() -> Path:
    """Copy the running app to INSTALL_DIR and create shortcuts. Returns the installed exe."""
    if not is_frozen():
        raise RuntimeError("Installing works from the packaged PC Agent.exe, not from source.")
    source = Path(sys.executable).parent
    exe = INSTALL_DIR / Path(sys.executable).name
    if not is_installed():
        shutil.copytree(source, INSTALL_DIR, dirs_exist_ok=True)
    _shortcut("Programs", exe)
    _shortcut("Desktop", exe)
    return exe


def restart_installed(exe: Path):
    """Start the installed copy a moment after this process exits (only one instance may run)."""
    # `timeout` needs a console, so wait with PowerShell (a hidden one – DETACHED_PROCESS breaks it).
    # -WindowStyle Normal: otherwise the app inherits the hidden helper's window state.
    cmd = ["powershell", "-NoProfile", "-Command", f"Start-Sleep 3; Start-Process -WindowStyle Normal '{exe}'"]
    flags = subprocess.CREATE_NO_WINDOW
    try:
        # Also leave a job object the app may run in (otherwise the helper dies with the app).
        subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB)
    except OSError:
        subprocess.Popen(cmd, creationflags=flags)
