@echo off
REM Windows 작업 스케줄러용 실행 파일.
REM 작업 스케줄러에서 "시작 위치"를 이 폴더로 지정해야 config.toml / data 경로가 맞습니다.
cd /d "%~dp0"
uv run main.py
