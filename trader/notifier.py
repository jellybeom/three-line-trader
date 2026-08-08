"""Discord 메시지 구성 — 포맷·색·필터 (발송은 discord_bot 이 담당).

알림 문구와 embed 를 만드는 순수 함수 모음이라 네트워크 없이 단위 테스트할 수 있다.
발송 경로는 **Discord 봇 하나로 일원화**되어 있다. 웹훅을 함께 두면 "지금 무엇이
연결된 상태인지" 가 헷갈려 운영 사고로 이어지므로 쓰지 않는다.
"""

from __future__ import annotations


class NotifierError(RuntimeError):
    """알림 구성·발송 실패."""


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
_COLOR_INFO = 0x455A64  # 청회색 — 관심종목 변경 등 정보성 묶음
_COLOR_WARN = 0xEF6C00  # 주황 — 확인이 필요한 경고 묶음


def shorten_error(text: str) -> str:
    """API 오류 문구에서 사람이 읽을 부분만 남긴다.

    예) "kt10000 실패 (HTTP 200, code 20): [2000] (855056:매수증거금이 부족합니다.
        127주 매수가능)" → "매수증거금이 부족합니다. 127주 매수가능"
    형식이 예상과 다르면 원문을 그대로 둔다(진단 정보를 잃지 않기 위해).
    """
    import re

    # "kt10000 실패 (HTTP 200, code 20): [2000] (855056:사람이 읽을 내용)" 통째를
    # 사람이 읽을 내용으로 치환한다. 앞뒤 문맥(예: "주문 실패(3회):", "— 30초간 보류")은 남긴다.
    tech = re.search(r"\S*\d+\s*실패\s*\(HTTP[^)]*\)[^(]*\((?:\d+:)?([^()]*)\)", text)
    if tech:
        return (
            text[: tech.start()] + tech.group(1).strip() + text[tech.end() :]
        ).strip()
    # 괄호 안이 "코드:메시지" 형태일 때만 꺼낸다 — 일반 괄호 설명은 본문이므로 건드리지 않는다
    tail = re.search(r"\((\d+):([^()]*)\)\s*$", text.strip())
    if tail:
        return tail.group(2).strip()
    return text


def build_trade_embed(
    name: str,
    symbol: str,
    reason: str,
    qty: int,
    price: float,
    realized: float,
    fees: float,
) -> dict:
    """종목이 '종료' 될 때의 결산 embed — 색 띠로 이익/손실이 한눈에 들어온다.

    매수·단계 익절처럼 자주 오는 알림은 한 줄 텍스트로 두고, 하루에 몇 번뿐인
    '종료' 만 embed 로 보내 대비를 만든다.
    """
    net = realized - fees
    result = reason.split("→")[-1].strip()
    if net > 0:
        icon, color = "💰", _COLOR_PROFIT
    elif net < 0:
        icon, color = "🛑", _COLOR_LOSS
    else:
        icon, color = "⚪", _COLOR_FLAT
    lines = [f"{result} — **{qty}주** @ {price:,.0f}"] if qty > 0 else [result]
    lines.append(
        f"실현손익 **{net:+,.0f}원** (세전 {realized:+,.0f} · 비용 {fees:,.0f})"
    )
    return {
        "title": f"{icon} {name}({symbol}) 종료",
        "description": "\n".join(lines),
        "color": color,
    }


_COLOR_LINK = 0x00838F  # 청록 — 연결·감시 상태

# 종류별 아이콘·색. 줄글로 흘려보내면 무엇이 중요한지 구분되지 않으므로,
# 성격이 다른 알림에 서로 다른 색 띠와 아이콘을 준다.
_KIND_STYLE = {
    "에러": ("⛔", _COLOR_LOSS),
    "경고": ("⚠️", _COLOR_WARN),
    "보류": ("⏸️", _COLOR_WARN),
    "등록": ("➕", _COLOR_INFO),
    "편집": ("✏️", _COLOR_INFO),
    "삭제": ("🗑️", _COLOR_INFO),
    "이월": ("📦", _COLOR_INFO),
    "리셋": ("♻️", _COLOR_INFO),
    "설정": ("⚙️", _COLOR_INFO),
    "연결": ("🔗", _COLOR_LINK),
    "감시": ("👁️", _COLOR_LINK),
    "시작": ("🚀", _COLOR_LINK),
    "요약": ("📊", _COLOR_INFO),
    "차트": ("📈", _COLOR_INFO),
}


