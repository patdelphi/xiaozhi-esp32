@echo off
rem ============================================================================
rem Program: Package CastServer.exe, Quote0Local.exe and .env into release dir
rem Usage: Double-click or pass args e.g. package_release.bat -RebuildQuote0
rem ============================================================================
chcp 65001 >nul
setlocal

where pwsh >nul 2>&1
if %errorlevel%==0 (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0package_release.ps1" %*
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0package_release.ps1" %*
)
set "EXIT_CODE=%errorlevel%"

if "%~1"=="" if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Release packaging exited with code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
