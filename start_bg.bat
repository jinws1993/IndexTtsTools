@echo off
rem ============================================================
rem  TTSBatch - start in background (minimized)
rem
rem  Usage:  start_bg.bat [port]
rem  Log:     work\logs\server.log
rem  Stop:    stop.bat
rem
rem  NOTE: pure ASCII + CRLF on purpose. Chinese in .bat files
rem  makes cmd.exe mis-parse the file.
rem ============================================================
setlocal
cd /d "%~dp0"

set "PORT=7861"
if not "%~1"=="" set "PORT=%~1"

set "PY="
if exist "%~dp0python\python.exe" set "PY=%~dp0python\python.exe"
if not defined PY if exist "%~dp0venv\Scripts\python.exe" set "PY=%~dp0venv\Scripts\python.exe"

rem --- No bundled python? Ask IndexTTS2 for its own interpreter. ---
if not defined PY (
  for %%d in ("D:\IndexTTS2_portable" "D:\IndexTTS2" "C:\IndexTTS2_portable") do (
    if not defined PY if exist "%%~fd\python\python.exe" set "PY=%%~fd\python\python.exe"
  )
)

if not defined PY for /f "delims=" %%p in ('where python 2^>nul') do (
    if not defined PY set "PY=%%p"
)

if not defined PY (
  echo [ERROR] Python not found.
  echo   Put python.exe in "%~dp0python\python.exe", or set config.json.
  pause
  exit /b 1
)

rem --- Verify this interpreter can actually run the service ---
"%PY%" -c "import fastapi, uvicorn" 2>nul
if errorlevel 1 (
  echo [ERROR] %PY%
  echo   does not have fastapi / uvicorn installed.
  echo   Use the Python that ships with IndexTTS2.
  pause
  exit /b 1
)

echo Using Python: %PY%
echo.

if not exist "work\logs" mkdir "work\logs"

rem --- launch fully detached ---
rem  Do NOT use "start /min cmd /c ..." here: the child would share our
rem  console, and closing this window sends WM_CLOSE to it. The Intel
rem  Fortran runtime inside the CUDA stack then aborts the process with
rem  "forrtl: error (200): program aborting due to window-CLOSE event".
rem  launcher.py uses DETACHED_PROCESS so the service owns no window at all.
"%PY%" launcher.py --port %PORT%
if errorlevel 1 (
  echo [ERROR] Failed to spawn the service. Check work\logs\server.err.log
  pause
  exit /b 1
)

rem --- wait until the port answers ---
rem  Cold start imports torch/CUDA first, which can take 20s+ before
rem  uvicorn even binds. Allow up to ~2 minutes.
set /a TRIES=0
:WAIT
ping -n 2 127.0.0.1 >nul
set /a TRIES+=1
curl -s -o nul "http://127.0.0.1:%PORT%/api/v1/health"
if not errorlevel 1 goto READY
if %TRIES% GEQ 120 goto SLOW
goto WAIT

:READY
echo.
echo ============================================================
echo   Service is UP
echo.
echo   Web UI   : http://127.0.0.1:%PORT%
echo   API docs : http://127.0.0.1:%PORT%/docs
echo   Log file : work\logs\server.log
echo.
echo   For the LAN address (phone), open:
echo     http://127.0.0.1:%PORT%/api/v1/network
echo.
echo   Stop with: stop.bat
echo ============================================================
echo.
pause
exit /b 0

:SLOW
echo.
echo [WARN] Service did not answer within 2 minutes.
echo   It may still be starting, or something failed.
echo   Check work\logs\server.err.log
echo.
pause
exit /b 1
