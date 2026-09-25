@echo off
setlocal
title AIoT PowerGuard

rem Double-click this file, or run it from any shell. %~dp0 is this file's own
rem folder and keeps working when the project path contains spaces, so nothing
rem depends on the current directory.

set "PG_ROOT=%~dp0"
set "PG_SCRIPT=%PG_ROOT%scripts\start_powerguard.ps1"

if not exist "%PG_SCRIPT%" (
    echo.
    echo   Cannot find "%PG_SCRIPT%".
    echo   Keep START_POWERGUARD.cmd in the project root, next to the scripts folder.
    echo.
    pause
    exit /b 1
)

rem PowerShell 7 if it is installed, Windows PowerShell 5.1 otherwise; the
rem script is written to run on both.
set "PG_SHELL=powershell.exe"
where pwsh.exe >nul 2>&1 && set "PG_SHELL=pwsh.exe"

"%PG_SHELL%" -NoProfile -ExecutionPolicy Bypass -File "%PG_SCRIPT%" %*
set "PG_EXIT=%ERRORLEVEL%"

if not "%PG_EXIT%"=="0" (
    echo.
    echo   PowerGuard exited with code %PG_EXIT%. The message above says why.
    echo.
    pause
)

endlocal & exit /b %PG_EXIT%
