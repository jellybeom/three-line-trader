"""매매일지 — 저장·조회, 차트 보관, 벤치마크 계산.

매매 데이터는 이미 다른 테이블에 있으므로, 여기서 검증할 것은 "사람이 쓴 것"이
정확히 남고 필요한 시점에 다시 꺼내지는가다.
"""

import asyncio

import pytest

from trader.notifier import benchmark, build_benchmark_field
from trader.state_machine import Params, Position, State
from trader.store import Store
from trader.ui import bus

D = "2026-08-10"
P = Params(
    line1=11_280, line2=10_800, line3=10_300, buy1_amount=200_000, buy2_amount=200_000
)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def _traded(store, symbol="003670", name="포스코엠텍", pnl=8_670):
    store.register_symbol(
        D,
        symbol,
        name,
        P,
        Position(
            state=State.CLOSED,
            avg_price=11_280,
            total_bought=18,
            remaining=0,
            realized_pnl=pnl,
            fees=378,
            high_price=12_070,
            low_price=11_200,
            day_open=11_280,
            day_close=12_050,
        ),
        memo="철강 테마",
        tags="테마주,상한가",
        base_date="2026-08-05",
    )


# ── 저장·조회 ──────────────────────────────────────────────────


def test_코멘트가_저장되고_복원된다(store):
    _traded(store)
    store.save_journal(D, "003670", good="3단계 다 먹음", bad="진입이 늦었다")
    saved = store.load_journal(D, "003670")
    assert saved["good"] == "3단계 다 먹음" and saved["bad"] == "진입이 늦었다"


def test_긴_글도_그대로_저장된다(store):
    """글자 수 제한을 두지 않는다 — 차트 한 장이 텍스트 수십 건보다 크다."""
    _traded(store)
    long_text = "가" * 3000
    store.save_journal(D, "003670", good=long_text)
    assert store.load_journal(D, "003670")["good"] == long_text


def test_차트_경로와_코멘트는_따로_갱신된다(store):
    """차트는 종료 시 자동으로, 코멘트는 나중에 사람이 쓴다."""
    _traded(store)
    store.save_journal(D, "003670", daily_path="/a/d.png", minute_path="/a/m.png")
    store.save_journal(D, "003670", good="잘함")

    saved = store.load_journal(D, "003670")
    assert saved["good"] == "잘함"
    assert saved["daily_path"] == "/a/d.png"  # 코멘트 저장이 경로를 지우지 않는다

    store.save_journal(D, "003670", daily_path="/b/d.png")
    assert store.load_journal(D, "003670")["good"] == "잘함"  # 반대도 마찬가지


def test_매매가_있었던_종목만_일지_대상이다(store):
    _traded(store)
    store.register_symbol(D, "005930", "삼성전자", P, Position())  # 미진입
    entries = store.journal_entries()
    assert [e["symbol"] for e in entries] == ["003670"]
    assert entries[0]["tags"] == "테마주,상한가"
    assert entries[0]["good"] == ""  # 아직 미작성


def test_기간으로_거를_수_있다(store):
    _traded(store)
    store.register_symbol(
        "2026-08-07",
        "005930",
        "삼성전자",
        P,
        Position(
            state=State.CLOSED,
            avg_price=100,
            total_bought=10,
            remaining=0,
            realized_pnl=500,
        ),
    )
    assert len(store.journal_entries()) == 2
    assert len(store.journal_entries(since=D)) == 1


# ── 벤치마크 ───────────────────────────────────────────────────


def _sym(name, bought, day_open, day_close):
    return {
        "name": name,
        "total_bought": bought,
        "day_open": day_open,
        "day_close": day_close,
    }


def test_내_종목과_관심종목_평균을_나눠_계산한다():
    """성과가 실력인지 시장 덕인지 가늠하는 재료."""
    symbols = [
        _sym("A", 10, 100, 107),
        _sym("B", 5, 100, 103),
        _sym("C", 0, 100, 101),
        _sym("D", 0, 100, 99),
    ]
    result = benchmark(symbols)
    assert result["traded_count"] == 2
    assert result["traded_avg"] == pytest.approx(0.05)
    assert result["watch_count"] == 4
    assert result["watch_avg"] == pytest.approx(0.025)


def test_틱을_못_받은_종목은_평균에서_빠진다():
    symbols = [_sym("A", 10, 100, 110), _sym("B", 0, 0, 0)]
    assert benchmark(symbols)["watch_count"] == 1


