@echo off
rem Post Metrics enrichment. Registered in Windows Task Scheduler, every 15 minutes.
rem No Cyrillic inside: cmd.exe reads .cmd in the OEM codepage and would mangle it.
rem Project root is derived from this file location, so the path never appears literally.

setlocal enabledelayedexpansion
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set ROOT=%~dp0..
cd /d "%ROOT%"

set LOG=data\processed\post_metrics_enrichment.log
echo.>> "%LOG%"
echo ===== %DATE% %TIME% =====>> "%LOG%"
python postgres\enrich_post_metrics.py --apply >> "%LOG%" 2>&1
echo exit=!ERRORLEVEL!>> "%LOG%"
endlocal
