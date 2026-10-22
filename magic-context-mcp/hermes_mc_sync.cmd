@echo off
REM hermes_mc_sync.cmd - push Hermes session history into magic-context.
REM Run by hand or from Task Scheduler; safe to run repeatedly (idempotent).
REM Logs to hermes_mc_sync.log next to this file.
REM
REM NOTE: HERMES_STATE_DB is deliberately NOT set. hermes_mc_sync.py now
REM auto-discovers every Hermes DB (root state.db + profiles/*/state.db) so the
REM live per-profile databases are synced too; pinning one DB here would skip
REM them again. Only MAGIC_CONTEXT_DB is forced, to reach the live context.db.
setlocal
set HERE=%~dp0
set PY=I:\tools\cloud-workspace\.venv\Scripts\python.exe
set MAGIC_CONTEXT_DB=I:\cortexkit\magic-context\context.db
set DAYS=%1
if "%DAYS%"=="" set DAYS=30

"%PY%" "%HERE%hermes_mc_sync.py" --apply --days %DAYS% --mode both --quiet >> "%HERE%hermes_mc_sync.log" 2>&1
endlocal