def kind_icon(kind: str) -> str:
    return _KIND_STYLE.get(kind, ("ℹ️", _COLOR_INFO))[0]


def build_alert_embed(kind: str, label: str, text: str) -> dict:
    """단건 알림 embed — 종류별 색 띠가 있어야 흐름 속에서 눈에 띈다."""
    icon, color = _KIND_STYLE.get(kind, ("ℹ️", _COLOR_INFO))
    title = f"{icon} {kind}"
    if label and label != "시스템":
        title += f" · {label}"
    return {
        "title": title[:256],
        "description": shorten_error(text)[:4000],
        "color": color,
    }


def base_date_label(base_date: str, trade_date: str) -> str:
    """기준봉으로부터 며칠째인지 — 'D+2' 형태. 날짜가 없거나 이상하면 빈 문자열."""
    import datetime as _dt

    if not base_date:
        return ""
    try:
        days = (
            _dt.date.fromisoformat(trade_date) - _dt.date.fromisoformat(base_date)
        ).days
    except ValueError:
        return ""
    return f"D{days:+d}" if days else "D0"


def _tag_counts(symbols: list[dict]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for s in symbols:
        for tag in (t.strip() for t in (s.get("tags") or "").split(",") if t.strip()):
            counts[tag] = counts.get(tag, 0) + 1
    return sorted(counts.items(), key=lambda x: -x[1])


def _base_date_counts(symbols: list[dict], trade_date: str) -> list[tuple[str, int]]:
    """기준봉 경과일 분포 — 'D+1 12종목' 처럼 묶어 보여주기 위한 집계."""
    counts: dict[str, int] = {}
    for s in symbols:
        label = base_date_label(s.get("base_date", ""), trade_date)
        if label:
            counts[label] = counts.get(label, 0) + 1
    return sorted(counts.items(), key=lambda x: int(x[0].lstrip("D") or 0))


def build_briefing_embed(
    trade_date: str,
    symbols: list[dict],
    funds: dict | None = None,
    deposit: float | None = None,
) -> dict:
    """개장 브리핑 — 감시 시작 시 '오늘 무엇을 들고 시작하는가' 를 한 장으로.

    종목을 일일이 나열하면 수십 줄이 되고 Discord 제한에 걸린다. 여기서는 **집계와
    확인이 필요한 항목만** 싣고, 전체 목록은 `/관심종목` 으로 조회한다.
    """
    warned = [s for s in symbols if s.get("qty") is not None and 0 < s["qty"] < 3]
    lines = []
    if tags := _tag_counts(symbols):
        lines.append("**태그** " + " · ".join(f"`#{t}` {n}" for t, n in tags[:8]))
    if bases := _base_date_counts(symbols, trade_date):
        lines.append(
            "**기준봉** " + " · ".join(f"{label} {n}종목" for label, n in bases[:8])
        )
    if funds:
        lines.append(
            f"**자금** 총 {funds['total']:,.0f}원 · 최대 {funds['max_symbols']}종목 "
            f"· 종목당 {funds['per_symbol']:,.0f}원"
        )
    if deposit is not None:
        lines.append(f"**주문가능** {deposit:,.0f}원")

    embed = {
        "title": f"🔔 {trade_date} 감시 시작 · 관심종목 {len(symbols)}종목",
        "description": "\n".join(lines) or "\u200b",
        "color": _COLOR_LINK,
    }
    if warned:
        embed["fields"] = [
            {
                "name": f"⚠️ 소량 진입 예상 {len(warned)}종목",
                "value": "\n".join(
                    f"{s['name']}({s['symbol']}) 1차 {s['qty']}주" for s in warned[:10]
                )[:1024],
                "inline": False,
            }
        ]
    return embed


def build_watchlist_embed(
    trade_date: str,
    symbols: list[dict],
    page: int = 1,
    per_page: int = 15,
    tag: str = "",
) -> dict:
    """`/관심종목` — 종목별 태그·메모·기준봉을 페이지로 나눠 보여준다."""
    rows = [s for s in symbols if not tag or tag in (s.get("tags") or "")]
    total_pages = max(1, -(-len(rows) // per_page))
    page = min(max(page, 1), total_pages)
    chunk = rows[(page - 1) * per_page : page * per_page]

    lines = []
    for s in chunk:
        head = f"**{s['name']}**(`{s['symbol']}`)"
        meta = []
        if tags := (s.get("tags") or ""):
            meta.append(" ".join(f"`#{t}`" for t in tags.split(",") if t))
        if label := base_date_label(s.get("base_date", ""), trade_date):
            meta.append(f"기준봉 {label}")
        if meta:
            head += " · " + " · ".join(meta)
        lines.append(head)
        if memo := (s.get("memo") or ""):
            lines.append(f"　📝 {memo}")

    title = f"📋 {trade_date} 관심종목 {len(rows)}종목"
    if tag:
        title += f" · #{tag}"
    return {
        "title": title,
        "description": "\n".join(lines)[:4000] or "해당하는 종목이 없습니다.",
        "color": _COLOR_INFO,
        "footer": {"text": f"{page}/{total_pages} 쪽"},
    }


def build_registration_embed(
    trade_date: str,
    rows: list[dict],
    warnings: list[str] | None = None,
    staged: int = 0,
    skipped: int = 0,
) -> dict:
    """관심종목 등록 알림 — 종목마다 선정 근거(태그·기준봉)를 함께 보여준다.

    rows: {symbol, name, tags, base_date, memo, qty(1차 예상 수량)} 목록.
    등록은 '무엇을 왜 골랐는지' 를 남기는 자리이므로 상태·잔량 같은 기계적 정보 대신
    선정 근거를 싣는다.
    """
    lines = []
    for r in rows[:40]:
        if lines:  # 종목 사이에 옅은 구분선 — 목록이 길어도 경계가 보인다
            lines.append("─" * 18)
        head = f"▸ **{r['name']}**(`{r['symbol']}`)"
        meta = []
        if tags := r.get("tags"):
            meta.append(" ".join(f"`#{t}`" for t in tags.split(",") if t))
        if label := base_date_label(r.get("base_date", ""), trade_date):
            meta.append(f"기준봉 {label}")
        if meta:
            head += " · " + " · ".join(meta)
        lines.append(head)
        detail = []
        if (qty := r.get("qty")) is not None and qty < 3:
            detail.append(f"⚠️ 1차 {qty}주 — 단계 익절 어려움")
        if memo := r.get("memo"):
            detail.append(f"📝 {memo}")
        if detail:
            lines.append("　" + " · ".join(detail))
    if len(rows) > 40:
        lines.append(f"…외 {len(rows) - 40}종목")

    extra = []
    if staged:
        extra.append(f"3선 미입력 {staged}종목")
    if skipped:
        extra.append(f"중복 제외 {skipped}종목")
    embed = {
        "title": f"➕ 관심종목 등록 {len(rows)}종목 · {trade_date}",
        "description": "\n".join(lines)[:4000] or "\u200b",
        "color": _COLOR_INFO,
    }
    if extra:
        embed["footer"] = {"text": " · ".join(extra)}
    if warnings:
        embed["fields"] = [
            {
                "name": f"⚠️ 확인 필요 {len(warnings)}건",
                "value": "\n".join(warnings[:10])[:1024],
                "inline": False,
            }
        ]
        embed["color"] = _COLOR_WARN
    return embed


def build_batch_embed(items: list[tuple[str, str, str]]) -> dict:
    """짧은 알림 여러 건을 한 장의 embed 로 묶는다.

    items: (종류, 종목표시, 내용) 목록. 등록·편집·삭제·보류처럼 한 번에 여러 건이
    쏟아지는 알림을 줄 단위로 보내면, 발송 간격(1.5초) 때문에 24건이 36초에 걸쳐
    도착하고 그 사이 체결 알림이 밀린다. 묶으면 한 번의 발송으로 끝난다.
    """
    kinds = [k for k, _, _ in items]
    warn = [i for i in items if i[0] in ("경고", "에러", "보류")]
    main = [i for i in items if i not in warn]

    counts: dict[str, int] = {}
    for kind in kinds:
        counts[kind] = counts.get(kind, 0) + 1
    title = " · ".join(
        f"{_KIND_STYLE.get(k, ('ℹ️',))[0]} {k} {n}건" for k, n in counts.items()
    )

    lines = [f"`{label}` {shorten_error(text)}" for _, label, text in main[:40]]
    if len(main) > 40:
        lines.append(f"…외 {len(main) - 40}건")
    embed = {
        "title": title[:256],
        "description": "\n".join(lines)[:4000] or "\u200b",
        "color": _COLOR_WARN if warn else _COLOR_INFO,
    }
    if warn:
        embed["fields"] = [
            {
                "name": f"⚠️ 확인 필요 {len(warn)}건",
                "value": "\n".join(
                    f"`{label}` {shorten_error(text)}" for _, label, text in warn[:10]
                )[:1024],
                "inline": False,
            }
        ]
    return embed


def proximity_rows(symbols: list[dict]) -> list[tuple[float, dict]]:
    """미진입 종목의 (1선 대비 최저가 괴리율, 종목) — 가까운 순.

    진입이 없는 날이 '설정이 보수적' 인지 '시장이 안 맞는' 것인지 구분하는 근거다.
    """
    rows = [
        ((s["day_low"] - s["line1"]) / s["line1"], s)
        for s in symbols
        if not s["total_bought"] and s.get("day_low") and s.get("line1")
    ]
    rows.sort(key=lambda x: x[0])
    return rows


def build_proximity_field(symbols: list[dict], top: int = 5) -> dict | None:
    """일일 요약에 붙일 근접도 필드 (없으면 None)."""
    rows = proximity_rows(symbols)
    if not rows:
        return None
    lines = [
        f"`{gap:+6.1%}` {s['name']}({s['symbol']}) · 최저 {s['day_low']:,.0f} "
        f"/ 1선 {s['line1']:,.0f}"
        for gap, s in rows[:top]
    ]
    lines.append("\n" + _proximity_counts(rows))
    return {
        "name": "🎯 1선 근접도 (미진입)",
        "value": "\n".join(lines)[:1024],
        "inline": False,
    }


def _proximity_counts(rows: list[tuple[float, dict]]) -> str:
    def within(pct: float) -> int:
        return sum(1 for gap, _ in rows if gap <= pct)

    return (
        f"3% 이내 **{within(0.03)}종목** · 5% 이내 **{within(0.05)}종목** · "
        f"10% 이내 **{within(0.10)}종목** (총 {len(rows)}종목)"
    )


def build_proximity_embed(trade_date: str, symbols: list[dict], top: int = 12) -> dict:
    """`/근접도` 전용 — 요약보다 많은 종목을 보여준다."""
    rows = proximity_rows(symbols)
    if not rows:
        return {
            "title": f"🎯 {trade_date} 1선 근접도",
            "description": "아직 시세를 받은 미진입 종목이 없습니다.",
            "color": _COLOR_FLAT,
        }
    lines = [
        f"`{gap:+6.1%}` {s['name']}({s['symbol']}) · 최저 {s['day_low']:,.0f} "
        f"/ 1선 {s['line1']:,.0f}"
        for gap, s in rows[:top]
    ]
    if len(rows) > top:
        lines.append(f"…외 {len(rows) - top}종목")
    return {
        "title": f"🎯 {trade_date} 1선 근접도 (미진입 {len(rows)}종목)",
        "description": "\n".join(lines)[:4000],
        "color": _COLOR_INFO,
        "footer": {"text": _proximity_counts(rows).replace("**", "")},
    }


def build_daily_summary_embed(
    trade_date: str,
    symbols: list[dict],
    fills: list[dict],
    deposit: float | None = None,
    account: dict | None = None,
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

    if field := build_proximity_field(symbols):  # 미진입 종목의 1선 근접도
        fields.append(field)

    if (
        account
    ):  # 계좌 기준 평가 현황 — 이월 종목이 있으면 실현손익만으로는 성과가 안 보인다
        parts = []
        if account.get("value"):
            parts.append(
                f"평가 {account['value']:,.0f}원 "
                f"({account.get('pnl', 0):+,.0f} · {account.get('rate', 0):+.2f}%)"
            )
        if account.get("asset"):
            parts.append(f"추정자산 {account['asset']:,.0f}원")
        if parts:
            fields.append(
                {"name": "💼 계좌", "value": "\n".join(parts), "inline": False}
            )

    footer = f"미진입 {len(symbols) - len(traded)}종목"
    if holding:
        footer += f" · 보유 중 {len(holding)}종목"
    if deposit is not None:
        footer += f" · 주문가능 {deposit:,.0f}원"
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
