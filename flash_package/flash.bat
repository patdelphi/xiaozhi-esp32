@echo off
rem ============================================================================
rem Program: ESP32-S3-RLCD-4.2 Portable Standalone Flasher Entry
rem Usage: Double-click to run on any Windows 10/11 PC (No ESP-IDF/Python needed)
rem ============================================================================
chcp 65001 >nul
setlocal

where pwsh >nul 2>&1
if %errorlevel%==0 (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0flash.ps1" %*
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0flash.ps1" %*
)
set "EXIT_CODE=%errorlevel%"

if "%~1"=="" if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Flasher exited with code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
