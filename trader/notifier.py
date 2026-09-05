"""Discord 메시지 구성 — 포맷·색·필터 (발송은 discord_bot 이 담당).

알림 문구와 embed 를 만드는 순수 함수 모음이라 네트워크 없이 단위 테스트할 수 있다.
발송 경로는 **Discord 봇 하나로 일원화**되어 있다. 웹훅을 함께 두면 "지금 무엇이
연결된 상태인지" 가 헷갈려 운영 사고로 이어지므로 쓰지 않는다.
"""

from __future__ import annotations

import re


class NotifierError(RuntimeError):
    """알림 구성·발송 실패."""


def should_notify(level: str, symbol: str, kind: str) -> bool:
    """알림 수준 필터 — 로그 한 줄을 Discord 로 보낼지 결정한다.

    '매매만' 은 **시스템의 잡음**을 걸러 달라는 뜻이지 **사고를 숨겨 달라는 뜻이 아니다.**
    예전에는 종류와 무관하게 시스템 로그를 전부 막아, 키움 연결 실패·자동 시작 실패·
    휴장 안내처럼 반드시 알아야 할 것까지 가지 않았다(2026-08-17 확인). 경고·에러는
    수준과 상관없이 보낸다 — '끔' 만 예외다.
    """
    if level == "끔":
        return False
    if kind in ("에러", "경고"):
        return True
    if level == "에러만":
        return False
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
    """첫 체결 ~ 마지막 체결 경과 시간.

    ⚠️ **당일 체결만** 보므로 이월 종목에서는 답이 되지 않는다(어제 산 것을 오늘 팔면
    매수 행이 없어 빈칸이 된다). 보유기간은 호출부가 `holdings` 로 넘겨주는 값을 쓰고,
    이 함수는 그 값이 없을 때의 대비책으로만 남긴다.
    """
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
    path: str = "",
    avg_price: float = 0.0,
    total_bought: int = 0,
    holding: str = "",
    timeline: str = "",
    tags: str = "",
) -> dict:
    """종목이 '종료' 될 때의 결산 embed — 색 띠로 이익/손실이 한눈에 들어온다.

    매수·단계 익절처럼 자주 오는 알림은 한 줄 텍스트로 두고, 하루에 몇 번뿐인
    '종료' 만 embed 로 보내 대비를 만든다.

    담는 것은 **경로 · 세후 손익 · 가격대** 셋이다(2026-08-18 재구성).
    - 예전에는 '전량 손절 — 125주 @ 3,065' 였는데, 잔량 주수는 "그래서 어땠나" 에
      답하지 않았고 1차에서 죽었는지 2차까지 갔는지도 알 수 없었다. 상태 경로가 그 자리를
      대신한다.
    - **수익률(%)** 을 넣는다. 금액만으로는 큰 손실인지 알 수 없다 — 저가주는 주식 수가
      많아 절대액이 커 보인다.
    - 청산가는 마지막 체결가가 아니라 **평균 청산가**다. 3단 익절을 하면 판 가격이
      제각각이라 마지막 한 건만으로는 "결국 얼마에 팔았나" 를 답하지 못한다.

    매매일지 스레드로 갈 때는 timeline(진입·청산 시각과 기준봉 D±n)과 tags 를 함께
    받는다. **선정 근거와 시간축이 있어야 코멘트를 쓸 수 있다** — "왜 골랐나" 와
    "얼마나 들고 있었나" 가 복기의 첫 두 질문이다. 알림 채널로 갈 때는 비워 두어
    장중에 흘려보는 알림이 길어지지 않게 한다.

    최고/최저(MFE/MAE)는 **일부러 넣지 않는다.** 숫자가 늘수록 무엇을 보고 쓸지
    흐려진다. 스레드는 무엇을 쓸지 떠올리게 하는 자리이지 결산표가 아니고, 그 값들은
    문서에서 차분히 본다.
    """
    net = realized - fees
    if net > 0:
        icon, color = "💰", _COLOR_PROFIT
    elif net < 0:
        icon, color = "🛑", _COLOR_LOSS
    else:
        icon, color = "⚪", _COLOR_FLAT

    lines = [path or reason.split("→")[-1].strip()]
    invested = avg_price * total_bought
    rate = f" ({net / invested:+.2%})" if invested else ""
    lines.append(f"세후 **{net:+,.0f}원**{rate}")
    if avg_price and total_bought:
        # 평균 청산가 = 평단 + 주당 세전손익. 매도 건마다 값이 달라도 하나로 모인다.
        exit_price = avg_price + realized / total_bought
        lines.append(
            f"평단 {avg_price:,.0f} → 청산 {exit_price:,.0f}"
            f" · 투입 {_short_won(invested)}"
            + (f" · 보유 {holding}" if holding else "")
        )
    if timeline:
        lines.append(timeline)
    if tags:
        lines.append(" ".join(f"`#{t.strip()}`" for t in tags.split(",") if t.strip()))
    return {
        "title": f"{icon} {name}({symbol}) 종료",
        "description": "\n".join(lines),
        "color": color,
    }


