@echo off
echo [SOLAR] Building SolarLauncher.exe...
cd /d "%~dp0"
python -m PyInstaller --noconfirm SolarLauncher.spec
echo.
if exist "dist\SolarLauncher\SolarLauncher.exe" (
    echo [OK] Build complete! EXE is at: dist\SolarLauncher\SolarLauncher.exe
) else (
    echo [FAIL] Build failed. Check errors above.
)
pause
