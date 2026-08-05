"""notifier 단위 테스트 — 알림 수준 필터, 메시지·embed 형식 (발송은 봇 담당)."""

import pytest

from trader.notifier import format_message, should_notify

# ── 알림 수준 필터 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "level,symbol,kind,expected",
    [
        ("전체", "시스템", "연결", True),
        ("전체", "005930", "체결", True),
        ("매매만 (시스템 제외)", "005930", "체결", True),
        ("매매만 (시스템 제외)", "005930", "에러", True),
        ("매매만 (시스템 제외)", "시스템", "연결", False),
        ("에러만", "005930", "체결", False),
        ("에러만", "005930", "에러", True),
        ("에러만", "시스템", "경고", True),
        ("끔", "005930", "에러", False),
    ],
)
def test_알림_수준_필터(level, symbol, kind, expected):
    assert should_notify(level, symbol, kind) is expected


def _report():
    symbols = [
        {
            "symbol": "010060",
            "name": "OCI홀딩스",
            "memo": "",
            "state": "종료",
            "avg_price": 200_500,
            "total_bought": 1,
            "remaining": 0,
            "realized_pnl": 200,
            "fees": 69,
            "high_price": 210_000,
            "low_price": 198_000,
        },
        {
            "symbol": "475150",
            "name": "SK이터닉스",
            "memo": "",
            "state": "2차 매수",
            "avg_price": 61_000,
            "total_bought": 8,
            "remaining": 8,
            "realized_pnl": 0,
            "fees": 66,
            "high_price": 62_900,
            "low_price": 59_800,
        },
    ]
    fills = [
        {
            "ts": "2026-07-27 09:31:00",
            "symbol": "010060",
            "side": "매수",
            "qty": 1,
            "price": 200_500,
            "reason": "",
        },
        {
            "ts": "2026-07-27 14:10:00",
            "symbol": "010060",
            "side": "매도",
            "qty": 1,
            "price": 200_700,
            "reason": "",
        },
    ]
    return symbols, fills


def test_요약_embed는_손익_부호에_따라_색이_바뀐다():
    from trader.notifier import build_daily_summary_embed

    symbols, fills = _report()
    embed = build_daily_summary_embed("2026-07-27", symbols, fills, deposit=438_191)
    assert embed["color"] == 0x2E7D32  # 세후 +65원 → 초록

    symbols[0]["realized_pnl"] = -5_000
    loss = build_daily_summary_embed("2026-07-27", symbols, fills)
    assert loss["color"] == 0xC62828


def test_요약_embed에_종목별_필드와_이월_안내가_담긴다():
    from trader.notifier import build_daily_summary_embed

    symbols, fills = _report()
    embed = build_daily_summary_embed("2026-07-27", symbols, fills, deposit=438_191)
    names = [f["name"] for f in embed["fields"]]
    assert any("OCI홀딩스(010060)" in n for n in names)
    assert any("⚠️" in n for n in names)  # 보유 중 종목 경고 아이콘
    assert any("이월" in f["value"] for f in embed["fields"])
    assert "438,191원" in embed["footer"]["text"]


def test_매매가_없으면_회색_embed():
    from trader.notifier import build_daily_summary_embed

    embed = build_daily_summary_embed("2026-07-25", [], [])
    assert embed["color"] == 0x616161
    assert "없습니다" in embed["description"]


def test_여러_건은_한_장의_embed로_묶인다():
    from trader.notifier import build_batch_embed

    items = [("등록", f"종목{i}(00000{i})", "대기, 잔량 0주") for i in range(5)]
    embed = build_batch_embed(items)
    assert "등록 5건" in embed["title"]
    assert embed["description"].count("\n") == 4  # 5줄
    assert "fields" not in embed  # 경고가 없으면 필드도 없다


def test_경고는_별도_필드로_분리되고_색이_바뀐다():
    from trader.notifier import build_batch_embed

    items = [
        ("등록", "삼성전자(005930)", "대기"),
        ("경고", "동아쏘시오(000640)", "1차 매수 예상 2주"),
    ]
    embed = build_batch_embed(items)
    assert embed["color"] == 0xEF6C00  # 주황 — 확인 필요
    assert "확인 필요 1건" in embed["fields"][0]["name"]
    assert "동아쏘시오" in embed["fields"][0]["value"]
    assert "동아쏘시오" not in embed["description"]  # 본문에는 중복되지 않는다


def test_너무_많으면_잘라내고_남은_건수를_알린다():
    from trader.notifier import build_batch_embed

    embed = build_batch_embed([("등록", f"종목{i}", "대기") for i in range(60)])
    assert "외 20건" in embed["description"]
    assert len(embed["description"]) <= 4000


# ── 경보 embed / 에러 압축 ─────────────────────────────────────


def test_API_오류는_사람이_읽을_부분만_남긴다():
    from trader.notifier import shorten_error

    raw = (
        "주문 실패(3회): kt10000 실패 (HTTP 200, code 20): [2000] "
        "(855056:매수증거금이 부족합니다. 127주 매수가능) — 30초간 재시도 보류"
    )
    out = shorten_error(raw)
    assert "매수증거금이 부족합니다. 127주 매수가능" in out
    assert "HTTP" not in out and "kt10000" not in out
    assert out.startswith("주문 실패(3회):") and out.endswith(
        "재시도 보류"
    )  # 문맥은 유지


def test_일반_괄호_설명은_그대로_둔다():
    from trader.notifier import shorten_error

    text = "주문가능금액 필드를 찾지 못함 (kt00001 응답 필드 변경 가능성)"
    assert shorten_error(text) == text


