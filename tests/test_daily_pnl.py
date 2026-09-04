"""실현손익은 **그날 것**, 매매 전체는 **합산**.

2026-08-18 실제 사고: 이월이 포지션을 통째로 복사해 어제 번 돈이 오늘 것으로 다시
계산됐다. 씨앤씨인터내셔널은 평단 그대로 팔아 오늘 0원인데 +707원으로 보고됐고,
증권사 대조가 이를 잡아냈다(프로그램 +707 · 증권사 -274).

한 값이 두 질문에 답하려던 것이 원인이다.
- "오늘 얼마 벌었나" → UI 상단·일일 요약·증권사 대조
- "이 매매로 얼마 벌었나" → 매매일지·종료 알림
"""

from __future__ import annotations

import sqlite3

import pytest

from trader.state_machine import Params, Position, State, carry_to_next_day
from trader.store import Store, _split_daily_pnl

_P = Params(21_800, 21_200, 20_600, 200_000, 200_000)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    yield s
    s.close()


# ── 이월 ──────────────────────────────────────────────────────


def test_이월하면_그날_손익과_비용을_두고_간다():
    pos = Position(
        State.BUY1_TP1,
        avg_price=21_200,
        total_bought=9,
        remaining=6,
        realized_pnl=1_050,
        fees=134,
    )
    carried = carry_to_next_day(pos)
    assert carried.realized_pnl == 0
    assert carried.fees == 0


def test_이월해도_상태와_평단과_잔량은_그대로다():
    """이월의 목적은 포지션을 이어가는 것이다."""
    pos = Position(State.BUY2, 21_200, 9, 6, realized_pnl=1_050, fees=134)
    carried = carry_to_next_day(pos)
    assert carried.state is State.BUY2
    assert carried.avg_price == 21_200
    assert carried.total_bought == 9
    assert carried.remaining == 6


def test_최고_최저는_이월한다():
    """보유가 이어지는 동안의 값이라 며칠에 걸치는 것이 원래 의미다."""
    pos = Position(State.BUY1, 21_200, 9, 9, high_price=22_500, low_price=20_800)
    carried = carry_to_next_day(pos)
    assert carried.high_price == 22_500
    assert carried.low_price == 20_800


def test_당일_시가_종가_최저는_비운다():
    """하루짜리 값이다 — 넘기면 다음 날 등락률이 엉뚱해진다."""
    pos = Position(
        State.BUY1, 21_200, 9, 9, day_open=21_000, day_close=21_500, day_low=20_900
    )
    carried = carry_to_next_day(pos)
    assert carried.day_open == 0
    assert carried.day_close == 0
    assert carried.day_low == 0


