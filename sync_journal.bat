@echo off
REM Generate journal documents and push them to git.
REM
REM   .\sync_journal.bat              all trade dates on record (default)
REM   .\sync_journal.bat 2026-08-21   that day only
REM   .\sync_journal.bat --month 2026-08
REM
REM In PowerShell you must prefix it with `.\` -- cmd.exe does not need it.
REM
REM ASCII ONLY. cmd.exe reads batch files by byte offset; mixing `chcp 65001`
REM with multi-byte comments shifts those offsets and cmd ends up running a
REM stray fragment (2026-08-23: `'.' is not recognized as ...`). All Korean
REM text and every decision lives in sync_journal.py instead.
cd /d "%~dp0"
uv run python sync_journal.py %*