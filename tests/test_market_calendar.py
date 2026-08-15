"""증시 개장일 판정 — 휴장일 목록(holidays.csv) + 지수 일봉.

2026-08-17(광복절 대체휴일)처럼 **주말이 아닌 휴장일**이 문제였다. 주말만 건너뛰던
이월 날짜가 그날로 가서, 장이 없는 날에 종목이 놓이고 정작 개장일에는 비어 있었다.

판정에서 가장 중요한 원칙은 **의심스러우면 여는 쪽**이다. 휴장일에 감시를 켜도 체결
틱이 없어 손해가 없지만, 개장일에 안 켜면 하루 매매를 통째로 놓친다.
"""

from __future__ import annotations

import datetime as dt

import pytest

from trader.trading_calendar import (
    CLOSED,
    OPEN,
    UNKNOWN,
    MarketDay,
    TradingCalendar,
    load_holidays,
)

_2026 = {
    "2026-01-01": "신정",
    "2026-05-01": "근로자의날",
    "2026-08-17": "광복절(대체휴일)",
    "2026-09-24": "추석",
    "2026-09-25": "추석",
    "2026-12-31": "연말휴장일",
}


@pytest.fixture
def calendar() -> TradingCalendar:
    return TradingCalendar(holidays=dict(_2026))


# ── 판정 ──────────────────────────────────────────────────────


def test_주말은_목록_없이도_휴장이다():
    plain = TradingCalendar()
    assert plain.market_day("2026-08-15").status == CLOSED  # 토
    assert plain.market_day("2026-08-16").status == CLOSED  # 일
    assert plain.market_day("2026-08-15").note == "주말"


def test_목록에_있는_평일은_휴장이다(calendar):
    day = calendar.market_day("2026-08-17")  # 월요일
    assert day.status == CLOSED
    assert day.note == "광복절(대체휴일)"
    assert day.is_closed


def test_공휴일이_아닌_휴장일도_잡는다(calendar):
    """근로자의날·연말휴장일은 법정공휴일이 아니다 — 공휴일 API 로는 못 잡는다."""
    assert calendar.market_day("2026-05-01").note == "근로자의날"
    assert calendar.market_day("2026-12-31").note == "연말휴장일"


def test_목록에_없는_평일은_개장이다(calendar):
    """그 해 목록이 있으면 목록에 없는 평일은 장이 열린 날이다."""
    day = calendar.market_day("2026-08-18")
    assert day.status == OPEN
    assert day.is_open
    assert not day.is_closed


def test_목록이_없는_연도는_확인_불가다(calendar):
    """2027 목록을 아직 안 받았다면 함부로 휴장이라고 하면 안 된다."""
    day = calendar.market_day("2027-03-02")
    assert day.status == UNKNOWN
    assert not day.is_closed  # 게이트가 감시를 막지 않는다


def test_목록이_없으면_지수_일봉으로_판정한다():
    """휴장일 목록이 아직 없어도 과거는 지수 일봉으로 알 수 있다."""
    calendar = TradingCalendar(["2026-08-13", "2026-08-14", "2026-08-18"])
    assert calendar.market_day("2026-08-14").status == OPEN
    assert calendar.market_day("2026-08-17").status == CLOSED  # 일봉에 없다
    assert calendar.market_day("2026-08-17").note == ""  # 사유는 모른다


def test_목록이_지수_일봉보다_우선한다():
    """목록이 있으면 그 해 **아무 날짜나** 답할 수 있다 — 일봉은 최근 180일뿐이다."""
    calendar = TradingCalendar(["2026-08-13", "2026-08-14"], holidays=dict(_2026))
    assert calendar.market_day("2026-01-01").status == CLOSED  # 일봉 범위 밖
    assert calendar.market_day("2026-11-02").status == OPEN  # 미래


def test_이상한_날짜는_확인_불가다(calendar):
    assert calendar.market_day("").status == UNKNOWN
    assert calendar.market_day("2026-13-99").status == UNKNOWN


# ── 목록이 낡았는지 ───────────────────────────────────────────


