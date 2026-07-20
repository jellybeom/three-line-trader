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
