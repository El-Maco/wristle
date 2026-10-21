@echo off
cd /d "%~dp0"
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Wristle.vbs" 2>nul
.venv\Scripts\python wristle.py --stop
echo Auto-start removed.
pause
