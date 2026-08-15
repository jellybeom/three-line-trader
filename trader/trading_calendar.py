"""거래일 달력 — 기준봉으로부터 며칠째인지를 **실제 장이 열린 날**로 센다.

주말만 빼면 공휴일이 그대로 포함돼 숫자가 어긋난다. 한국 공휴일은 대체휴일·임시공휴일
때문에 규칙만으로 정확히 계산할 수 없으므로, **키움 지수(KOSPI) 일봉의 날짜 목록**을
거래일 달력으로 쓴다. 지수 일봉은 장이 열린 날에만 존재하기 때문이다.

달력을 구하지 못하는 상황(키움 미연결, 조회 실패)에서는 주말만 제외한 근사값으로
물러난다 — 값이 없는 것보다 대략이라도 보여주는 편이 낫고, 연결되면 곧 정확해진다.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

HOLIDAYS_FILE = Path(__file__).resolve().parents[1] / "holidays.csv"

OPEN = "개장"
CLOSED = "휴장"
UNKNOWN = "확인 불가"
WEEKEND = "주말"


@dataclass(frozen=True)
class MarketDay:
    """어떤 날짜가 개장일인지 — 사유까지 함께."""

    status: str  # OPEN / CLOSED / UNKNOWN
    note: str = ""  # 휴장 사유 (주말, 광복절(대체휴일) ...)

    @property
    def is_open(self) -> bool:
        return self.status == OPEN

    @property
    def is_closed(self) -> bool:
        """**확실히** 휴장인가. 확인 불가는 False — 의심스러우면 여는 쪽으로 둔다."""
        return self.status == CLOSED

    def label(self) -> str:
        """'휴장 · 광복절(대체휴일)' — 사유에 이미 괄호가 있어 다시 감싸지 않는다."""
        return f"{self.status} · {self.note}" if self.note else self.status


def load_holidays(path: Path | str | None = None) -> dict[str, str]:
    """휴장일 목록 (YYYY-MM-DD → 사유). 파일이 없거나 깨지면 빈 목록.

    읽기에 실패해도 예외를 올리지 않는다 — 이 목록이 없다고 매매가 멈추면 안 된다.
    목록이 비면 '확인 불가' 로 판정되고, 자동 시작 게이트는 평소대로 감시를 켠다.
    """
    target = Path(path) if path else HOLIDAYS_FILE
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return {}
    rows = [line for line in text.splitlines() if line.strip() and line[0] != "#"]
    holidays: dict[str, str] = {}
    for row in csv.DictReader(rows):
        day = (row.get("date") or "").strip()
        if _to_date(day) is not None:
            holidays[day] = (row.get("note") or "").strip()
    return holidays


def _to_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def weekday_days_between(base: str, target: str) -> int | None:
    """주말만 제외한 경과 영업일 (달력이 없을 때의 근사값)."""
    start, end = _to_date(base), _to_date(target)
    if start is None or end is None:
        return None
    step = 1 if end >= start else -1
    days, cursor = 0, start
    while cursor != end:
        cursor += timedelta(days=step)
        if cursor.weekday() < 5:  # 토·일 제외
            days += step
    return days


class TradingCalendar:
    """실제 거래일 목록. 비어 있으면 주말 기준 근사로 동작한다."""

    def __init__(
        self, days: list[str] | None = None, holidays: dict[str, str] | None = None
    ):
        self._days: list[str] = []
        self._index: dict[str, int] = {}
        self._holidays: dict[str, str] = dict(holidays or {})
        self._holiday_years: set[str] = {d[:4] for d in self._holidays}
        self.replace(days or [])

    def set_holidays(self, holidays: dict[str, str]) -> None:
        self._holidays = dict(holidays)
        self._holiday_years = {d[:4] for d in self._holidays}

    @property
    def holiday_years(self) -> set[str]:
        return set(self._holiday_years)

    def market_day(self, day: str) -> MarketDay:
        """그 날짜가 개장일인지 — 사유까지.

        판정 순서는 **확실한 것부터**다.
        1. 토·일 → 휴장(주말)
        2. 그 해 휴장일 목록이 있으면 → 목록에 있으면 휴장(사유), 없으면 개장
        3. 목록이 없으면 → 지수 일봉 범위 안일 때만 그것으로 판정 (사유는 모른다)
        4. 둘 다 없으면 → 확인 불가

        목록을 지수 일봉보다 먼저 보는 이유는, 목록이 있으면 그 해 **아무 날짜나**
        답할 수 있기 때문이다. 지수 일봉은 최근 180일뿐이고 미래는 아예 없다.
        """
        parsed = _to_date(day)
        if parsed is None:
            return MarketDay(UNKNOWN)
        if parsed.weekday() >= 5:
            return MarketDay(CLOSED, WEEKEND)
        if day[:4] in self._holiday_years:
            if day in self._holidays:
                return MarketDay(CLOSED, self._holidays[day] or "휴장일")
            return MarketDay(OPEN)
        if self.covers(day):
            return MarketDay(OPEN if day in self._index else CLOSED)
        return MarketDay(UNKNOWN)

    def conflicts(self) -> list[str]:
        """목록은 휴장이라는데 지수 일봉에는 장이 열린 날 — 목록이 낡았다는 신호.

        임시공휴일이 추가되거나 취소됐는데 목록을 갱신하지 않은 경우를 잡는다.
        """
        return sorted(d for d in self._index if d in self._holidays)

    def next_open_day(self, day: str, limit: int = 14) -> str:
        """day 다음의 개장일. 확실히 휴장인 날만 건너뛴다.

        limit 은 무한 루프 방지용이다 — 목록이 이상해도 2주 안에는 반드시 멈춘다.
        """
        current = _to_date(day)
        if current is None:
            return day
        for _ in range(limit):
            current += timedelta(days=1)
            if not self.market_day(current.isoformat()).is_closed:
                break
        return current.isoformat()

    def replace(self, days: list[str]) -> None:
        """거래일 목록 교체 (YYYY-MM-DD, 순서 무관 — 정렬해서 보관)."""
        clean = sorted({d for d in days if _to_date(d) is not None})
        self._days = clean
        self._index = {d: i for i, d in enumerate(clean)}

    @property
    def days(self) -> list[str]:
        return list(self._days)

    def __len__(self) -> int:
        return len(self._days)

    def covers(self, day: str) -> bool:
        """달력 범위 안의 날짜인지 (범위를 벗어나면 근사로 물러나야 한다)."""
        return bool(self._days) and self._days[0] <= day <= self._days[-1]

    def _position(self, day: str) -> int | None:
        """거래일이면 그 인덱스, 휴장일이면 **직전 거래일**의 인덱스."""
        if day in self._index:
            return self._index[day]
        if not self.covers(day):
            return None
        lo, hi = 0, len(self._days) - 1  # 이진 탐색으로 직전 거래일 찾기
        found = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if self._days[mid] <= day:
                found, lo = mid, mid + 1
            else:
                hi = mid - 1
        return found

    def days_between(self, base: str, target: str) -> int | None:
        """base 에서 target 까지 몇 번째 거래일인지. 같은 날이면 0.

        달력이 두 날짜를 모두 담지 못하면 주말 기준 근사로 물러난다.
        """
        if not (base and target):
            return None
        start, end = self._position(base), self._position(target)
        if start is None or end is None:
            return weekday_days_between(base, target)
        return end - start


def format_days(days: int | None) -> str:
    """경과 거래일 → 'D+2' / 'D0' / 'D-1'. 값이 없으면 빈 문자열."""
    if days is None:
        return ""
    return f"D{days:+d}" if days else "D0"