def test_비교_대상이_없으면_필드를_만들지_않는다():
    """내 종목만 있고 비교군이 없으면 '시장 대비' 가 의미가 없다."""
    assert build_benchmark_field([_sym("A", 10, 100, 110)]) is None
    field = build_benchmark_field([_sym("A", 10, 100, 110)], index_rate=0.01)
    assert field is not None and "KOSPI +1.00%" in field["value"]


def test_시장_대비_필드_형식():
    symbols = [_sym("A", 10, 100, 107), _sym("C", 0, 100, 101)]
    value = build_benchmark_field(symbols, index_rate=0.0082)["value"]
    assert "내 종목 **+7.00%** (1종목)" in value
    assert "관심종목 평균 +4.00% (2종목)" in value
    assert "KOSPI +0.82%" in value


# ── 코어 연동 ──────────────────────────────────────────────────


@pytest.fixture
def core(tmp_path):
    from trader.core import Core

    c = Core(bus.Bus(), db_dir=str(tmp_path))
    c._date = D
    c._store = Store(str(tmp_path / "t.db"))
    yield c
    c._store.close()


def test_일지_조회와_저장이_명령으로_오간다(core):
    _traded(core._store)
    asyncio.run(core._handle_command(bus.RequestJournal()))

    events = []
    while not core._bus.events.empty():
        events.append(core._bus.events.get_nowait())
    entries = [e for e in events if isinstance(e, bus.JournalEntries)][0].entries
    assert entries[0]["name"] == "포스코엠텍"

    asyncio.run(
        core._handle_command(bus.SaveJournal(D, "003670", "잘한 점", "아쉬운 점"))
    )
    assert core._store.load_journal(D, "003670")["good"] == "잘한 점"


def test_차트가_결과별_파일명으로_보관된다(core, tmp_path):
    """탐색기에서 정렬만 해도 손절 매매가 모이도록 파일명에 결과를 넣는다."""
    _traded(core._store)
    core._entries["003670"] = {
        "name": "포스코엠텍",
        "params": P,
        "pos": Position(
            state=State.CLOSED,
            avg_price=11_280,
            total_bought=18,
            remaining=0,
            realized_pnl=8_670,
            fees=378,
        ),
        "price": 12_050,
        "memo": "",
        "high": 0,
        "low": 0,
    }

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    daily, minute = src_dir / "d.png", src_dir / "m.png"
    for path in (daily, minute):
        path.write_bytes(b"\x89PNG\r\n\x1a\n")

    core._archive_charts("003670", str(daily), str(minute))

    saved = core._store.load_journal(D, "003670")
    assert "익절" in saved["daily_path"] and "포스코엠텍" in saved["daily_path"]
    assert f"journal/{D[:7]}/{D}".replace("/", "") in saved["daily_path"].replace(
        "\\", ""
    ).replace("/", "")
    from pathlib import Path

    assert Path(saved["daily_path"]).exists() and Path(saved["minute_path"]).exists()


def test_손절은_손절로_분류된다(core, tmp_path):
    _traded(core._store, pnl=-5_000)
    core._entries["003670"] = {
        "name": "포스코엠텍",
        "params": P,
        "pos": Position(
            state=State.CLOSED,
            avg_price=11_280,
            total_bought=18,
            remaining=0,
            realized_pnl=-5_000,
            fees=378,
        ),
        "price": 10_000,
        "memo": "",
        "high": 0,
        "low": 0,
    }
    chart = tmp_path / "d.png"
    chart.write_bytes(b"\x89PNG\r\n\x1a\n")

    core._archive_charts("003670", str(chart), "")
    assert "손절" in core._store.load_journal(D, "003670")["daily_path"]


def test_차트_보관_실패는_조용히_넘어간다(core):
    """보관은 부가 기능이라 매매·알림을 막으면 안 된다."""
    _traded(core._store)
    core._entries["003670"] = {
        "name": "포스코엠텍",
        "params": P,
        "pos": Position(state=State.CLOSED),
        "price": 0,
        "memo": "",
        "high": 0,
        "low": 0,
    }
    core._archive_charts("003670", "/없는경로/d.png", "")  # 예외가 새어 나오지 않는다
    assert core._store.load_journal(D, "003670").get("daily_path", "") == ""
