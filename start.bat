@echo off
REM 프로그램 실행 파일. 시작프로그램(앱 > 시작프로그램)과 작업 스케줄러 양쪽에서 쓴다.
REM 작업 스케줄러에서는 "시작 위치"를 이 폴더로 지정해야 config.toml / data 경로가 맞습니다.
cd /d "%~dp0"

REM 이미 돌고 있으면 조용히 빠진다.
REM
REM main.py 도 포트 47321 로 중복 실행을 막지만, 그쪽은 **오류 창을 띄우고** 종료한다.
REM 시작프로그램이 아침에 띄워 놓은 상태에서 08:50 안전망 작업이 돌면 그 창이 매일
REM 쌓이므로, 여기서 먼저 확인해 창 없이 끝낸다. 잠금 자체는 main.py 가 계속 담당한다
REM (여기 확인과 실제 실행 사이의 틈은 main.py 가 막는다).
netstat -ano | findstr /r /c:"127.0.0.1:47321 .*LISTENING" > nul
if not errorlevel 1 (
    echo three-line-trader 가 이미 실행 중입니다.
    exit /b 0
)

uv run main.py