@echo off
rem ============================================================================
rem Program: ESP32-S3-RLCD-4.2 One-Click Build and Portable Package Entry
rem Usage: Double-click for interactive menu, or pass args e.g. make.bat -Action pack
rem ============================================================================
chcp 65001 >nul
setlocal

where pwsh >nul 2>&1
if %errorlevel%==0 (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0make.ps1" %*
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make.ps1" %*
)
set "EXIT_CODE=%errorlevel%"

if "%~1"=="" if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Script exited with code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
