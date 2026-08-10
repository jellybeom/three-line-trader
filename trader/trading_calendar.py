"""거래일 달력 — 기준봉으로부터 며칠째인지를 **실제 장이 열린 날**로 센다.

주말만 빼면 공휴일이 그대로 포함돼 숫자가 어긋난다. 한국 공휴일은 대체휴일·임시공휴일
때문에 규칙만으로 정확히 계산할 수 없으므로, **키움 지수(KOSPI) 일봉의 날짜 목록**을
거래일 달력으로 쓴다. 지수 일봉은 장이 열린 날에만 존재하기 때문이다.

달력을 구하지 못하는 상황(키움 미연결, 조회 실패)에서는 주말만 제외한 근사값으로
물러난다 — 값이 없는 것보다 대략이라도 보여주는 편이 낫고, 연결되면 곧 정확해진다.
"""

from __future__ import annotations

from datetime import date, timedelta


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

    def __init__(self, days: list[str] | None = None):
        self._days: list[str] = []
        self._index: dict[str, int] = {}
        self.replace(days or [])

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