def _short_won(amount: float) -> str:
    """40.6만원 / 1,250만원 — 폰에서 자릿수를 세지 않아도 규모가 잡히게."""
    if amount >= 100_000_000:
        return f"{amount / 100_000_000:,.2f}억원"
    if amount >= 10_000:
        return f"{amount / 10_000:,.1f}만원"
    return f"{amount:,.0f}원"


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


def benchmark(symbols: list[dict]) -> dict:
    """벤치마크 — 내가 산 종목 vs 관심종목 전체가 그날 어땠는지.

    성과가 실력인지 시장 덕인지 가늠하는 재료다. 감시 중 받은 **첫 체결가 대비
    마지막 체결가**로 계산하므로 추가 조회가 없다.
    """

    def rate(s: dict) -> float | None:
        opened, closed = s.get("day_open") or 0, s.get("day_close") or 0
        return (closed - opened) / opened if opened and closed else None

    all_rates = [r for s in symbols if (r := rate(s)) is not None]
    traded_rates = [
        r for s in symbols if s.get("total_bought") and (r := rate(s)) is not None
    ]
    result: dict[str, float | int] = {
        "watch_count": len(all_rates),
        "traded_count": len(traded_rates),
    }
    if all_rates:
        result["watch_avg"] = sum(all_rates) / len(all_rates)
    if traded_rates:
        result["traded_avg"] = sum(traded_rates) / len(traded_rates)
    return result


def build_benchmark_field(
    symbols: list[dict], index_rate: float | None = None
) -> dict | None:
    """일일 요약에 붙일 '시장 대비' 필드."""
    bench = benchmark(symbols)
    lines = []
    if "traded_avg" in bench:
        lines.append(
            f"내 종목 **{bench['traded_avg']:+.2%}** ({bench['traded_count']}종목)"
        )
    # 관심종목 평균은 '내가 산 것 말고도 있을 때' 만 비교 가치가 있다.
    # 산 종목이 전부면 두 숫자가 같아 대조가 되지 않는다.
    if "watch_avg" in bench and bench["watch_count"] > bench["traded_count"]:
        lines.append(
            f"관심종목 평균 {bench['watch_avg']:+.2%} ({bench['watch_count']}종목)"
        )
    if index_rate is not None:
        lines.append(f"KOSPI {index_rate:+.2%}")
    if len(lines) < 2:  # 비교 대상이 없으면 의미가 없다
        return None
    return {"name": "📐 시장 대비", "value": " · ".join(lines), "inline": False}


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


def build_registration_warning_embed(
    trade_date: str, warnings: list[str], total: int
) -> dict:
    """CSV 불러오기에서 **고쳐야 할 것만** 담는 embed.

    편성 결과 전체는 08:55 개장 브리핑이 맡는다. 경고 한 건 때문에 80종목 목록이
    폰으로 쏟아지면, 정작 고쳐야 할 한 줄이 그 안에 묻힌다(2026-08-31 실측).
    등록이 몇 종목인지는 제목에 숫자로만 남긴다.
    """
    return {
        "title": f"⚠️ 관심종목 확인 필요 {len(warnings)}건 · {trade_date}",
        "description": "\n".join(f"• {w}" for w in warnings[:10])[:4000],
        "color": _COLOR_WARN,
        "footer": {"text": f"등록 {total}종목 — 나머지는 정상입니다"},
    }


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


