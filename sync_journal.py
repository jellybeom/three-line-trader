"""매매일지를 문서로 만들고 git 으로 올린다.

문서 생성(`export_journal.py`)에 git 커밋·푸시를 얹은 것뿐이다. 문서는 순수 생성물이라
언제 몇 번을 돌려도 안전하고, 내용이 바뀐 것이 없으면 커밋도 만들지 않는다.

    uv run python sync_journal.py              기록이 있는 모든 매매일 (기본)
    uv run python sync_journal.py 2026-08-21   그날만
    uv run python sync_journal.py --month 2026-08

윈도우에서는 `sync_journal.bat` 이 이것을 부른다.

**왜 배치 파일이 아니라 파이썬인가**
cmd.exe 는 배치 파일을 바이트 위치를 기억하며 한 줄씩 읽는데, `chcp 65001` 로 코드페이지를
바꾸면 그 뒤 한글(3바이트)의 위치 계산이 어긋나 주석 끝의 마침표가 명령으로 튀어나온다
(2026-08-23 실측: `'.' is not recognized...`). 배치 파일은 ASCII 로만 두고 한글과 판단은
전부 여기로 옮겼다. 덤으로 git 처리에 테스트를 붙일 수 있게 됐다.

**기본이 전체(--all) 인 이유**
지난주 매매의 일지를 오늘 고치는 일이 흔하다. 최근 하루만 다시 만들면 그 문서는 옛날
내용 그대로 남는다. 내용이 같은 파일은 건드리지도 커밋하지도 않으므로 비용이 거의 없다.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

import export_journal


def run_git(*args: str) -> subprocess.CompletedProcess:
    """git 호출. **--no-pager 를 항상 붙인다.**

    빠뜨리면 git 이 출력을 less 로 넘긴다. 손으로 돌릴 때는 q 를 누를 때까지 멈춰 있고,
    작업 스케줄러로 돌면 눌러 줄 사람이 없어 영원히 멈춘다(2026-08-23 실측).
    """
    return subprocess.run(
        ["git", "--no-pager", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def has_staged_changes() -> bool:
    """스테이지에 올라온 변경이 있으면 True. (git diff --cached --quiet 는 있으면 1)"""
    return run_git("diff", "--cached", "--quiet").returncode != 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not Path(".git").exists():
        print("git 저장소가 없습니다. README 9장의 준비 단계를 먼저 따라 주세요.")
        return 1

    print(f"[1/3] 매매일지 문서 생성 ({' '.join(argv) or '--all'})")
    if export_journal.main(argv or ["--all"]) != 0:
        print("\n문서 생성에 실패했습니다. git 은 건드리지 않습니다.")
        return 1

    print("\n[2/3] 변경 확인")
    if (added := run_git("add", "journal")).returncode != 0:
        print(f"git add 에 실패했습니다: {added.stderr.strip()}")
        return 1
    if not has_staged_changes():
        print("바뀐 것이 없습니다.")
        return 0
    # 파일이 수십 개씩 바뀌는 날이 흔해 목록은 읽히지 않는다 — 한 줄 요약만 낸다.
    print(run_git("diff", "--cached", "--shortstat").stdout.strip())

    print("\n[3/3] 커밋과 푸시")
    if (done := run_git("commit", "-m", f"journal: {date.today()}")).returncode != 0:
        print(f"커밋에 실패했습니다: {(done.stderr or done.stdout).strip()}")
        return 1
    if (pushed := run_git("push")).returncode != 0:
        print(f"\n푸시에 실패했습니다: {pushed.stderr.strip()}")
        print(
            "커밋은 남아 있으니 인터넷 연결을 확인하고 `git push` 를 다시 실행하세요."
        )
        return 1

    print("완료.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
