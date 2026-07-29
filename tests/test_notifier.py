"""notifier 단위 테스트 — 알림 수준 필터, 메시지 형식, 발송 요청/오류 처리."""

import pytest

from trader.notifier import (
    DiscordNotifier,
    NotifierError,
    format_message,
    load_webhook,
    should_notify,
)

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


def test_메시지_형식():
    assert (
        format_message("005930", "체결", "매수 7주") == "**[체결]** 005930 · 매수 7주"
    )
    assert format_message("시스템", "연결", "재연결됨") == "**[연결]** 재연결됨"


def test_매매_요약_형식():
    from trader.notifier import format_trade

    assert (
        format_trade("삼성전자", "005930", "1선 이탈 → 1차 매수", 38, 13170)
        == "🟢 **삼성전자(005930)** 1차 매수 — 38주 @ 13,170"
    )
    assert (
        format_trade("삼성전자", "005930", "평단 +5% 도달 → 2차 익절", 50, 72400)
        == "💰 **삼성전자(005930)** 2차 익절 — 50주 @ 72,400"
    )
    assert (
        format_trade("흥구석유", "024060", "3선 이탈 → 전량 손절", 76, 13068, -4750)
        == "🛑 **흥구석유(024060)** 전량 손절 — 76주 @ 13,068\n실현손익 **-4,750원**"
    )
    assert (
        format_trade(
            "모나리자", "012690", "3선 갭 이탈 → 진입 금지, 당일 종료", 0, 1700
        )
        == "⚪ **모나리자(012690)** 진입 금지, 당일 종료"
    )


# ── webhook 로드 / 발송 ────────────────────────────────────────


def test_webhook_미설정은_명확한_에러(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[discord]\nwebhook_url = ""\n', encoding="utf-8")
    with pytest.raises(NotifierError, match="webhook_url"):
        load_webhook(cfg)


def test_webhook_로드(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[discord]\nwebhook_url = "https://discord.com/api/webhooks/x"\n',
        encoding="utf-8",
    )
    assert load_webhook(cfg) == "https://discord.com/api/webhooks/x"


def _fake_response(status: int, text: str = ""):
    class R:
        status_code = status

        @property
        def text(self):
            return text

    return R()


def test_발송_요청_형식과_204_성공(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured.update(url=url, body=json)
        return _fake_response(204)

    monkeypatch.setattr("trader.notifier.requests.post", fake_post)
    DiscordNotifier("https://hook").send("**[체결]** 005930 · 매수")
    assert captured["url"] == "https://hook"
    assert captured["body"] == {"content": "**[체결]** 005930 · 매수"}


def test_발송_실패는_명확한_에러(monkeypatch):
    monkeypatch.setattr(
        "trader.notifier.requests.post",
        lambda *a, **k: _fake_response(429, "rate limited"),
    )
    with pytest.raises(NotifierError, match="429"):
        DiscordNotifier("https://hook").send("x")


def test_긴_메시지는_1900자로_절단(monkeypatch):
    sent = {}
    monkeypatch.setattr(
        "trader.notifier.requests.post",
        lambda url, json=None, timeout=None: sent.update(json) or _fake_response(204),
    )
    DiscordNotifier("https://hook").send("가" * 3000)
    assert len(sent["content"]) == 1900  # Discord 2000자 제한 여유


# ── 일일 요약 embed (가독성) ───────────────────────────────────


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


def test_embed는_embeds_필드로_전송된다(monkeypatch):
    from trader.notifier import DiscordNotifier

    captured = {}

    def fake_post(url, json=None, data=None, files=None, timeout=None):
        captured.update(json or {})

        class R:
            status_code = 204
            text = ""

        return R()

    monkeypatch.setattr("trader.notifier.requests.post", fake_post)
    assert (
        DiscordNotifier("https://hook").send_embed({"title": "t", "color": 1}) is True
    )
    assert captured["embeds"][0]["title"] == "t"


# ── 묶음 알림 embed ────────────────────────────────────────────


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