def block_reason(text: str) -> str:
    """보류 사유를 묶을 수 있는 짧은 이름으로. `최대 종목 수(7) 도달 — …` → `최대 종목 수`

    괄호 안의 숫자를 떼는 이유는 그것이 설정값이라 같은 사유가 갈라지기 때문이다.
    """
    head = text.split("—")[0].strip()
    return re.sub(r"\s*\(.*?\)\s*", " ", head).replace(" 도달", "").strip() or "기타"


def proximity_rows(
    symbols: list[dict], exclude: "set[str] | None" = None
) -> list[tuple[float, dict]]:
    """미진입 종목의 (1선 대비 최저가 괴리율, 종목) — 가까운 순.

    진입이 없는 날이 '설정이 보수적' 인지 '시장이 안 맞는' 것인지 구분하는 근거다.

    exclude 는 보류된 종목이다. 이 목록은 **'내일 볼 만한 종목'** 을 보는 자리인데,
    보류 종목은 이미 1선을 지나 괴리율이 음수라 맨 위를 차지해 버린다. 그것들은 따로
    떼어 사유와 함께 보여주는 편이 답을 더 잘 준다.
    """
    skip = exclude or set()
    rows = [
        ((s["day_low"] - s["line1"]) / s["line1"], s)
        for s in symbols
        if not s["total_bought"]
        and s.get("day_low")
        and s.get("line1")
        and s["symbol"] not in skip
    ]
    rows.sort(key=lambda x: x[0])
    return rows


def build_monthly_embed(
    month: str, entries: list, buckets: list, tags: list, slips: list, blocked: dict
) -> dict:
    """월간 집계 embed — **숫자만**. 그래프는 다음 단계다.

    건수를 늘 함께 적는다. 표본이 작을 때 3건짜리 100% 를 신호로 읽으면 그 판단이 몇 달을
    간다. 값이 없으면 `-` 로 두고 0 으로 채우지 않는다 — 없는 것과 0 은 다르다.
    """
    closed = [e for e in entries if (e.get("state") or "") == "종료"]
    wins = sum(
        1 for e in closed if (e.get("realized_pnl") or 0) - (e.get("fees") or 0) > 0
    )
    losses = sum(
        1 for e in closed if (e.get("realized_pnl") or 0) - (e.get("fees") or 0) < 0
    )
    net = sum((e.get("realized_pnl") or 0) - (e.get("fees") or 0) for e in closed)
    decided = wins + losses
    head = [
        f"{'📈' if net >= 0 else '📉'}  세후 **{net:+,.0f}원** · {len(closed)}건",
        f"익절 {wins} · 손절 {losses}"
        + (f" · 승률 **{wins / decided:.0%}**" if decided else ""),
    ]
    if holding := len(entries) - len(closed):
        head.append(f"보유 중 {holding}종목")

    fields = []
    if rows := [b for b in buckets if b.trades]:
        fields.append(
            {
                "name": "📦 1차 수량별",
                "value": "\n".join(_bucket_line(b) for b in rows),
                "inline": False,
            }
        )
    if rows := [s for s in slips if s.count]:
        fields.append(
            {
                "name": "🎯 판정가 대비 체결 오차",
                "value": "\n".join(_slip_line(s) for s in rows),
                "inline": False,
            }
        )
    if tags:
        fields.append(
            {
                "name": "🏷️ 태그별",
                "value": "\n".join(_bucket_line(b) for b in tags),
                "inline": False,
            }
        )
    if blocked:
        top = sorted(blocked.items(), key=lambda kv: -kv[1])[:5]
        fields.append(
            {
                "name": f"⏸️ 진입 못 함 {sum(blocked.values())}회",
                "value": "\n".join(f"{reason} `{n}회`" for reason, n in top),
                "inline": False,
            }
        )
    return {
        "title": f"📊 {month} 월간 집계",
        "description": "\n".join(head),
        "color": _COLOR_PROFIT if net >= 0 else _COLOR_LOSS,
        "fields": fields,
        "footer": {"text": "표본이 작으면 승률보다 건수를 먼저 본다"},
    }


