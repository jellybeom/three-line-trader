@echo off
REM Launch three-line-trader. Used both by the Startup folder and by the
REM Task Scheduler safety net (see README section 9).
REM Task Scheduler must have "Start in" set to this folder, otherwise
REM config.toml and data/ paths will not resolve.
REM
REM ASCII ONLY -- see the note in sync_journal.bat for why.
cd /d "%~dp0"

REM Quietly bail out if it is already running. main.py also guards with the
REM same port, but it pops up a message box; the 08:50 safety net would then
REM stack one dialog every morning on top of the Startup-folder instance.
REM The real lock stays in main.py -- this only avoids the dialog.
netstat -ano | findstr /r /c:"127.0.0.1:47321 .*LISTENING" > nul
if not errorlevel 1 (
    echo three-line-trader is already running.
    exit /b 0
)

uv run main.py