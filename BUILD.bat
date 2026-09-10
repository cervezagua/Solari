@echo off
title Solari - Build Script
echo.
echo  ========================================
echo    Solari - Flip-Clock Widget Builder
echo  ========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found. Install Python 3.10+ from python.org
    pause & exit /b 1
)

echo  Closing Solari if running...
taskkill /f /im Solari.exe >nul 2>&1
echo.
echo  [1/4] Checking PyInstaller...
pip show pyinstaller >nul 2>&1 || pip install pyinstaller

echo  [2/4] Checking tzdata...
pip show tzdata >nul 2>&1 || pip install tzdata

echo  [3/4] Checking Pillow (card rendering) + pystray (tray icon)...
pip show Pillow  >nul 2>&1 || pip install Pillow
pip show pystray >nul 2>&1 || pip install pystray

echo  [4/4] Building EXE...
echo.

pyinstaller ^
  --onefile ^
  --windowed ^
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
  --add-data "solari.ico;." ^
  --collect-all tzdata ^
  --collect-all pystray ^
  --exclude-module numpy ^
  --exclude-module pandas ^
  --exclude-module matplotlib ^
  --exclude-module scipy ^
  --exclude-module IPython ^
  --exclude-module notebook ^
  --exclude-module docutils ^
  --exclude-module setuptools ^
  --exclude-module pkg_resources ^
  --exclude-module xml ^
  --exclude-module xmlrpc ^
  --exclude-module unittest ^
  --exclude-module http ^
  --exclude-module email ^
  --exclude-module html ^
  --exclude-module ftplib ^
  --exclude-module imaplib ^
  --exclude-module poplib ^
  --exclude-module smtplib ^
  --exclude-module telnetlib ^
  --exclude-module urllib ^
  solari.py

echo.
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
    echo    3. Add  --upx-dir .  to the pyinstaller command above
    echo.
) else (
    echo  [ERROR] Build failed. Check output above.
)
pause