def test_두_이월_경로가_같은_변환을_쓴다():
    """'상태만 이월' 과 '전체 이월' 이 다르게 동작하면 안 된다."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "trader" / "core.py").read_text(
        encoding="utf-8"
    )
    assert source.count('carry_to_next_day(e["pos"])') == 2  # CarryPosition · CarryOver


# ── 사이클 합산 ───────────────────────────────────────────────


def _three_days(store: Store) -> None:
    """8/13 매수 → 8/14 3% 익절 → 8/18 잔량 청산 (그날 것만 담긴 새 방식)."""
    store.register_symbol(
        "2026-08-13",
        "352480",
        "씨앤씨",
        _P,
        Position(State.BUY1, 21_200, 9, 9, fees=28),
    )
    store.register_symbol(
        "2026-08-14",
        "352480",
        "씨앤씨",
        _P,
        Position(State.BUY1_TP1, 21_200, 9, 6, realized_pnl=1_050, fees=106),
    )
    store.register_symbol(
        "2026-08-18",
        "352480",
        "씨앤씨",
        _P,
        Position(State.CLOSED, 21_200, 9, 0, realized_pnl=0, fees=209),
    )


def test_일일_손익은_그날_것만_센다(store):
    """오늘 평단 그대로 팔았으면 오늘 벌이는 0 이다."""
    _three_days(store)
    today = {s["symbol"]: s for s in store.daily_report("2026-08-18")[0]}
    assert today["352480"]["realized_pnl"] == 0
    assert today["352480"]["fees"] == 209


def test_매매_전체는_합산해서_구한다(store):
    """이 매매로 번 돈은 +1,050 − 343 = +707 이다."""
    _three_days(store)
    realized, fees = store.cycle_totals("352480")
    assert (realized, fees) == (1_050, 343)


def test_매매일지도_사이클_합계를_보여준다(store):
    _three_days(store)
    entry = store.journal_entries()[0]
    assert entry["realized_pnl"] == 1_050
    assert entry["fees"] == 343


def test_다음_사이클은_따로_센다(store):
    """종료 후 재진입하면 새 매매다 — 앞 매매의 손익이 따라오면 안 된다."""
    _three_days(store)
    store.register_symbol(
        "2026-08-19",
        "352480",
        "씨앤씨",
        _P,
        Position(State.CLOSED, 20_000, 5, 0, realized_pnl=500, fees=50),
    )
    assert store.cycle_totals("352480") == (500, 50)


def test_사이클의_최고_최저는_극값이다(store):
    """며칠에 걸친 보유 구간 전체에서 가장 높았던 값·낮았던 값."""
    store.register_symbol(
        "2026-08-13",
        "352480",
        "씨앤씨",
        _P,
        Position(State.BUY1, 21_200, 9, 9, high_price=21_500, low_price=21_000),
    )
    store.register_symbol(
        "2026-08-14",
        "352480",
        "씨앤씨",
        _P,
        Position(State.CLOSED, 21_200, 9, 0, high_price=22_500, low_price=20_500),
    )
    entry = store.journal_entries()[0]
    assert entry["high_price"] == 22_500
    assert entry["low_price"] == 20_500


# ── 과거 기록 재계산 (이관 v12) ───────────────────────────────


def _old_style(path: str) -> None:
    """예전 방식으로 저장된 DB — 이월이 손익을 누적해 복사했다."""
    s = Store(path)
    s.register_symbol(
        "2026-08-13",
        "352480",
        "씨앤씨",
        _P,
        Position(State.BUY1, 21_200, 9, 9, fees=28),
    )
    s.register_symbol(
        "2026-08-14",
        "352480",
        "씨앤씨",
        _P,
        Position(State.BUY1_TP1, 21_200, 9, 6, realized_pnl=1_050, fees=134),
    )
    s.register_symbol(
        "2026-08-18",
        "352480",
        "씨앤씨",
        _P,
        Position(State.CLOSED, 21_200, 9, 0, realized_pnl=1_050, fees=343),
    )
    s.close()


def test_과거_기록을_날짜별로_되돌린다(tmp_path):
    path = str(tmp_path / "old.db")
    _old_style(path)

    conn = sqlite3.connect(path)
    _split_daily_pnl(conn)
    conn.commit()
    rows = dict(
        (r[0], (r[1], r[2]))
        for r in conn.execute("SELECT trade_date, realized_pnl, fees FROM positions")
    )
    conn.close()

    assert rows["2026-08-13"] == (0, 28)
    assert rows["2026-08-14"] == (1_050, 106)  # 134 − 28
    assert rows["2026-08-18"] == (0, 209)  # 어제 것을 뺀 그날 몫


def test_재계산해도_매매_전체_손익은_그대로다(tmp_path):
    """되돌리는 것이지 지우는 것이 아니다."""
    path = str(tmp_path / "old.db")
    _old_style(path)
    conn = sqlite3.connect(path)
    _split_daily_pnl(conn)
    conn.commit()
    total_realized, total_fees = conn.execute(
        "SELECT SUM(realized_pnl), SUM(fees) FROM positions"
    ).fetchone()
    conn.close()
    assert (total_realized, total_fees) == (1_050, 343)


def test_종료_뒤_새_매매는_기준을_다시_잡는다(tmp_path):
    """사이클이 끝나면 다음 행의 누적은 0 부터다."""
    path = str(tmp_path / "old.db")
    _old_style(path)
    s = Store(path)
    s.register_symbol(
        "2026-08-19",
        "352480",
        "씨앤씨",
        _P,
        Position(State.BUY1, 20_000, 5, 5, realized_pnl=0, fees=15),
    )
    s.close()
    conn = sqlite3.connect(path)
    _split_daily_pnl(conn)
    conn.commit()
    row = conn.execute(
        "SELECT realized_pnl, fees FROM positions WHERE trade_date='2026-08-19'"
    ).fetchone()
    conn.close()
    assert row == (0, 15)  # 앞 매매의 누적을 빼지 않는다


def test_다른_종목끼리_섞이지_않는다(tmp_path):
    path = str(tmp_path / "old.db")
    s = Store(path)
    for code, pnl, fee in (("111111", 5_000, 300), ("222222", 0, 20)):
        s.register_symbol(
            "2026-08-18",
            code,
            "종목",
            _P,
            Position(State.CLOSED, 10_000, 10, 0, realized_pnl=pnl, fees=fee),
        )
    s.close()
    conn = sqlite3.connect(path)
    _split_daily_pnl(conn)
    conn.commit()
    rows = dict(
        (r[0], (r[1], r[2]))
        for r in conn.execute("SELECT symbol, realized_pnl, fees FROM positions")
    )
    conn.close()
    assert rows["111111"] == (5_000, 300)
    assert rows["222222"] == (0, 20)


def test_이관_전에_DB_를_복사해_둔다(tmp_path):
    """값을 다시 계산하는 이관은 되돌릴 수 없다 — 돌아갈 곳을 남긴다."""
    from pathlib import Path

    path = str(tmp_path / "old.db")
    _old_style(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version=11")
    conn.commit()
    conn.close()

    Store(path).close()  # 열면서 이관된다
    assert Path(f"{path}.v11.bak").exists()


# ── 요약 종목별 줄은 사이클 전체 (2026-09-03) ───────────────────


def test_종료된_매매만_사이클_손익으로_넘긴다(tmp_path):
    """보유 중인 종목은 담지 않는다 — 오늘 실현분은 머리글 합계에 이미 있다."""
    from trader.core import Core
    from trader.state_machine import Decision, Params, Position, Side, State
    from trader.store import Store
    from trader.ui import bus

    core = Core(bus.Bus(), db_dir=str(tmp_path))
    core._store = Store(str(tmp_path / "t.db"))
    params = Params(
        line1=5_000, line2=4_500, line3=4_000, buy1_amount=500_000, buy2_amount=500_000
    )
    for date in ("2026-09-02", "2026-09-03"):
        for sym, name in (("005930", "삼성전자"), ("035420", "네이버")):
            core._store.register_symbol(date, sym, name, params)
    # 삼성전자: 어제 1차 익절 +4,000 → 오늘 전량 매도 +6,000 (종료)
    core._store.save_transition(
        "2026-09-02",
        "005930",
        State.BUY1,
        Position(State.BUY1_TP1, 5_000, 100, 60, realized_pnl=4_000, fees=100),
        Decision(State.BUY1_TP1, Side.SELL, 40, "1차 익절"),
        5_100,
        5_100,
    )
    core._store.save_transition(
        "2026-09-03",
        "005930",
        State.BUY1_TP1,
        Position(State.CLOSED, 5_000, 100, 0, realized_pnl=6_000, fees=150),
        Decision(State.CLOSED, Side.SELL, 60, "본절 이탈"),
        5_100,
        5_100,
    )
    # 네이버: 오늘 부분 익절만 하고 이월 (보유 중)
    core._store.save_transition(
        "2026-09-03",
        "035420",
        State.BUY1,
        Position(State.BUY1_TP1, 200_000, 10, 6, realized_pnl=1_500, fees=100),
        Decision(State.BUY1_TP1, Side.SELL, 4, "1차 익절"),
        206_000,
        206_000,
    )
    core._date = "2026-09-03"
    symbols, _fills = core._store.daily_report("2026-09-03")

    pnl = core._cycle_pnl("2026-09-03", symbols)

    assert set(pnl) == {"005930"}  # 종료된 것만
    assert pnl["005930"] == (10_000, 250)  # 어제 4,000 + 오늘 6,000
    core._store.close()


def test_요약이_사이클_손익을_실제로_받는다(tmp_path):
    """세 호출부 중 하나라도 빠뜨리면 그 경로만 조용히 하루치로 남는다."""
    import inspect

    from trader.core import Core

    source = inspect.getsource(Core)
    assert source.count("build_daily_summary_embed(") == source.count("cycle_pnl=")


def test_나중에_진입한_종목은_보류_목록에서_빠진다(tmp_path):
    """자리가 나서 실제로 들어갔으면 총량이 부족했던 것이 아니다."""
    from trader.state_machine import Decision, Params, Position, Side, State
    from trader.store import Store

    store = Store(tmp_path / "t.db")
    params = Params(
        line1=5_000, line2=4_500, line3=4_000, buy1_amount=500_000, buy2_amount=500_000
    )
    for sym, name in (("005930", "삼성전자"), ("035420", "네이버")):
        store.register_symbol("2026-09-02", sym, name, params)
        store.log("2026-09-02", sym, "보류", "최대 종목 수(7) 도달 — 자리가 나면")
    # 네이버만 나중에 자리가 나서 진입
    store.save_transition(
        "2026-09-02",
        "035420",
        State.WAITING,
        Position(State.BUY1, 5_000, 10, 10),
        Decision(State.BUY1, Side.BUY, 10, "1차 매수"),
        5_000,
        5_000,
    )

    blocked = store.blocked_symbols("2026-09-02")

    assert set(blocked) == {"005930"}
    assert "최대 종목 수" in blocked["005930"]
    store.close()


def test_요약이_보류_목록을_실제로_받는다():
    """세 호출부 중 하나라도 빠뜨리면 그 경로만 조용히 안 보인다."""
    import inspect

    from trader.core import Core

    source = inspect.getsource(Core)
    assert source.count("build_daily_summary_embed(") == source.count(
        "blocked=self._store"
    )
