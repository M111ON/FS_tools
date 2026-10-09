@echo off
REM hermes_mc_sync.cmd — push Hermes session history into magic-context.
REM Run by hand or from Task Scheduler; safe to run repeatedly (idempotent).
REM Logs to hermes_mc_sync.log next to this file.
setlocal
set HERE=%~dp0
set PY=I:\tools\cloud-workspace\.venv\Scripts\python.exe
set MAGIC_CONTEXT_DB=I:\cortexkit\magic-context\context.db
set HERMES_STATE_DB=I:\Hermes\state.db

"%PY%" "%HERE%hermes_mc_sync.py" --apply --days %1 --mode digest --quiet >> "%HERE%hermes_mc_sync.log" 2>&1
endlocal
