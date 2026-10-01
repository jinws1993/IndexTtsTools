@echo off
rem ============================================================
rem  TTSBatch - start in foreground
rem  Keep this window OPEN while using the service.
rem  Press Ctrl+C to stop.
rem
rem  NOTE: this file is intentionally pure ASCII with CRLF endings.
rem  Chinese text in .bat files breaks cmd.exe parsing.
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
  echo.
  echo   Option A: copy IndexTTS2's python folder here, as
  echo             "%~dp0python"
  echo   Option B: install Python and add it to PATH
  echo.
  pause
  exit /b 1
)

rem --- Verify this interpreter can actually run the service ---
"%PY%" -c "import fastapi, uvicorn" 2>nul
if errorlevel 1 (
  echo [ERROR] This Python cannot import fastapi / uvicorn:
  echo   %PY%
  echo.
  echo   Use the Python that ships with IndexTTS2, or:
  echo     "%PY%" -m pip install fastapi uvicorn pydantic python-multipart
  echo.
  pause
  exit /b 1
)

echo Python: %PY%
echo Starting service on port %PORT% ...
echo Close this window or press Ctrl+C to stop.
echo.

"%PY%" webapp_server.py --port %PORT%

echo.
echo Service stopped.
pause
