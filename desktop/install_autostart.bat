@echo off
rem Run the Wristle listener silently now and at every login.
rem Extra options are passed through, e.g.  install_autostart.bat --hotkey ctrl+shift+space
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (
  echo Setting up Python environment ^(first run only^)...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
set "VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Wristle.vbs"
> "%VBS%" echo CreateObject("WScript.Shell").Run """%~dp0.venv\Scripts\pythonw.exe"" ""%~dp0wristle.py"" --background %*", 0, False
.venv\Scripts\python wristle.py --stop >nul
wscript "%VBS%"
echo.
echo Done. Wristle is running in the background and will start at every login.
echo Log file: %APPDATA%\wristle\listener.log
echo Stop it: stop.bat    Remove auto-start: uninstall_autostart.bat
pause
