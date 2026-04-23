@echo off
REM Build the XonsaroyBot Windows executable.
REM Run from inside an activated virtualenv that has the dev requirements installed.

where pyinstaller >nul 2>nul
if errorlevel 1 (
    echo [build] PyInstaller not found. Install with:
    echo           pip install -r requirements-dev.txt
    exit /b 1
)

echo [build] cleaning previous build artefacts...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

echo [build] running PyInstaller...
pyinstaller --noconfirm XonsaroyBot.spec
if errorlevel 1 (
    echo [build] PyInstaller failed.
    exit /b 1
)

echo.
echo [build] success. Executable: dist\XonsaroyBot\XonsaroyBot.exe
echo [build] Copy .env.example next to the exe on the target machine, rename to .env,
echo        or launch the exe once and fill the Settings tab.
