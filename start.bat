@echo off
rem Development run (without building the .exe)
cd /d "%~dp0"
if not exist .venv (
    echo First run - installing libraries...
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -q -r requirements.txt
)
.venv\Scripts\python.exe run.py
