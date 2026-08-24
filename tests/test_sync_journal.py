"""매매일지 git 동기화 (3단계-2).

배치 파일이 아니라 파이썬이라 시험할 수 있다. 실제 저장소를 임시 폴더에 만들어 돌린다 —
git 호출을 흉내 내면 정작 어긋나기 쉬운 인자와 종료 코드를 검증하지 못한다.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import sync_journal
from trader.state_machine import Decision, Params, Position, Side, State
from trader.store import Store

P = Params(
    line1=5_200, line2=5_000, line3=4_900, buy1_amount=200_000, buy2_amount=200_000
)


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8"
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """빈 저장소 + 매매 기록 하나. 원격은 같은 디스크의 bare 저장소로 흉내 낸다."""
    if not subprocess.run(["git", "--version"], capture_output=True).returncode == 0:
        pytest.skip("git 이 없는 환경")
    work, remote = tmp_path / "work", tmp_path / "remote.git"
    work.mkdir()
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "test")
    _git(work, "remote", "add", "origin", str(remote))

    (work / ".gitignore").write_text("data/\n", encoding="utf-8")  # 실제 저장소와 같게
    (work / "data").mkdir()
    (work / "data" / "mode.txt").write_text("실전", encoding="utf-8")
    store = Store(work / "data" / "trader-real.db")
    store.register_symbol("2026-08-21", "263800", "데이타솔루션", P)
    store.save_transition(
        "2026-08-21",
        "263800",
        State.WAITING,
        Position(State.BUY1, 5_200, 37, 37),
        Decision(State.BUY1, Side.BUY, 37, "1선 이탈 → 1차 매수"),
        5_200,
        5_200,
    )
    store.save_transition(
        "2026-08-21",
        "263800",
        State.BUY1,
        Position(
            State.CLOSED,
            5_200,
            37,
            0,
            realized_pnl=-7_000,
            fees=300,
            high_price=5_270,
            low_price=4_990,
        ),
        Decision(State.CLOSED, Side.SELL, 37, "3선 이탈 → 전량 손절"),
        5_010,
        4_990,
    )
    store.close()

    monkeypatch.chdir(work)
    _git(work, "add", ".gitignore")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "push", "-q", "-u", "origin", "HEAD")
    return work


def test_문서를_만들고_커밋해_푸시한다(repo, capsys):
    assert sync_journal.main([]) == 0

    out = capsys.readouterr().out
    assert "[1/3]" in out and "[2/3]" in out and "[3/3]" in out and "완료" in out
    assert (repo / "journal" / "2026-08" / "2026-08-21.md").exists()
    log = _git(repo, "log", "-1", "--pretty=%s").stdout
    assert log.startswith("journal: 2026-")
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""  # 남은 변경 없음


def test_바뀐_것이_없으면_커밋을_만들지_않는다(repo, capsys):
    """문서는 순수 생성물이라 매번 다시 만들어진다 — 그때마다 커밋이 생기면 안 된다."""
    sync_journal.main([])
    before = _git(repo, "rev-parse", "HEAD").stdout

    assert sync_journal.main([]) == 0

    assert "바뀐 것이 없습니다" in capsys.readouterr().out
    assert _git(repo, "rev-parse", "HEAD").stdout == before


def test_git_저장소가_없으면_아무것도_하지_않는다(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert sync_journal.main([]) == 1
    assert "git 저장소가 없습니다" in capsys.readouterr().out


def test_문서_생성이_실패하면_git을_건드리지_않는다(repo, monkeypatch, capsys):
    """DB 를 못 찾는 상태로 커밋하면 멀쩡한 문서가 지워진 것으로 기록된다."""
    sync_journal.main([])
    before = _git(repo, "rev-parse", "HEAD").stdout
    (repo / "data" / "trader-real.db").unlink()

    assert sync_journal.main([]) == 1

    assert "문서 생성에 실패했습니다" in capsys.readouterr().out
    assert _git(repo, "rev-parse", "HEAD").stdout == before


def test_푸시가_실패해도_커밋은_남는다(repo, capsys):
    """인터넷이 끊겨도 다음에 git push 만 하면 되도록."""
    _git(repo, "remote", "set-url", "origin", str(repo / "없는원격.git"))

    assert sync_journal.main([]) == 1

    out = capsys.readouterr().out
    assert "푸시에 실패했습니다" in out and "git push" in out
    assert _git(repo, "log", "-1", "--pretty=%s").stdout.startswith("journal: ")


def test_인자를_그대로_넘긴다(repo, capsys):
    """`sync_journal.bat 2026-08-21` 처럼 날짜를 지정할 수 있어야 한다."""
    assert sync_journal.main(["2026-08-21"]) == 0
    assert (repo / "journal" / "2026-08" / "2026-08-21.md").exists()

    assert sync_journal.main(["--month", "2026-07"]) == 0  # 해당 월에 기록 없음
    assert "바뀐 것이 없습니다" in capsys.readouterr().out


# ── 배치 파일 (윈도우에서만 도는 코드라 파일 자체를 검사한다) ────


@pytest.mark.parametrize("name", ["sync_journal.bat", "start.bat"])
def test_배치_파일은_ASCII_에_CRLF_다(name):
    """cmd.exe 는 배치 파일을 바이트 위치로 읽는다.

    `chcp 65001` 과 한글 주석이 섞이면 위치 계산이 어긋나 주석 끝의 마침표가 명령으로
    튀어나온다(2026-08-23 실측: `'.' is not recognized...`). LF 만 있어도 여러 줄 블록을
    잘못 끊는다. 한글과 판단은 전부 파이썬 쪽에 둔다.
    """
    data = Path(__file__).resolve().parents[1].joinpath(name).read_bytes()
    data.decode("ascii")  # 한글이 섞이면 여기서 터진다
    assert data.count(b"\n") == data.count(b"\r\n"), "LF 만 있는 줄이 있다"
    for line in data.decode("ascii").splitlines():
        assert not line.strip().lower().startswith("chcp"), line


# ── 실행 기록 (작업 스케줄러에는 0x0/0x1 밖에 안 남는다) ────────


def _sync_log(repo) -> str:
    path = repo / "data" / "sync_journal.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_푸시_결과가_로그에_남는다(repo):
    """작업 스케줄러로 돌면 콘솔이 없다 — 왜 실패했는지 볼 곳이 있어야 한다."""
    sync_journal.main([])

    log = _sync_log(repo)
    assert "푸시 완료" in log
    assert "files changed" in log  # 무엇이 바뀌었는지도 함께


def test_실패_원인이_로그에_남는다(repo):
    _git(repo, "remote", "set-url", "origin", str(repo / "없는원격.git"))

    sync_journal.main([])

    assert "푸시 실패" in _sync_log(repo)


def test_변경이_없어도_돌았다는_기록은_남는다(repo):
    """'작업이 안 돈 것' 과 '돌았는데 바뀐 게 없는 것' 은 다르다."""
    sync_journal.main([])
    sync_journal.main([])

    assert "변경 없음" in _sync_log(repo)


def test_로그는_무한정_쌓이지_않는다(repo, monkeypatch):
    monkeypatch.setattr(sync_journal, "_LOG_KEEP", 3)
    for _ in range(6):
        sync_journal.main([])

    assert len(_sync_log(repo).strip().splitlines()) == 3