def _bucket_line(b) -> str:
    parts = [f"`{b.label:>6}` {b.trades:>2}건"]
    if (rate := b.win_rate) is not None:
        parts.append(f"승률 {rate:.0%}")
    if (ret := b.rate) is not None:
        parts.append(f"수익률 {ret:+.2%}")
    if (mfe := b.mfe) is not None:
        parts.append(f"최고 {mfe:+.1%}")
    return " · ".join(parts)


def _slip_line(s) -> str:
    parts = [f"`{s.label}` {s.count:>2}건"]
    if (buy := s.buy) is not None:
        parts.append(f"매수 {buy:+.2%}")
    if (sell := s.sell) is not None:
        parts.append(f"매도 {sell:+.2%}")
    parts.append(f"최악 {s.worst:.2%}")
    return " · ".join(parts)


def build_log_csv(rows: list[tuple[str, str, str, str, str]]) -> bytes:
    """하루치 로그를 CSV 바이트로. 화면의 `CSV 내보내기` 와 **같은 형식**이다.

    맨 앞에 BOM 을 둔다 — 없으면 엑셀이 UTF-8 을 못 알아보고 한글이 깨진다.
    줄바꿈은 CRLF 로 둔다(csv 모듈 기본). 엑셀과 폰 앱 양쪽에서 안전하다.
    """
    import csv
    import io

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["시각", "대상", "종목명", "종류", "내용"])
    writer.writerows(rows)
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")


def build_blocked_fields(symbols: list[dict], blocked: dict[str, str]) -> list[dict]:
    """보류 종목 필드 — **사유별로 나누고, 전부 보여준다.**

    사유가 섞이면 자금을 늘려야 할지 종목 수를 늘려야 할지 알 수 없다. 사유마다 필드를
    나누면 Discord 가 제목을 굵게 그려 줘서, 사유가 하나뿐인 날에도 지저분해지지 않는다.

    개수를 자르지 않는 이유는 이 목록 자체가 '몇 개나 놓쳤나' 에 답하기 때문이다 —
    5개만 보여주면 그 답이 사라진다. Discord 필드 한도(1024자)에 걸릴 때만 줄인다.
    """
    if not blocked:
        return []
    by_symbol = {s["symbol"]: s for s in symbols}
    groups: dict[str, list] = {}
    for symbol, reason in blocked.items():
        s = by_symbol.get(symbol)
        if s is None:
            continue
        gap = (
            (s["day_low"] - s["line1"]) / s["line1"]
            if s.get("day_low") and s.get("line1")
            else 0.0
        )
        groups.setdefault(block_reason(reason), []).append((gap, s))

    fields = []
    for reason, rows in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        rows.sort(key=lambda x: x[0], reverse=True)  # 1선에 가까웠던 것부터
        lines = [
            f"`{gap:+6.1%}` {s['name']}({s['symbol']}) · 최저 {s['day_low']:,.0f} "
            f"/ 1선 {s['line1']:,.0f}"
            for gap, s in rows
        ]
        fields.append(
            {
                "name": f"⏸️ 진입 못 함 · {reason} {len(rows)}종목",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            }
        )
    return fields


