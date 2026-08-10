"""거래일 달력 — 공휴일까지 반영한 경과일 계산.

주말만 제외하면 광복절·대체휴일 같은 날이 그대로 세어져 숫자가 어긋난다.
실제 거래일 목록(키움 지수 일봉의 날짜)을 기준으로 세는 것이 이 모듈의 목적이다.
"""

from trader.trading_calendar import (
    TradingCalendar,
    format_days,
    weekday_days_between,
)

# 2026-08-15(광복절, 토) → 08-17(월) 대체휴일이라 8/17 이 빠진 거래일 목록
DAYS = [
    "2026-08-05",
    "2026-08-06",
    "2026-08-07",
    "2026-08-10",
    "2026-08-11",
    "2026-08-12",
    "2026-08-13",
    "2026-08-14",
    "2026-08-18",
    "2026-08-19",
]


def test_공휴일을_빼고_거래일로_센다():
    cal = TradingCalendar(DAYS)
    assert cal.days_between("2026-08-14", "2026-08-18") == 1  # 대체휴일 제외
    assert weekday_days_between("2026-08-14", "2026-08-18") == 2  # 주말만 빼면 2


def test_주말도_당연히_빠진다():
    cal = TradingCalendar(DAYS)
    assert cal.days_between("2026-08-07", "2026-08-10") == 1  # 금 → 월


def test_같은_날은_0():
    assert TradingCalendar(DAYS).days_between("2026-08-10", "2026-08-10") == 0


def test_휴장일이_기준봉이면_직전_거래일로_본다():
    """기준봉 날짜를 휴장일로 잘못 입력해도 값이 나와야 한다."""
    cal = TradingCalendar(DAYS)
    assert (
        cal.days_between("2026-08-08", "2026-08-10") == 1
    )  # 토요일 → 직전 금요일 기준


def test_달력_범위_밖이면_주말_기준_근사로_물러난다():
    cal = TradingCalendar(DAYS)
    approx = cal.days_between("2026-07-01", "2026-08-10")
    assert approx == weekday_days_between("2026-07-01", "2026-08-10")


def test_달력이_없어도_동작한다():
    """키움 미연결 상태에서도 대략의 값은 보여준다."""
    cal = TradingCalendar()
    assert cal.days_between("2026-08-07", "2026-08-10") == 1


def test_과거_날짜는_음수():
    cal = TradingCalendar(DAYS)
    assert cal.days_between("2026-08-12", "2026-08-10") == -2


def test_잘못된_날짜는_None():
    cal = TradingCalendar(DAYS)
    assert cal.days_between("", "2026-08-10") is None
    assert cal.days_between("이상한값", "2026-08-10") is None


def test_표시_형식():
    assert format_days(2) == "D+2"
    assert format_days(0) == "D0"
    assert format_days(-1) == "D-1"
    assert format_days(None) == ""


def test_목록은_정렬되고_중복이_제거된다():
    cal = TradingCalendar(["2026-08-10", "2026-08-05", "2026-08-10", "이상한값"])
    assert cal.days == ["2026-08-05", "2026-08-10"]
    assert len(cal) == 2
