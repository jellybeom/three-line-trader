"""Discord 알림 (notifier) — webhook 발송 + 알림 수준 필터.

- 발송은 blocking(requests)이므로 코어에서는 asyncio.to_thread,
  시뮬레이터에서는 백그라운드 스레드로 호출한다 (매매 루프를 막지 않음).
- 알림 수준(UI Discord 그룹의 콤보, settings 영속):
    전체              → 모든 로그
    매매만 (시스템 제외) → 종목 관련 로그만 (symbol != "시스템")
    에러만            → 에러 / 경고만
    끔                → 발송 안 함
- Discord webhook 은 분당 약 30건 제한이 있다. '전체' 수준에서 로그가 많은 날은
  일부가 지연·거부될 수 있으니 평소엔 '매매만' 을 권장.
"""

from __future__ import annotations

import threading
import time
import tomllib
from pathlib import Path

import requests


class NotifierError(RuntimeError):
    """webhook 미설정 또는 발송 실패."""


def load_webhook(config_path: str | Path = "config.toml") -> str:
    path = Path(config_path)
    if not path.exists():
        raise NotifierError(f"{config_path} 가 없습니다.")
    url = (
        tomllib.loads(path.read_text(encoding="utf-8"))
        .get("discord", {})
        .get("webhook_url", "")
    )
    if not url:
        raise NotifierError("config.toml 의 [discord] webhook_url 이 비어 있습니다.")
    return url


def should_notify(level: str, symbol: str, kind: str) -> bool:
    """알림 수준 필터 — 로그 한 줄을 Discord 로 보낼지 결정한다."""
    if level == "끔":
        return False
    if level == "에러만":
        return kind in ("에러", "경고")
    if level.startswith("매매만"):
        return symbol != "시스템"
    return True  # 전체


def format_trade(
    name: str,
    symbol: str,
    reason: str,
    qty: int,
    price: float,
    closed_pnl: float | None = None,
) -> str:
    """매매 알림 전용 요약 — 과정("1선 이탈" 등)은 빼고 결과만 담는다.

    예) 🟢 **삼성전자(005930)** 1차 매수 — 38주 @ 13,170
        💰 **삼성전자(005930)** 2차 익절 — 50주 @ 72,400
        🛑 **흥구석유(024060)** 전량 손절 — 76주 @ 13,068  ␤  실현손익 **-4,750원**
    """
    result = reason.split("→")[-1].strip()  # Decision.reason 의 "과정 → 결과" 중 결과부
    if "손절" in result:
        icon = "🛑"
    elif "익절" in result or "본절" in result or "청산" in result:
        icon = "💰"
    elif "매수" in result:
        icon = "🟢"
    else:
        icon = "⚪"  # 진입 금지 종료 등
    msg = f"{icon} **{name}({symbol})** {result}"
    if qty > 0:
        msg += f" — {qty}주 @ {price:,.0f}"
    if closed_pnl is not None:
        msg += f"\n실현손익 **{closed_pnl:+,.0f}원**"
    return msg


