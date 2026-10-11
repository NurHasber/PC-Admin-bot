@echo off
REM Prefer silent VBS launcher (no console flash).
wscript //nologo "%~dp0run.vbs"
exit /b %ERRORLEVEL%
