@echo off
REM chat-index daily refresh - rebuilds chatmap.db atomically.
REM Sources are opened read-only. Safe to run while a query is in progress.
REM Runs under the scheduler's non-interactive token, so use an absolute interpreter.
setlocal
set "PY=I:\python3.14.4\python.exe"
if not exist "%PY%" set "PY=python"
cd /d I:\tools\chat-index
echo [%DATE% %TIME%] start (python=%PY%) >> refresh.log
"%PY%" scripts\map_ds_stream.py >> refresh.log 2>&1
if errorlevel 1 goto mapfail
"%PY%" scripts\build_chatmap.py >> refresh.log 2>&1
if errorlevel 1 goto buildfail
echo [%DATE% %TIME%] done >> refresh.log
exit /b 0

:mapfail
echo [%DATE% %TIME%] map_ds_stream FAILED >> refresh.log
exit /b 1

:buildfail
echo [%DATE% %TIME%] build_chatmap FAILED >> refresh.log
exit /b 1
