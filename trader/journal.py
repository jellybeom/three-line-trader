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

import datetime as _dt
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


def entry_time(transitions: list[dict]) -> str:
    """사이클의 **첫 매수 체결 시각** (YYYY-MM-DD HH:MM:SS). 없으면 빈 문자열.

    2차 매수(물타기)는 새 진입이 아니므로 시계를 되돌리지 않는다 — 언제나 첫 매수다.
    체결통보 유실로 강제 복구된 포지션이나 등록 창에서 상태를 직접 지정한 오버나이트
    종목은 매수 전이 행이 없어 빈 문자열이 된다. 추정하지 않는다.
    """
    for row in transitions:
        if row.get("side") == "매수":
            return row.get("ts") or ""
    return ""


def format_holding(
    entry_ts: str, until_ts: str = "", hold_days: int | None = None
) -> str:
    """보유기간 표기.

    시계가 두 개다. 벽시계 시간은 장중에는 정직하지만 밤·주말을 넘기면 거짓말이 된다
    (금 15:20 매수 → 월 09:05 은 벽시계로 66시간이지만 실제 장중 노출은 6시간 남짓).
    그래서 **당일과 이월을 다른 단위로** 적는다.

        47분 · 3시간 12분 · 3일차

    'N일차' 는 **거래일** 기준이며 진입일이 1일차다(주말·휴장 제외). 옆 열의 기준봉이
    이미 `D+n` 이라 같은 표기를 쓰면 눈으로 구분되지 않아 형태를 달리했다.
    hold_days 는 거래일 달력이 계산해 넘겨준 값이고, 없으면 날짜 차이로 물러난다.
    """
    if not entry_ts:
        return ""
    start = _parse_ts(entry_ts)
    end = _parse_ts(until_ts) if until_ts else _dt.datetime.now()
    if start is None or end is None or end < start:
        return ""
    if hold_days is None:
        hold_days = (end.date() - start.date()).days
    if hold_days > 0:  # 날짜를 넘겼다 — 시간 단위는 의미를 잃는다
        return f"{hold_days + 1}일차"
    minutes = int((end - start).total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}분"
    return f"{minutes // 60}시간 {minutes % 60}분"


def _parse_ts(value: str) -> "_dt.datetime | None":
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return _dt.datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            continue
    return None


def cycle_holding(
    transitions: list[dict], calendar: TradingCalendar | None = None
) -> str:
    """사이클 전체의 보유기간. 청산했으면 진입~청산, 보유 중이면 지금까지."""
    entry = entry_time(transitions)
    if not entry:
        return ""
    closed = [r for r in transitions if (r.get("to_state") or "") == _CLOSED]
    until = (closed[-1].get("ts") or "") if closed else ""
    last_day = until[:10] if until else _dt.date.today().isoformat()
    days = calendar.days_between(entry[:10], last_day) if calendar is not None else None
    return format_holding(entry, until, days)


def compact_timeline(
    transitions: list[dict],
    base_date: str = "",
    calendar: TradingCalendar | None = None,
) -> str:
    """Discord 스레드용 한 줄. `진입 09:05 · 청산 09:41 · 기준봉 08-19 (D+5)`

    cycle_timeline 과 달리 **보유기간을 넣지 않는다** — embed 의 가격대 줄에 이미 있어
    중복된다. 하루 안에 끝난 매매는 시각만, 날짜를 넘긴 매매는 날짜까지 적는다.
    폰에서 한 줄에 들어가야 하므로 연도는 생략한다.
    """
    buys = [r for r in transitions if r.get("side") == "매수"]
    closed = [r for r in transitions if (r.get("to_state") or "") == _CLOSED]
    if not buys:
        return ""
    entry, exit_ts = buys[0].get("ts") or "", (
        (closed[-1].get("ts") or "") if closed else ""
    )
    same_day = bool(entry and exit_ts and entry[:10] == exit_ts[:10])

    def stamp(ts: str) -> str:
        if len(ts) < 16:
            return ts[:10]
        return ts[11:16] if same_day else f"{ts[5:10]} {ts[11:16]}"

    parts = [f"진입 {stamp(entry)}"] if entry else []
    if exit_ts:
        parts.append(f"청산 {stamp(exit_ts)}")
    if base_date:
        text = f"기준봉 {base_date[5:]}"
        if calendar is not None and entry:
            if days := format_days(calendar.days_between(base_date, entry[:10])):
                text += f" ({days})"
        parts.append(text)
    return " · ".join(parts)


def cycle_timeline(
    transitions: list[dict],
    base_date: str = "",
    calendar: TradingCalendar | None = None,
) -> str:
    """진입·청산 날짜, 기준봉 대비 경과 거래일, 보유기간.

    예) `진입 2026-08-07 09:14 (D+1) · 청산 2026-08-12 13:02 (D+4) · 보유 4일차`
    기준봉이 없으면 날짜만 적는다. 아직 보유 중이면 청산 부분이 빠진다.
    진입 시각까지 적는 이유는, 표의 '보유' 열이 `3시간 12분` 처럼 경과만 보여 주기
    때문이다 — 언제 들어갔는지는 여기서 확인한다.
    """
    fills = [r for r in transitions if r.get("side")]
    buys = [r for r in fills if r["side"] == "매수"]
    closed = [r for r in transitions if (r.get("to_state") or "") == _CLOSED]
    parts: list[str] = []
    for label, rows in (("진입", buys[:1]), ("청산", closed[-1:])):
        if not rows:
            continue
        ts = rows[0].get("ts") or ""
        date = rows[0].get("trade_date") or ts[:10]
        if not date:
            continue
        text = f"{label} {date}"
        if len(ts) >= 16 and ts[:10] == date:  # 같은 날의 기록일 때만 시각을 덧붙인다
            text += f" {ts[11:16]}"
        if base_date and calendar is not None:
            if days := format_days(calendar.days_between(base_date, date)):
                text += f" ({days})"
        parts.append(text)
    if held := cycle_holding(transitions, calendar):
        parts.append(f"보유 {held}")
    return " · ".join(parts)