def build_proximity_field(
    symbols: list[dict], top: int = 5, exclude: "set[str] | None" = None
) -> dict | None:
    """일일 요약에 붙일 근접도 필드 (없으면 None).

    보류된 종목은 뺀다 — 그것들은 사유와 함께 따로 보여준다(build_blocked_fields).
    여기는 '내일 볼 만한 종목' 을 보는 자리라 5개면 충분하다.
    """
    rows = proximity_rows(symbols, exclude)
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
    index_rate: float | None = None,
    holdings: dict[str, str] | None = None,
    cycle_pnl: dict[str, tuple[float, float]] | None = None,
    blocked: dict[str, str] | None = None,
) -> dict:
    """일일 요약 Discord embed — 왼쪽 색 띠와 항목 분리로 한눈에 읽히게 만든다.

    같은 내용을 줄글로 보내면 색·구분이 없어 눈에 들어오지 않는다(2026-07-27 피드백).
    embed 는 손익 부호에 따라 초록/빨강 띠가 붙고, 종목마다 필드로 분리된다.

    **손익 기준이 머리글과 종목별 줄에서 다르다.** 답하는 질문이 다르기 때문이다.

    - 머리글 = **오늘 실현된 것의 합계**. "오늘 계좌에 얼마가 들어왔나" 다. 날짜별로
      더했을 때 총합이 맞아야 하므로 그날 몫만 센다.
    - 종목별 줄(종료) = **그 매매의 최종 성적**. 이월된 매매는 며칠에 걸쳐 있어 하루치만
      적으면 전날 낸 익절이 통째로 빠진다. 문서·차트·종료 알림과 같은 값이다.

    cycle_pnl 은 {종목: (세전, 비용)} 로 호출부(코어)가 사이클 전체를 합산해 넘긴다 —
    notifier 는 DB 를 모른다. 보유 중인 종목은 아직 끝나지 않았으므로 손익을 적지 않는다.

    blocked 는 {종목: 보류 사유} 로, **1선에 닿았는데 끝내 못 산 종목**이다. 사유별로
    나눠 전부 보여주고 근접도 목록에서는 뺀다 — 자세한 이유는 build_blocked_fields 참고.
    """
    import datetime as _dt

    weekday = "월화수목금토일"[_dt.date.fromisoformat(trade_date).weekday()]
    traded = [s for s in symbols if s["total_bought"] > 0]
    closed = [s for s in traded if s["state"] == "종료"]
    holding = [s for s in traded if s["state"] != "종료"]
    # 머리글은 **오늘 실현분**이다 (positions 의 그날 행 = 스키마 v12 이후 하루치).
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
            f"{sign}  오늘 실현 **{net:+,.0f}원**"
            f"  (세전 {realized:+,.0f} · 비용 {fees:,.0f})\n"
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
        # 종료된 매매는 **사이클 전체**로 적는다. 호출부가 넘긴 값이 없으면 그날 행으로
        # 물러난다 — 요약이 통째로 빠지는 것보다 낫다.
        s_realized, s_fees = (cycle_pnl or {}).get(
            s["symbol"], (s["realized_pnl"], s["fees"])
        )
        s_net = s_realized - s_fees
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
        if s["state"] == "종료" and (s_realized or s_fees):
            # 보유 중인 종목에는 손익을 적지 않는다 — 아직 끝나지 않은 매매의 중간 손익은
            # 오해를 부른다. 오늘 실현한 몫은 머리글 합계에 이미 들어가 있다.
            # '매매 손익' 이라고 적는다. 머리글의 '오늘 실현' 과 기준이 달라서, 같은
            # 단어를 쓰면 두 숫자가 안 맞을 때 오류로 보인다. 이월된 매매는 여기 값이
            # 머리글보다 클 수 있고 그게 정상이다.
            lines.append(f"매매 손익 **{s_net:+,.0f}원** (세전 {s_realized:+,.0f})")
        if s["avg_price"] and s["high_price"]:
            high = (s["high_price"] - s["avg_price"]) / s["avg_price"]
            low = (s["low_price"] - s["avg_price"]) / s["avg_price"]
            # 보유기간은 매매 사이클 전체 기준이라 호출부(코어)가 계산해 넘긴다.
            # 당일 체결만 보면 이월 종목에서 빈칸이 되거나 실제보다 짧게 나온다.
            held = (holdings or {}).get(s["symbol"]) or _holding_time(own)
            lines.append(
                f"최고 `{high:+.1%}` / 최저 `{low:+.1%}`"
                + (f" · 보유 {held}" if held else "")
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

    # 보류 종목을 근접도보다 **먼저** 놓는다. '살 수 있었는데 못 산 것' 이 '내일 볼 만한
    # 것' 보다 급한 정보다.
    fields += build_blocked_fields(symbols, blocked or {})
    if field := build_proximity_field(  # 미진입 종목의 1선 근접도
        symbols, exclude=set(blocked or {})
    ):
        fields.append(field)

    if field := build_benchmark_field(symbols, index_rate):
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
