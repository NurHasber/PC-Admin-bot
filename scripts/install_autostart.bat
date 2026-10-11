@echo off
REM Thin wrapper only — does not write scripts into Startup.
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. Create it and pip install -r requirements.txt
  exit /b 1
)
".venv\Scripts\python.exe" -m src.autostart install
if errorlevel 1 (
  echo.
  echo If antivirus blocked this, use the manual method:
  echo   .venv\Scripts\python.exe -m src.autostart manual
  exit /b 1
)
exit /b 0