def _holding_time(fills: list[dict]) -> str:
    """첫 체결 ~ 마지막 체결 경과 시간 (요약 표시용)."""
    if len(fills) < 2:
        return ""
    from datetime import datetime

    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        start = datetime.strptime(fills[0]["ts"], fmt)
        end = datetime.strptime(fills[-1]["ts"], fmt)
    except (ValueError, KeyError):
        return ""
    minutes = int((end - start).total_seconds() // 60)
    return f"{minutes // 60}시간 {minutes % 60}분" if minutes >= 60 else f"{minutes}분"


_COLOR_PROFIT = 0x2E7D32  # 초록 — 세후 이익
_COLOR_LOSS = 0xC62828  # 빨강 — 세후 손실
_COLOR_FLAT = 0x616161  # 회색 — 매매 없음/본전


def build_daily_summary_embed(
    trade_date: str,
    symbols: list[dict],
    fills: list[dict],
    deposit: float | None = None,
) -> dict:
    """일일 요약 Discord embed — 왼쪽 색 띠와 항목 분리로 한눈에 읽히게 만든다.

    같은 내용을 줄글로 보내면 색·구분이 없어 눈에 들어오지 않는다(2026-07-27 피드백).
    embed 는 손익 부호에 따라 초록/빨강 띠가 붙고, 종목마다 필드로 분리된다.
    """
    import datetime as _dt

    weekday = "월화수목금토일"[_dt.date.fromisoformat(trade_date).weekday()]
    traded = [s for s in symbols if s["total_bought"] > 0]
    closed = [s for s in traded if s["state"] == "종료"]
    holding = [s for s in traded if s["state"] != "종료"]
    realized = sum(s["realized_pnl"] for s in traded)
    fees = sum(s["fees"] for s in traded)
    net = realized - fees
    invested = sum(s["avg_price"] * s["total_bought"] for s in traded)

    if not traded:
        head = "체결된 매매가 없습니다."
        color = _COLOR_FLAT
    else:
        sign = "📈" if net > 0 else ("📉" if net < 0 else "➖")
        head = (
            f"{sign}  **{net:+,.0f}원**  (세전 {realized:+,.0f} · 비용 {fees:,.0f})\n"
            f"체결 {len(fills)}건 · 진입 {len(traded)}종목 · 청산 {len(closed)}종목"
        )
        if invested:
            head += f" · 투입 대비 **{net / invested:+.2%}**"
        color = _COLOR_PROFIT if net > 0 else (_COLOR_LOSS if net < 0 else _COLOR_FLAT)

    fields = []
    for s in traded[:20]:  # embed 필드 상한(25) 여유
        own = [f for f in fills if f["symbol"] == s["symbol"]]
        bought = sum(f["qty"] for f in own if f["side"] == "매수")
        sold = sum(f["qty"] for f in own if f["side"] == "매도")
        s_net = s["realized_pnl"] - s["fees"]
        if s["state"] != "종료":
            icon = "⚠️"
        elif s_net > 0:
            icon = "💰"
        elif s_net < 0:
            icon = "🛑"
        else:
            icon = "⚪"
        lines = [
            f"매수 `{bought}`주 · 평단 `{s['avg_price']:,.0f}`"
            + (f" · 매도 `{sold}`주" if sold else "")
            + (f" · 잔량 `{s['remaining']}`주" if s["remaining"] else "")
        ]
        if s["realized_pnl"]:
            lines.append(f"실현 **{s_net:+,.0f}원** (세전 {s['realized_pnl']:+,.0f})")
        if s["avg_price"] and s["high_price"]:
            high = (s["high_price"] - s["avg_price"]) / s["avg_price"]
            low = (s["low_price"] - s["avg_price"]) / s["avg_price"]
            lines.append(
                f"최고 `{high:+.1%}` / 최저 `{low:+.1%}`"
                + (f" · 보유 {held}" if (held := _holding_time(own)) else "")
            )
        if s["state"] != "종료":
            lines.append("**다음 매매일로 이월하세요**")
        fields.append(
            {
                "name": f"{icon} {s['name']}({s['symbol']}) · {s['state']}",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            }
        )
    if len(traded) > 20:
        fields.append(
            {"name": f"외 {len(traded) - 20}종목", "value": "\u200b", "inline": False}
        )

    footer = f"미진입 {len(symbols) - len(traded)}종목"
    if holding:
        footer += f" · 보유 중 {len(holding)}종목"
    if deposit is not None:
        footer += f" · 예수금 {deposit:,.0f}원"
    return {
        "title": f"📊 {trade_date} ({weekday}) 매매 요약",
        "description": head,
        "color": color,
        "fields": fields,
        "footer": {"text": footer},
    }


def format_daily_summary(
    trade_date: str,
    symbols: list[dict],
    fills: list[dict],
    deposit: float | None = None,
) -> str:
    """하루 매매 요약 — 실현손익(세전/세후), 종목별 결과, 이월 필요 종목."""
    weekday = "월화수목금토일"[
        __import__("datetime").date.fromisoformat(trade_date).weekday()
    ]
    traded = [s for s in symbols if s["total_bought"] > 0]
    closed = [s for s in traded if s["state"] == "종료"]
    holding = [s for s in traded if s["state"] != "종료"]
    realized = sum(s["realized_pnl"] for s in traded)
    fees = sum(s["fees"] for s in traded)
    invested = sum(s["avg_price"] * s["total_bought"] for s in traded)

    lines = [f"📊 **{trade_date} ({weekday}) 매매 요약**", ""]
    lines.append(
        f"체결 {len(fills)}건 · 진입 {len(traded)}종목 / 청산 {len(closed)}종목"
    )
    if traded:
        lines.append(
            f"실현손익 **{realized:+,.0f}원** (세전) · **{realized - fees:+,.0f}원** (세후)"
        )
        if invested:
            lines.append(
                f"투입 {invested:,.0f}원 대비 {(realized - fees) / invested:+.2%}"
            )
    lines.append("")

    for s in traded:
        own = [f for f in fills if f["symbol"] == s["symbol"]]
        bought = sum(f["qty"] for f in own if f["side"] == "매수")
        sold = sum(f["qty"] for f in own if f["side"] == "매도")
        mark = "  ⚠ 이월 필요" if s["state"] != "종료" else ""
        lines.append(f"▸ **{s['name']}({s['symbol']})** · {s['state']}{mark}")
        detail = f"   매수 {bought}주 (평단 {s['avg_price']:,.0f})"
        if sold:
            detail += f" · 매도 {sold}주"
        if s["remaining"]:
            detail += f" · 잔량 {s['remaining']}주"
        lines.append(detail)
        if s["realized_pnl"]:  # 청산이 있었던 종목만 손익 표시
            lines.append(
                f"   실현 {s['realized_pnl']:+,.0f} → 세후 "
                f"**{s['realized_pnl'] - s['fees']:+,.0f}원**"
            )
        if s["avg_price"] and s["high_price"]:  # 보유 중 최고/최저 (MFE/MAE)
            high = (s["high_price"] - s["avg_price"]) / s["avg_price"]
            low = (s["low_price"] - s["avg_price"]) / s["avg_price"]
            extra = f"   최고 {high:+.1%} / 최저 {low:+.1%}"
            if held := _holding_time(own):
                extra += f" · 보유 {held}"
            lines.append(extra)

    if not traded:
        lines.append("체결된 매매가 없습니다.")
    lines.append("")
    lines.append(
        f"미진입 {len(symbols) - len(traded)}종목"
        + (
            f" · 보유 중 {len(holding)}종목 (다음 매매일로 이월하세요)"
            if holding
            else ""
        )
    )
    if deposit is not None:
        lines.append(f"예수금 {deposit:,.0f}원")
    return "\n".join(lines)


def format_message(symbol: str, kind: str, text: str) -> str:
    prefix = f"**[{kind}]**"
    return f"{prefix} {text}" if symbol == "시스템" else f"{prefix} {symbol} · {text}"


class DiscordNotifier:
    _MIN_INTERVAL = 1.5  # webhook 분당 제한(약 30건) 방어용 최소 발송 간격
    _MUTE_SEC = 60.0  # 429 수신 시 발송 중단 시간

    def __init__(self, webhook_url: str):
        self._url = webhook_url
        self._lock = threading.Lock()  # 발송 스레드 간 간격 보장
        self._last_send = 0.0
        self._mute_until = 0.0

    def send(self, text: str) -> bool:
        """텍스트 발송 (blocking). 성공 True, 제한 중 생략 False."""
        return self._post(
            lambda: requests.post(self._url, json={"content": text[:1900]}, timeout=10)
        )

    def send_embed(self, embed: dict) -> bool:
        """embed 발송 (blocking) — 색 띠·필드가 있는 구조화 메시지."""
        return self._post(
            lambda: requests.post(self._url, json={"embeds": [embed]}, timeout=10)
        )

    def send_image(self, path: str, caption: str = "") -> bool:
        """이미지 1장 발송 — send_images 의 단축형."""
        return self.send_images([path], caption)

    def send_images(self, paths: list[str], caption: str = "") -> bool:
        """이미지 여러 장을 **한 메시지로** 발송 (blocking) — 복기 차트 전송용.

        Discord webhook 의 multipart 업로드로 files[0], files[1], ... 을 함께 올리면
        사진들이 하나의 메시지에 나란히 붙는다. 텍스트 발송과 같은 잠금을 공유해
        최소 간격·429 뮤트가 그대로 적용된다. 업로드가 있어 타임아웃은 넉넉히 둔다.
        """
        import json
        from contextlib import ExitStack
        from pathlib import Path

        def request():
            with ExitStack() as stack:
                files = {
                    f"files[{i}]": (
                        Path(p).name,
                        stack.enter_context(open(p, "rb")),
                        "image/png",
                    )
                    for i, p in enumerate(paths)
                }
                return requests.post(
                    self._url,
                    data={
                        "payload_json": json.dumps(
                            {"content": caption[:1900]}, ensure_ascii=False
                        )
                    },
                    files=files,
                    timeout=30 + 15 * len(paths),
                )

        return self._post(request)

    def _post(self, request) -> bool:
        """발송 공통부: 최소 간격 보장 → 요청 → 429 뮤트 / 오류 판정.

        연속 발송은 최소 간격을 두고 순서대로 나가며, 429(전송 제한)를 받으면
        일정 시간 발송을 통째로 생략해 제한 반복을 막는다.
        """
        with self._lock:
            now = time.monotonic()
            if now < self._mute_until:
                return False  # 제한 중 — 조용히 생략 (호출부 로그 불필요)
            wait = self._last_send + self._MIN_INTERVAL - now
            if wait > 0:
                time.sleep(wait)
            self._last_send = time.monotonic()
            resp = request()
            if resp.status_code == 429:
                self._mute_until = time.monotonic() + self._MUTE_SEC
                raise NotifierError(
                    f"Discord 전송 제한(429) — {self._MUTE_SEC:.0f}초간 알림을 생략합니다"
                )
            if resp.status_code not in (200, 204):
                raise NotifierError(
                    f"Discord 발송 실패 (HTTP {resp.status_code}): {resp.text[:200]}"
                )
            return True
