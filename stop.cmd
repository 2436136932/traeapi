@echo off
rem traeapi one-click STOP launcher (double-click friendly).
rem NOTE: keep this file pure ASCII. cmd.exe reads .cmd as ANSI/GBK,
rem so non-ASCII comments get garbled and break command parsing.
rem
rem Equivalent to: start.ps1 -Stop
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -Stop
set EXITCODE=%ERRORLEVEL%
echo.
pause
exit /b %EXITCODE%
