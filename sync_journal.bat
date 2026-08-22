@echo off
chcp 65001 > nul
setlocal

REM 매매일지를 문서로 만들고 git 으로 올린다.
REM
REM watchline 의 sync.bat 과 같은 방식이다. 문서는 순수 생성물이라 언제 몇 번을 돌려도
REM 안전하고, 내용이 바뀐 것이 없으면 커밋도 만들지 않는다.
REM
REM   sync_journal.bat            최근 매매일 하나
REM   sync_journal.bat 2026-08-21 그날만
REM   sync_journal.bat --all      기록이 있는 모든 매매일 (처음 한 번)

cd /d "%~dp0"

echo [1/3] 매매일지 문서 생성
uv run python export_journal.py %*
if errorlevel 1 (
    echo.
    echo 문서 생성에 실패했습니다. git 은 건드리지 않습니다.
    exit /b 1
)

if not exist "journal\.git" if not exist ".git" (
    echo.
    echo git 저장소가 없습니다. README 9장의 안내를 먼저 따라 주세요.
    exit /b 1
)

echo.
echo [2/3] 변경 확인
git add journal
git diff --cached --quiet
if not errorlevel 1 (
    echo 바뀐 것이 없습니다.
    exit /b 0
)
git diff --cached --stat

echo.
echo [3/3] 커밋과 푸시
for /f "tokens=1-3 delims=/ " %%a in ("%date%") do set TODAY=%%a-%%b-%%c
git commit -q -m "journal: %TODAY%"
git push -q
if errorlevel 1 (
    echo.
    echo 푸시에 실패했습니다. 커밋은 남아 있으니 인터넷 연결을 확인하고
    echo   git push
    echo 를 다시 실행하세요.
    exit /b 1
)

echo 완료.
endlocal