@echo off
setlocal
REM Kill only PC Admin Bot (pythonw -m src.main from this project). Does not touch other Python apps.
cd /d "%~dp0.."
set "MARKER=Project PC admin bot"

powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command ^
  "$n=0; Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | Where-Object { $_.CommandLine -and ($_.CommandLine -like '*Project PC admin bot*' -and $_.CommandLine -like '*src.main*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Output ('Stopped PID ' + $_.ProcessId); $n++ }; Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | Where-Object { $_.ExecutablePath -and $_.ExecutablePath -like '*Project PC admin bot*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Output ('Stopped bridge node PID ' + $_.ProcessId); $n++ }; if ($n -eq 0) { Write-Output 'PC Admin Bot is not running.' }"

exit /b 0