def test_목록과_실제_개장일이_다르면_알아챈다():
    """임시공휴일이 취소됐는데 목록을 안 고친 경우 — 조용히 두면 감시를 안 켠다."""
    calendar = TradingCalendar(["2026-08-17"], holidays={"2026-08-17": "광복절"})
    assert calendar.conflicts() == ["2026-08-17"]


def test_어긋난_곳이_없으면_조용하다(calendar):
    calendar.replace(["2026-08-13", "2026-08-14", "2026-08-18"])
    assert calendar.conflicts() == []


# ── 다음 개장일 ───────────────────────────────────────────────


def test_대체휴일을_건너뛴다(calendar):
    """금요일 보유 종목을 이월하면 휴장일(월)이 아니라 화요일로 가야 한다."""
    assert calendar.next_open_day("2026-08-14") == "2026-08-18"


def test_연휴_전체를_건너뛴다(calendar):
    """추석 목~금 + 주말 → 그다음 월요일."""
    assert calendar.next_open_day("2026-09-23") == "2026-09-28"


def test_평일_다음날은_그대로다(calendar):
    assert calendar.next_open_day("2026-08-18") == "2026-08-19"


def test_목록이_없으면_주말만_건너뛴다():
    """예전 동작으로 물러난다 — 모르는 채로 며칠씩 건너뛰면 더 위험하다."""
    plain = TradingCalendar()
    assert plain.next_open_day("2026-08-14") == "2026-08-17"  # 월요일


def test_무한_루프에_빠지지_않는다():
    """목록이 이상해도 반드시 멈춘다."""
    everything = {
        (dt.date(2026, 8, 17) + dt.timedelta(days=i)).isoformat(): "이상한목록"
        for i in range(60)
    }
    calendar = TradingCalendar(holidays=everything)
    assert calendar.next_open_day("2026-08-17")  # 값을 돌려주고 멈춘다


# ── 파일 읽기 ─────────────────────────────────────────────────


def test_실제_파일을_읽는다():
    """루트의 holidays.csv — git 으로 관리해 노트북에서도 같이 쓴다."""
    holidays = load_holidays()
    assert holidays.get("2026-08-17") == "광복절(대체휴일)"
    assert "2026-05-01" in holidays  # 근로자의날


def test_주석과_빈_줄을_건너뛴다(tmp_path):
    path = tmp_path / "h.csv"
    path.write_text(
        "# 주석\n\ndate,note\n2026-08-17,광복절(대체휴일)\n", encoding="utf-8"
    )
    assert load_holidays(path) == {"2026-08-17": "광복절(대체휴일)"}


def test_파일이_없어도_터지지_않는다(tmp_path):
    """목록이 없다고 매매가 멈추면 안 된다."""
    assert load_holidays(tmp_path / "없는파일.csv") == {}


def test_망가진_줄은_버리고_나머지를_읽는다(tmp_path):
    path = tmp_path / "h.csv"
    path.write_text(
        "date,note\n2026-08-17,광복절\n이상한줄\n,사유없음\n2026-12-25,성탄절\n",
        encoding="utf-8",
    )
    holidays = load_holidays(path)
    assert set(holidays) == {"2026-08-17", "2026-12-25"}


def test_사유가_비어도_휴장으로_본다(tmp_path):
    path = tmp_path / "h.csv"
    path.write_text("date,note\n2026-08-17,\n", encoding="utf-8")
    calendar = TradingCalendar(holidays=load_holidays(path))
    assert calendar.market_day("2026-08-17").status == CLOSED


# ── 표시 문구 ─────────────────────────────────────────────────


def test_표시_문구는_괄호를_겹치지_않는다():
    """사유에 이미 괄호가 있다 — 다시 감싸면 '휴장 (광복절(대체휴일))' 이 된다."""
    assert MarketDay(CLOSED, "광복절(대체휴일)").label() == "휴장 · 광복절(대체휴일)"
    assert MarketDay(OPEN).label() == "개장"


# ── 자동 시작 게이트 ──────────────────────────────────────────


