@echo off
rem Builds two single-file programs in release\:
rem   PC Agent.exe           - installs itself (Start menu + Desktop shortcut)
rem   PC Agent Portable.exe  - runs where it is, settings in a "data" folder next to it
cd /d "%~dp0"
if not exist .venv (
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -q -r requirements.txt pyinstaller
)
.venv\Scripts\python.exe -m pc_agent.icon
.venv\Scripts\pyinstaller.exe --noconfirm --onefile --windowed --name "PC Agent" --icon assets\icon.ico ^
    --add-data "pc_agent\ui.html;pc_agent" --collect-submodules uvicorn --collect-submodules pc_agent ^
    --hidden-import pystray._win32 --distpath release --workpath build run.py
rem The same program; the word "Portable" in its name makes it portable.
copy /y "release\PC Agent.exe" "release\PC Agent Portable.exe" >nul
echo.
echo Done: release\PC Agent.exe and release\PC Agent Portable.exe
pause
