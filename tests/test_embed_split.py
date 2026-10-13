"""Discord embed 제한(25칸·6000자) 대응 — 2026-10-13 일일 요약 미발송 재발 방지.

사고: 진입 21종목인 날, 요약 필드가 26개가 되어 Discord 가 400 으로 거부했다.
요약은 하루 한 번이라, 조용히 사라지면 그날 결과를 폰에서 볼 수 없다.
"""

import asyncio

import pytest

from trader.notifier import (
    EMBED_MAX_FIELD_VALUE,
    EMBED_MAX_FIELDS,
    EMBED_MAX_TOTAL,
    build_daily_summary_embed,
    embed_chars,
    split_embed,
    split_text,
)


def _symbols(traded: int, closed: int, waiting: int = 60):
    rows = []
    for i in range(traded):
        done = i < closed
        rows.append(
            {
                "symbol": f"{i:06d}",
                "name": "엘브이엠씨홀딩스",
                "total_bought": 10,
                "state": "종료" if done else "1차 매수",
                "realized_pnl": -14_324.0 if done else 0.0,
                "fees": 557.0,
                "avg_price": 1_140.68,
                "remaining": 0 if done else 10,
                "high_price": 1_200.0,
                "low_price": 1_080.0,
                "day_low": 1_081.0,
                "day_open": 1_120.0,
                "day_close": 1_090.0,
                "line1": 1_150.0,
                "line2": 1_100.0,
                "line3": 1_090.0,
            }
        )
    for i in range(waiting):
        rows.append(
            {
                "symbol": f"9{i:05d}",
                "name": "미진입종목",
                "total_bought": 0,
                "state": "대기",
                "realized_pnl": 0.0,
                "fees": 0.0,
                "avg_price": 0.0,
                "remaining": 0,
                "high_price": 0.0,
                "low_price": 0.0,
                "day_low": 9_000.0 + i,
                "day_open": 9_500.0,
                "day_close": 9_400.0,
                "line1": 8_900.0,
                "line2": 8_000.0,
                "line3": 7_000.0,
            }
        )
    return rows


def _summary(traded, closed=None, blocked=11):
    symbols = _symbols(traded, traded if closed is None else closed)
    fills = [
        {
            "symbol": f"{i % traded:06d}",
            "side": "매수",
            "qty": 5,
            "price": 1_000,
            "ts": "",
        }
        for i in range(traded * 2)
    ]
    embed = build_daily_summary_embed(
        "2026-10-13",
        symbols,
        fills,
        1_000_000,
        {"value": 1e6, "pnl": -1.0, "rate": -0.1, "asset": 2e6},
        index_rate=-0.012,
        blocked={
            f"9{i:05d}": "최대 종목 수(7) 도달 — 자리가 나면" for i in range(blocked)
        },
    )
    # 코어가 뒤에 붙이는 두 필드 (일지 미작성 · 증권사 대조)
    embed["fields"] += [
        {"name": "✍ 일지 미작성 12건", "value": "가 · 나 · 다 외", "inline": False},
        {"name": "⚖️ 증권사 대조 — 확인 필요", "value": "차이", "inline": False},
    ]
    return embed


def _assert_within_limits(pages):
    for page in pages:
        assert len(page["fields"]) <= EMBED_MAX_FIELDS
        assert embed_chars(page) <= EMBED_MAX_TOTAL
        assert all(len(f["value"]) <= EMBED_MAX_FIELD_VALUE for f in page["fields"])


def test_사고_재현_21종목이면_필드가_25개를_넘는다():
    """수정 전에는 이것이 한 장으로 나가 거부됐다."""
    assert len(_summary(21)["fields"]) > EMBED_MAX_FIELDS


def test_종목을_자르지_않는다():
    """20종목 상한과 '외 N종목' 접기를 없앴다 — 나누면 되므로 자를 이유가 없다."""
    embed = _summary(21)
    names = [f["name"] for f in embed["fields"]]
    assert sum("엘브이엠씨홀딩스" in n for n in names) == 21
    assert not any(n.startswith("외 ") for n in names)


