"""매매일지 표시용 문자열 — 상태 경로와 진입·청산 시점.

순수 함수만 둔다 (DB·tkinter 무관). 코어와 UI 가 같은 문구를 쓰기 위한 공용 모듈이다.

상태 경로는 "이 매매가 어떤 길을 걸었나" 를 한 줄로 압축한 것이다. 표기 규칙:

- **'대기' 는 적지 않는다** — 모든 매매는 대기에서 시작하므로 정보가 없다.
- **'1차 매수 → 2차 매수' 는 '2차 매수' 로 줄인다** — 2차는 1차 뒤에만 오므로
  1차를 적지 않아도 읽는 데 지장이 없다. (2선 이하 갭으로 1·2차를 동시에 산 경우도
  결과가 같아 구분되지 않지만, 복기에 필요한 정보는 '2차까지 샀다' 는 사실이다.)
- **'종료' 라는 말은 쓰지 않고 끝난 이유를 적는다** — '종료' 만으로는 7% 익절로 끝난
  것인지 본절로 밀린 것인지 알 수 없다. 마지막 칸에 사유를 그대로 둔다.

예) `2차 매수 → 3% 익절 → 5% 익절 → 7% 익절`
    `1차 매수 → 3% 익절 → 본절 이탈`
    `2차 매수 → 손절`
"""

from __future__ import annotations

import re

from trader.trading_calendar import TradingCalendar, format_days

_WAITING = "대기"
_CLOSED = "종료"
_BUY1 = "1차 매수"
_BUY2 = "2차 매수"

# 종료 사유 판정 — 사유 문구에 이 낱말이 있으면 그 라벨로 적는다.
# **순서가 중요하다**: "본절 이탈 → 잔량 전량 청산" 과 "사용자 판단 → 수동 전량 청산" 은
# 둘 다 '전량 청산' 을 품고 있어, 구체적인 것부터 먼저 검사해야 한다.
_CLOSE_REASONS: tuple[tuple[str, str], ...] = (
    ("진입 금지", "진입 금지"),  # 3선 이하 갭 시가 — 사지도 않고 끝난 경우
    ("수동", "수동 청산"),
    ("손절", "손절"),
    ("본절", "본절 이탈"),
    ("장 마감", "장 마감 청산"),
)


def close_label(reason: str) -> str:
    """종료 전이의 사유 → 짧은 라벨.

    익절 청산은 익절률이 설정값(tp_rates)이라 문구에서 숫자를 뽑는다 —
    "1차 평단 +7% 도달 → 전량 청산" 처럼 온다. 설정을 바꿔도 라벨이 따라간다.
    """
    for keyword, label in _CLOSE_REASONS:
        if keyword in reason:
            return label
    if match := re.search(r"\+(\d+(?:\.\d+)?)%", reason):
        return f"{match.group(1)}% 익절"
    if "전량 청산" in reason:
        return "전량 청산"
    return reason.split("→")[-1].strip() or _CLOSED


def state_label(to_state: str, reason: str) -> str:
    """전이의 도착 상태 → 경로에 적을 칸 이름."""
    if to_state == _CLOSED:
        return close_label(reason)
    # '1차 매수 + 3% 익절' → '3% 익절' (몇 차 매수인지는 앞 칸에 이미 있다)
    return to_state.split(" + ")[-1].strip()


def transition_path(transitions: list[dict]) -> str:
    """전이 목록 → `2차 매수 → 3% 익절 → 본절 이탈` 형태의 한 줄."""
    steps: list[str] = []
    for row in transitions:
        to_state = row.get("to_state") or ""
        if not to_state or to_state == _WAITING:
            continue  # 대기로 되돌아가는 전이는 없지만 방어적으로
        label = state_label(to_state, row.get("reason") or "")
        if label == _BUY2 and steps and steps[-1] == _BUY1:
            steps[-1] = _BUY2  # 1차를 2차가 흡수한다
            continue
        if steps and steps[-1] == label:
            continue  # 같은 칸이 연달아 나오면 한 번만 (수량 0 익절 전이 등)
        steps.append(label)
    return " → ".join(steps)


def cycle_timeline(
    transitions: list[dict],
    base_date: str = "",
    calendar: TradingCalendar | None = None,
) -> str:
    """진입·청산 날짜와 기준봉 대비 경과 거래일.

    예) `진입 2026-08-07 (D+1) · 청산 2026-08-12 (D+4)`
    기준봉이 없으면 날짜만 적는다. 아직 보유 중이면 청산 부분이 빠진다.
    """
    fills = [r for r in transitions if r.get("side")]
    buys = [r for r in fills if r["side"] == "매수"]
    closed = [r for r in transitions if (r.get("to_state") or "") == _CLOSED]
    parts: list[str] = []
    for label, rows in (("진입", buys[:1]), ("청산", closed[-1:])):
        if not rows:
            continue
        date = rows[0].get("trade_date") or (rows[0].get("ts") or "")[:10]
        if not date:
            continue
        text = f"{label} {date}"
        if base_date and calendar is not None:
            if days := format_days(calendar.days_between(base_date, date)):
                text += f" ({days})"
        parts.append(text)
    return " · ".join(parts)
