@echo off
rem Daily candidate pool collection. Registered in Windows Task Scheduler.
rem No Cyrillic inside: cmd.exe reads .cmd in the OEM codepage and would mangle it.
rem Project root is derived from this file location, so the path never appears literally.

setlocal
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set ROOT=%~dp0..
cd /d "%ROOT%"

set LOG=data\processed\candidates_daily.log
echo.>> "%LOG%"
echo ===== %DATE% %TIME% =====>> "%LOG%"
python posts_db\collect_candidates.py >> "%LOG%" 2>&1
echo exit=%ERRORLEVEL%>> "%LOG%"
endlocal
