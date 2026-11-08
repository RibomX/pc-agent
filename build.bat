@echo off
rem Builds release\PC Agent\PC Agent.exe
cd /d "%~dp0"
if not exist .venv (
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -q -r requirements.txt pyinstaller
)
.venv\Scripts\python.exe -m pc_agent.icon
.venv\Scripts\pyinstaller.exe --noconfirm --windowed --name "PC Agent" --icon assets\icon.ico ^
    --add-data "pc_agent\ui.html;pc_agent" --collect-submodules uvicorn --collect-submodules pc_agent ^
    --hidden-import pystray._win32 --distpath release --workpath build run.py
echo.
echo Hotovo: release\PC Agent\PC Agent.exe
pause