def _core(calendar: TradingCalendar):
    """스케줄 판정만 돌려보기 위한 최소 코어."""
    import datetime as dtm

    from trader.core import Core

    core = Core.__new__(Core)
    core._calendar = calendar
    core._schedule = {
        "enabled": True,
        "start": dtm.time(8, 55),
        "stop": dtm.time(15, 30),
        "summary": dtm.time(15, 35),
    }
    core._sched_done = {}
    core.logs = []
    core._log = lambda sym, kind, text, notify=True: core.logs.append((kind, text))
    core._sched_last = lambda key: core._sched_done.get(key, "")
    core._sched_mark = lambda key, day: core._sched_done.__setitem__(key, day)
    core.started = []
    core._running = False
    return core


async def _run_schedule(core, when: dt.datetime):
    """_check_schedule 을 특정 시각으로 돌린다."""
    import trader.core as core_mod

    real = core_mod.datetime

    class _Fixed(real):
        @classmethod
        def now(cls, tz=None):
            return when

    core_mod.datetime = _Fixed

    async def _auto_start(today):  # _check_schedule 이 await 한다
        core.started.append(today)

    core._auto_start = _auto_start
    try:
        await core._check_schedule()
    finally:
        core_mod.datetime = real


def test_휴장일에는_감시를_시작하지_않는다(calendar):
    import asyncio

    core = _core(calendar)
    asyncio.run(_run_schedule(core, dt.datetime(2026, 8, 17, 9, 0)))
    assert core.started == []
    assert any("광복절" in text for _kind, text in core.logs)


def test_개장일에는_평소대로_시작한다(calendar):
    import asyncio

    core = _core(calendar)
    asyncio.run(_run_schedule(core, dt.datetime(2026, 8, 18, 9, 0)))
    assert core.started == ["2026-08-18"]


def test_확인_불가면_시작한다(calendar):
    """목록을 아직 안 받은 해 — 개장일에 안 켜는 쪽이 훨씬 큰 손해다."""
    import asyncio

    core = _core(calendar)
    asyncio.run(_run_schedule(core, dt.datetime(2027, 3, 2, 9, 0)))
    assert core.started == ["2027-03-02"]


def test_목록이_아예_없어도_시작한다():
    """holidays.csv 가 없거나 깨졌을 때 매매가 멈추면 안 된다."""
    import asyncio

    core = _core(TradingCalendar())
    asyncio.run(_run_schedule(core, dt.datetime(2026, 8, 17, 9, 0)))
    assert core.started == ["2026-08-17"]  # 주말이 아니므로 켠다


def test_주말에는_시작하지_않는다(calendar):
    import asyncio

    core = _core(calendar)
    asyncio.run(_run_schedule(core, dt.datetime(2026, 8, 15, 9, 0)))
    assert core.started == []


def test_휴장_안내는_하루에_한_번만(calendar):
    """5초마다 도는 판정이라 그대로 두면 로그가 하루 종일 쌓인다."""
    import asyncio

    core = _core(calendar)
    for _ in range(5):
        asyncio.run(_run_schedule(core, dt.datetime(2026, 8, 17, 9, 0)))
    assert len([t for _k, t in core.logs if "광복절" in t]) == 1


# ── 이월 날짜 ─────────────────────────────────────────────────


def test_코어의_다음_매매일이_휴장일을_건너뛴다(calendar):
    """금요일 보유 종목을 이월하면 대체휴일(월)이 아니라 화요일로 가야 한다.

    달력만 맞아도 코어가 그것을 쓰지 않으면 소용없다 — 연결을 직접 확인한다.
    """
    from trader.core import Core

    core = Core.__new__(Core)
    core._calendar = calendar
    core._date = "2026-08-14"
    assert core._next_trade_date() == "2026-08-18"

    core._date = "2026-09-23"  # 추석 연휴 앞
    assert core._next_trade_date() == "2026-09-28"


def test_목록이_없으면_코어도_주말만_건너뛴다():
    from trader.core import Core

    core = Core.__new__(Core)
    core._calendar = TradingCalendar()
    core._date = "2026-08-14"
    assert core._next_trade_date() == "2026-08-17"
