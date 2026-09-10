@echo off
setlocal
title Solari - Build Script
echo.
echo  ========================================
echo    Solari - Flip-Clock Widget Builder
echo  ========================================
echo.

REM --- Locate a Python interpreter -----------------------------------------
set "PY=python"
%PY% --version >nul 2>&1
if errorlevel 1 (
    set "PY=py -3"
    py -3 --version >nul 2>&1
    if errorlevel 1 (
        echo  [ERROR] Python not found. Install Python 3.10+ from python.org
        echo          and tick "Add python.exe to PATH" in the installer.
        pause & exit /b 1
    )
)
for /f "delims=" %%V in ('%PY% --version 2^>^&1') do echo  Using %%V
echo.

echo  Closing Solari if running...
taskkill /f /im Solari.exe >nul 2>&1

echo.
echo  [1/4] Checking PyInstaller...
%PY% -m PyInstaller --version >nul 2>&1 || %PY% -m pip install --upgrade pyinstaller
%PY% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] PyInstaller could not be installed or run.
    echo          Try manually:  %PY% -m pip install --upgrade pyinstaller
    pause & exit /b 1
)

echo  [2/4] Checking tzdata...
%PY% -c "import tzdata" >nul 2>&1 || %PY% -m pip install tzdata

echo  [3/4] Checking Pillow (card rendering) + pystray (tray icon)...
%PY% -c "import PIL" >nul 2>&1 || %PY% -m pip install Pillow
%PY% -c "import pystray" >nul 2>&1 || %PY% -m pip install pystray

echo  [4/4] Building EXE...
echo.

REM Invoked as a module, not as pyinstaller.exe: right after a fresh install
REM the Scripts directory is often not on PATH yet, and the bare command fails.
%PY% -m PyInstaller ^
  --onefile ^
  --windowed ^
  --noconfirm ^
  --clean ^
  --name "Solari" ^
  --icon "solari.ico" ^
  --hidden-import zoneinfo ^
  --hidden-import tzdata ^
  --hidden-import pystray ^
  --hidden-import PIL ^
  --hidden-import PIL.Image ^
  --hidden-import PIL.ImageDraw ^
  --hidden-import PIL.ImageFilter ^
  --hidden-import PIL.ImageTk ^
  --collect-all tzdata ^
  --collect-all pystray ^
  --exclude-module numpy ^
  --exclude-module pandas ^
  --exclude-module matplotlib ^
  --exclude-module scipy ^
  --exclude-module IPython ^
  --exclude-module notebook ^
  --exclude-module docutils ^
  --exclude-module pytest ^
  --exclude-module unittest ^
  --exclude-module xmlrpc ^
  --exclude-module ftplib ^
  --exclude-module imaplib ^
  --exclude-module poplib ^
  --exclude-module smtplib ^
  solari.py

set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo  [ERROR] PyInstaller exited with code %RC%.
    echo          The real cause is in the output above - look for the last
    echo          line starting with "ERROR:" or a Python traceback.
    pause & exit /b %RC%
)

if exist "dist\Solari.exe" (
    echo  =========================================
    echo    SUCCESS!  dist\Solari.exe is ready
    echo  =========================================
    echo.
    for %%I in ("dist\Solari.exe") do echo    Size: %%~zI bytes
    echo.
    echo  Config saved to: %%APPDATA%%\Solari\
    echo.
    echo  Optional - further ~40%% compression with UPX:
    echo    1. Download upx.exe from github.com/upx/upx/releases
    echo    2. Place upx.exe in this folder
    echo    3. Add  --upx-dir .  to the PyInstaller command above
    echo.
) else (
    echo  [ERROR] PyInstaller reported success but dist\Solari.exe is missing.
)
pause