def test_경보는_종류별로_색과_아이콘이_다르다():
    from trader.notifier import build_alert_embed

    assert build_alert_embed("에러", "005930", "x")["color"] == 0xC62828
    assert build_alert_embed("경고", "005930", "x")["color"] == 0xEF6C00
    assert build_alert_embed("보류", "005930", "x")["color"] == 0xEF6C00
    assert "⛔" in build_alert_embed("에러", "005930", "x")["title"]


def test_종료_결산은_세후_손익_부호로_색이_정해진다():
    from trader.notifier import build_trade_embed

    loss = build_trade_embed(
        "대원전선", "006340", "3선 이탈 → 전량 손절", 43, 10_650, -31_850, 1_180
    )
    assert loss["color"] == 0xC62828
    assert "-33,030원" in loss["description"]  # 세후
    assert "종료" in loss["title"]

    win = build_trade_embed(
        "삼성전자", "005930", "+7% → 전량 청산", 10, 80_000, 50_000, 900
    )
    assert win["color"] == 0x2E7D32 and "💰" in win["title"]


def test_종류마다_아이콘과_색이_지정된다():
    from trader.notifier import build_alert_embed

    cases = {"삭제": "🗑️", "연결": "🔗", "감시": "👁️", "설정": "⚙️", "이월": "📦"}
    for kind, icon in cases.items():
        embed = build_alert_embed(kind, "005930", "내용")
        assert icon in embed["title"] and kind in embed["title"]
    assert build_alert_embed("연결", "시스템", "x")["color"] == 0x00838F
    assert "시스템" not in build_alert_embed("연결", "시스템", "x")["title"]


def test_묶음_제목에도_아이콘이_붙는다():
    from trader.notifier import build_batch_embed

    embed = build_batch_embed(
        [("등록", "A", "x"), ("등록", "B", "x"), ("삭제", "C", "x")]
    )
    assert "➕ 등록 2건" in embed["title"] and "🗑️ 삭제 1건" in embed["title"]


def test_요약에_계좌_평가와_추정자산이_들어간다():
    """이월 종목이 있는 날은 실현손익만으로 성과를 판단할 수 없다."""
    from trader.notifier import build_daily_summary_embed

    symbols, fills = _report()
    embed = build_daily_summary_embed(
        "2026-07-27",
        symbols,
        fills,
        deposit=1_029_296,
        account={"value": 315_425, "pnl": 6_801, "rate": 2.21, "asset": 1_344_071},
    )
    account_field = [f for f in embed["fields"] if "계좌" in f["name"]]
    assert account_field, "계좌 필드가 없음"
    assert "315,425원" in account_field[0]["value"]
    assert "+2.21%" in account_field[0]["value"]
    assert "1,344,071원" in account_field[0]["value"]
    assert "주문가능 1,029,296원" in embed["footer"]["text"]


def test_계좌_정보가_없으면_계좌_필드도_없다():
    from trader.notifier import build_daily_summary_embed

    symbols, fills = _report()
    embed = build_daily_summary_embed("2026-07-27", symbols, fills)
    assert not [f for f in embed["fields"] if "계좌" in f["name"]]


# ── 1선 근접도 (2026-08-05) ────────────────────────────────────


def _waiting(symbol, name, line1, day_low):
    return {
        "symbol": symbol,
        "name": name,
        "memo": "",
        "line1": line1,
        "state": "대기",
        "avg_price": 0,
        "total_bought": 0,
        "remaining": 0,
        "realized_pnl": 0,
        "fees": 0,
        "high_price": 0,
        "low_price": 0,
        "day_low": day_low,
    }


def test_근접도는_1선에_가까운_순으로_보여준다():
    """진입이 없는 날, 설정이 보수적인지 시장이 안 맞는지 구분하는 근거."""
    from trader.notifier import build_daily_summary_embed

    symbols = [
        _waiting("000660", "SK하이닉스", 50_000, 60_000),  # +20%
        _waiting("005430", "한국공항", 79_100, 79_700),  # +0.8%
        _waiting("098460", "고영", 20_000, 21_900),
    ]  # +9.5%
    embed = build_daily_summary_embed("2026-08-05", symbols, [])
    field = [f for f in embed["fields"] if "근접도" in f["name"]][0]
    lines = field["value"].splitlines()
    assert "한국공항" in lines[0]  # 가장 가까운 종목이 맨 위
    assert "SK하이닉스" in lines[2]
    assert "3% 이내 **1종목**" in field["value"]
    assert "10% 이내 **2종목**" in field["value"]


def test_진입한_종목은_근접도에서_빠진다():
    from trader.notifier import build_daily_summary_embed

    entered = _waiting("005930", "삼성전자", 10_000, 9_800)
    entered["total_bought"] = 10  # 실제로 진입한 종목
    symbols = [entered, _waiting("005430", "한국공항", 79_100, 79_700)]
    field = [
        f
        for f in build_daily_summary_embed("2026-08-05", symbols, [])["fields"]
        if "근접도" in f["name"]
    ][0]
    assert "삼성전자" not in field["value"]
    assert "총 1종목" in field["value"]


def test_틱이_없어_최저가가_없으면_근접도를_넣지_않는다():
    """감시를 켜지 않은 날이나 휴장일에 빈 섹션이 붙지 않게."""
    from trader.notifier import build_daily_summary_embed

    embed = build_daily_summary_embed(
        "2026-08-05", [_waiting("005430", "한국공항", 79_100, 0)], []
    )
    assert not [f for f in embed["fields"] if "근접도" in f["name"]]
