@echo off
rem ============================================================
rem  TTSBatch - stop the background service
rem
rem  Kills BOTH the server and the watchdog (launcher.py).
rem  The watchdog restarts the server if it dies, so it has to go
rem  first -- otherwise stop.bat would just see it come back.
rem
rem  Matches on python.exe command lines only, and requires the
rem  port to appear too, so the original IndexTTS2 server (7860)
rem  is never touched.
rem ============================================================
setlocal
cd /d "%~dp0"

set "PORT=7861"
if not "%~1"=="" set "PORT=%~1"

echo Stopping TTSBatch (port %PORT%) ...

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$port = '%PORT%';" ^
  "$procs = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\";" ^
  "$watch = $procs | Where-Object { $_.CommandLine -like '*launcher.py*' };" ^
  "if ($watch) { $watch | ForEach-Object { Write-Host ('  stopping watchdog PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } };" ^
  "Start-Sleep -Milliseconds 800;" ^
  "$hit = $procs | Where-Object { $_.CommandLine -like '*webapp_server.py*' -and $_.CommandLine -like ('*' + $port + '*') };" ^
  "if ($hit) { $hit | ForEach-Object { Write-Host ('  stopping server PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }" ^
  "else { Write-Host '  no matching server process found' }"

echo.
echo Done.
echo.
pause
