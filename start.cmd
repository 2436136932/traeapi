@echo off
rem trae2api-web one-click launcher (double-click friendly).
rem NOTE: keep this file pure ASCII. cmd.exe reads .cmd as ANSI/GBK,
rem so non-ASCII comments get garbled and break command parsing.
rem
rem Usage:
rem   start.cmd                background start (default)
rem   start.cmd -Foreground    run in foreground
rem   start.cmd -Restart       stop then start
rem   start.cmd -Stop          stop the service
rem   start.cmd -Port 8080     custom listen port
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set EXITCODE=%ERRORLEVEL%
if not "%EXITCODE%"=="0" (
    echo.
    echo [x] launcher exited with code: %EXITCODE%
)
echo.
pause
exit /b %EXITCODE%
