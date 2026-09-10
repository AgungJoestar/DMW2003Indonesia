@echo off
setlocal
cd /d "%~dp0"

echo ==================================================
echo DW2003 Indonesian Patcher v1.0 - Release
echo ==================================================
echo.

if not exist "all_strings.xlsx" (
    echo ERROR: all_strings.xlsx tidak ditemukan.
    echo Taruh all_strings.xlsx di folder yang sama dengan builder ini.
    goto :fail
)

if not exist "logo.png" (
    echo ERROR: logo.png tidak ditemukan.
    echo Taruh logo.png di folder yang sama dengan builder ini.
    goto :fail
)

echo [1/3] Membuat full translation manifest...
py build_manifest_v100.py
if errorlevel 1 goto :fail

echo.
echo [2/3] Install/check PyInstaller...
py -m pip install pyinstaller
if errorlevel 1 goto :fail

echo.
echo [3/3] Build single EXE + embed logo...
py -m PyInstaller --noconfirm --clean --onefile --windowed ^
 --add-data "logo.png;." ^
 --name "DW2003_Indonesia_Patcher" ^
 dw2003_patcher_standalone_v100_built.py
if errorlevel 1 goto :fail

echo.
echo ==================================================
echo BERHASIL
echo EXE:
echo %CD%\dist\DW2003_Indonesia_Patcher.exe
echo ==================================================
pause
exit /b 0

:fail
echo.
echo BUILD GAGAL.
pause
exit /b 1
