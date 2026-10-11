@echo off
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv.
  exit /b 1
)
".venv\Scripts\python.exe" -m src.autostart uninstall
exit /b %ERRORLEVEL%
