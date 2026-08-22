@echo off
chcp 65001 > nul
setlocal

REM 매매일지를 문서로 만들고 git 으로 올린다.
REM
REM   .\sync_journal.bat              기록이 있는 모든 매매일 (기본)
REM   .\sync_journal.bat 2026-08-21   그날만
REM   .\sync_journal.bat --month 2026-08
REM
REM PowerShell 에서는 앞에 `.\` 를 붙여야 한다. cmd.exe 에서는 없어도 된다.
REM
REM **기본이 --all 인 이유**: 지난주 매매의 일지를 오늘 고치는 일이 흔하다. 최근 하루만
REM 다시 만들면 그 문서는 옛날 내용 그대로 남는다. 내용이 같은 파일은 건드리지도, 커밋
REM 하지도 않으므로 전체를 돌려도 비용이 거의 없다.
REM
REM 15:40 작업 스케줄러 등록은 README 9장 참고. 요약 발송(15:35)과 겹치지 않게 둔다.

cd /d "%~dp0"

if not exist ".git" (
    echo git 저장소가 없습니다. README 9장의 준비 단계를 먼저 따라 주세요.
    exit /b 1
)

set ARGS=%*
if "%ARGS%"=="" set ARGS=--all

echo [1/3] 매매일지 문서 생성 ^(%ARGS%^)
uv run python export_journal.py %ARGS%
if errorlevel 1 (
    echo.
    echo 문서 생성에 실패했습니다. git 은 건드리지 않습니다.
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
REM %date% 는 윈도우 지역 설정마다 형식이 달라 커밋 메시지가 깨진다. PowerShell 로
REM 형식을 고정한다 (Win10 이상이면 항상 있다).
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i
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