@pytest.mark.parametrize("traded", [1, 21, 24, 40, 60])
def test_몇_종목이든_모든_장이_제한_안에_들고_빠지는_필드가_없다(traded):
    embed = _summary(traded, closed=traded // 2)
    pages = split_embed(embed)

    _assert_within_limits(pages)
    flat = [f for p in pages for f in p["fields"]]
    assert flat == embed["fields"]  # 순서·내용 그대로, 하나도 안 빠진다


def test_한_장에_들어가면_지금과_똑같이_한_장이다():
    embed = _summary(3)
    pages = split_embed(embed)

    assert len(pages) == 1
    assert pages[0]["title"] == embed["title"]  # 쪽 번호 없음
    assert pages[0]["footer"] == embed["footer"]


def test_나뉘면_제목에_쪽_번호가_붙고_머리글은_첫_장_꼬리말은_마지막_장에():
    embed = _summary(40)
    pages = split_embed(embed)

    assert len(pages) >= 2
    n = len(pages)
    for i, page in enumerate(pages, start=1):
        assert page["title"] == f"{embed['title']} ({i}/{n})"
        assert page["color"] == embed["color"]
    assert pages[0]["description"] == embed["description"]
    assert all(p["description"] == "" for p in pages[1:])
    assert pages[-1]["footer"] == embed["footer"]
    assert all("footer" not in p for p in pages[:-1])


def test_보류_근접도_같은_묶음은_마지막_장에_온전히_남는다():
    embed = _summary(40)
    last = split_embed(embed)[-1]
    names = [f["name"] for f in last["fields"]]
    assert any(n.startswith("⏸️ 진입 못 함") for n in names)
    assert names[-1].startswith("⚖️ 증권사 대조")


def test_필드_수가_아니라_글자_수로도_나눈다():
    """필드 수가 적어도 글이 길면 6000자에 걸린다."""
    embed = {
        "title": "T",
        "description": "D",
        "fields": [{"name": f"f{i}", "value": "가" * 1000} for i in range(10)],
    }
    pages = split_embed(embed)

    assert len(pages) >= 2
    _assert_within_limits(pages)


def test_너무_긴_칸은_자른다():
    embed = {
        "title": "제" * 300,
        "description": "설" * 5000,
        "fields": [{"name": "이" * 300, "value": "값" * 2000}],
    }
    page = split_embed(embed)[0]
    assert len(page["title"]) <= 256
    assert len(page["description"]) <= 4096
    assert len(page["fields"][0]["name"]) <= 256
    assert len(page["fields"][0]["value"]) <= 1024


def test_글자_요약은_메시지_한도로_나눈다():
    text = "\n".join(f"줄 {i} " + "가" * 50 for i in range(200))
    chunks = split_text(text)
    assert all(len(c) <= 1900 for c in chunks)
    assert "\n".join(chunks) == text


# ── 발송 경로 ────────────────────────────────────────────────


class _Channel:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send(self, content=None, embed=None, **_kw):
        if self.fail and embed is not None:
            raise RuntimeError("400 Bad Request: fields Must be 25 or fewer")
        self.sent.append(embed if embed is not None else content)
        return type("M", (), {"id": 1})()


def _bot(channel):
    from trader.discord_bot import BotConfig, TraderBot

    bot = TraderBot(object(), BotConfig("T", 1, frozenset()))
    bot._channel = channel
    return bot


def test_봇은_넘치는_embed_를_여러_메시지로_보낸다():
    channel = _Channel()
    assert asyncio.run(_bot(channel).send_embed(_summary(40))) is True

    assert len(channel.sent) >= 2
    for embed in channel.sent:
        assert len(embed.fields) <= EMBED_MAX_FIELDS
        assert (
            len(embed) <= EMBED_MAX_TOTAL
        )  # discord.Embed 의 __len__ = 제한 계산 기준


def test_요약_발송이_실패하면_로그에_남기고_글자_요약을_보낸다(tmp_path):
    from trader.core import Core
    from trader.store import Store
    from trader.ui import bus

    class Bot:
        def __init__(self):
            self.texts = []

        async def send_embed(self, data):
            raise RuntimeError("400 Bad Request")

        async def send_text(self, text):
            self.texts.append(text)
            return True

        async def clear_dashboard(self):
            pass

        async def send_day_log(self, *a):
            return True

    core = Core(bus.Bus(), db_dir=str(tmp_path))
    core._date = "2026-10-13"
    core._store = Store(str(tmp_path / "t.db"))
    core._bot = Bot()
    asyncio.run(core.send_daily_summary())

    logs = " ".join(str(r) for r in core._store.day_log(core._date))
    core._store.close()
    assert "알림 발송 실패" in logs  # 화면을 다시 불러와도 남는다
    assert core._bot.texts and "요약 카드 발송에 실패" in core._bot.texts[0]
