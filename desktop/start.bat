@echo off
rem Start the Wristle listener in a window (needed once to pair a new watch).
cd /d "%~dp0"
if not exist .venv (
  echo Setting up Python environment ^(first run only^)...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
.venv\Scripts\python wristle.py %*
pause
