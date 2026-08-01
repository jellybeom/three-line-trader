"""Discord 봇 — 설정 검증, 권한, 대시보드 구성, 코어 연동 테스트.

discord.py 네트워크 연결 없이 검증할 수 있는 부분(설정·표시 데이터·명령 경로)에
집중한다. 실제 게이트웨이 연결은 사용자 환경에서 확인한다.
"""

import asyncio

import pytest

from trader.discord_bot import (
    BotConfig,
    BotConfigError,
    build_dashboard_embed,
    build_status_lines,
    load_bot_config,
)
from trader.state_machine import Params, Position, State
from trader.store import Store
from trader.ui import bus

P = Params(
    line1=10_000, line2=9_000, line3=8_000, buy1_amount=1_000_000, buy2_amount=900_000
)


# ── 설정 ───────────────────────────────────────────────────────


def test_봇_설정을_읽는다(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[discord]\nbot_token = "T"\nchannel_id = "123"\n' 'allowed_users = ["456"]\n',
        encoding="utf-8",
    )
    config = load_bot_config(str(cfg))
    assert config.token == "T" and config.channel_id == 123
    assert config.allows(456) and not config.allows(999)


def test_설정이_없으면_봇을_쓰지_않는다는_신호(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[discord]\nwebhook_url = "https://hook"\n', encoding="utf-8")
    with pytest.raises(BotConfigError):
        load_bot_config(str(cfg))


def test_화이트리스트가_비면_설정을_거부한다(tmp_path):
    """비워두면 아무나 명령을 쓸 수 있다고 오해하기 쉬우므로 아예 막는다."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[discord]\nbot_token = "T"\nchannel_id = "1"\nallowed_users = []\n',
        encoding="utf-8",
    )
    with pytest.raises(BotConfigError, match="allowed_users"):
        load_bot_config(str(cfg))


def test_허용되지_않은_사용자는_거절된다():
    config = BotConfig("T", 1, frozenset({100}))
    assert config.allows(100)
    assert not config.allows(101)


# ── 표시 데이터 ────────────────────────────────────────────────


def _entries():
    return {
        "005930": {
            "name": "삼성전자",
            "params": P,
            "price": 10_400,
            "memo": "",
            "pos": Position(
                state=State.BUY1, avg_price=10_000, total_bought=100, remaining=100
            ),
        },
        "000660": {
            "name": "하이닉스",
            "params": P,
            "price": 9_500,
            "memo": "",
            "pos": Position(
                state=State.BUY1, avg_price=10_000, total_bought=50, remaining=50
            ),
        },
        "035420": {
            "name": "NAVER",
            "params": P,
            "price": 12_000,
            "memo": "",
            "pos": Position(),
        },  # 대기 — 목록에 나오지 않아야 한다
    }


def test_보유_종목만_표시하고_등락에_따라_아이콘이_다르다():
    lines = build_status_lines(_entries())
    assert len(lines) == 2  # 대기 종목 제외
    joined = "\n".join(lines)
    assert "🟢" in joined and "🔴" in joined
    assert "NAVER" not in joined
    assert "+4.00%" in joined and "-5.00%" in joined


def test_종료_종목은_세후_손익으로_표시된다():
    entries = {
        "005930": {
            "name": "삼성전자",
            "params": P,
            "price": 9_000,
            "memo": "",
            "pos": Position(
                state=State.CLOSED,
                avg_price=10_000,
                total_bought=100,
                remaining=0,
                realized_pnl=-50_000,
                fees=1_500,
            ),
        }
    }
    line = build_status_lines(entries)[0]
    assert "종료" in line and "-51,500원" in line


class FakeCore:
    def __init__(self, entries, running=True, kiwoom=True):
        self.entries = entries
        self.running = running
        self.trade_date = "2026-08-03"
        self.mode_real = True
        self.deposit_display = 842_100
        self.kiwoom_connected = kiwoom


def test_대시보드는_감시_상태와_손익을_담는다():
    embed = build_dashboard_embed(FakeCore(_entries()))
    assert "감시 중" in embed["title"] and "실전" in embed["title"]
    assert embed["color"] == 0x2E7D32
    assert "예수금 842,100원" in embed["footer"]["text"]
    assert "갱신" in embed["footer"]["text"]


def test_감시_중지_상태는_회색으로_표시된다():
    embed = build_dashboard_embed(FakeCore(_entries(), running=False))
    assert "감시 중지" in embed["title"] and embed["color"] == 0x616161


def test_보류_종목도_대시보드에_보인다():
    embed = build_dashboard_embed(
        FakeCore(_entries()), blocked={"035420": "최대 종목 수(3) 도달"}
    )
    assert "⏸️" in embed["description"] and "NAVER" in embed["description"]


def test_키움이_끊기면_대시보드가_경고한다():
    """감시 중이어도 키움이 끊겨 있으면 매매가 되지 않는다 — 한눈에 보여야 한다."""
    embed = build_dashboard_embed(FakeCore(_entries(), kiwoom=False))
    assert "키움 연결 안 됨" in embed["description"]
    assert embed["color"] == 0xEF6C00  # 주황 경고


def test_보유가_없으면_안내_문구():
    entries = {
        "005930": {
            "name": "삼성전자",
            "params": P,
            "price": 0,
            "memo": "",
            "pos": Position(),
        }
    }
    embed = build_dashboard_embed(FakeCore(entries))
    assert "보유 중인 종목이 없습니다" in embed["description"]


# ── 코어 연동 ──────────────────────────────────────────────────


@pytest.fixture
def core(tmp_path):
    from trader.core import Core

    c = Core(bus.Bus(), db_dir=str(tmp_path))
    c._date = "2026-08-03"
    c._store = Store(str(tmp_path / "t.db"))
    for symbol, name in (("005930", "삼성전자"), ("000660", "SK하이닉스")):
        c._store.register_symbol(c._date, symbol, name, P)
        c._entries[symbol] = {
            "name": name,
            "params": P,
            "pos": Position(),
            "price": 0,
            "memo": "",
            "high": 0.0,
            "low": 0.0,
        }
    yield c
    c._store.close()


def test_종목_검색은_코드와_이름을_모두_지원한다(core):
    assert core.find_symbol("005930") == "005930"
    assert core.find_symbol("하이닉스") == "000660"
    assert core.find_symbol("없는종목") is None


def test_이름이_여러개_걸리면_고르지_않는다(core):
    """'삼성' 처럼 모호한 입력으로 엉뚱한 종목을 건드리지 않게 한다."""
    core._entries["028050"] = {
        "name": "삼성E&A",
        "params": P,
        "pos": Position(),
        "price": 0,
        "memo": "",
        "high": 0.0,
        "low": 0.0,
    }
    assert core.find_symbol("삼성") is None
    assert core.find_symbol("삼성전자") == "005930"


def test_봇_요청은_명령_큐를_통해_전달된다(core):
    """봇이 코어 상태를 직접 바꾸지 않고 UI 와 같은 경로를 쓰는지 확인."""
    core.request_running(True)
    core.request_notify_level("에러만")
    core.request_daily_summary()
    core.request_chart("005930")

    got = []
    while not core._bus.commands.empty():
        got.append(core._bus.commands.get_nowait())
    assert isinstance(got[0], bus.SetRunning) and got[0].running is True
    assert isinstance(got[1], bus.SetNotifyLevel) and got[1].level == "에러만"
    assert isinstance(got[2], bus.RequestDailySummary)
    assert isinstance(got[3], bus.ChartRequest) and got[3].symbol == "005930"
    assert core.running is False  # 큐에 넣었을 뿐 아직 바뀌지 않았다


def test_봇_설정이_없으면_코어는_그대로_동작한다(core, tmp_path):
    core._config_path = str(tmp_path / "없는파일.toml")
    asyncio.run(core._start_bot())
    assert core._bot is None  # 봇 없이도 예외 없이 진행


def test_대시보드_갱신은_봇이_없으면_아무_일도_하지_않는다(core):
    asyncio.run(core._tick_bot())  # 예외가 나지 않으면 통과


# ── 슬래시 명령 (실제 discord.py 객체로 검증) ──────────────────


class _CommandCore:
    entries: dict = {}
    running = False
    trade_date = "2026-08-03"
    mode_real = True
    deposit_display = None
    kiwoom_connected = True

    def __init__(self):
        self.calls = []

    def request_running(self, on):
        self.calls.append(("running", on))

    def request_notify_level(self, level):
        self.calls.append(("notify", level))

    def request_daily_summary(self):
        self.calls.append(("summary",))

    def request_chart(self, symbol):
        self.calls.append(("chart", symbol))

    def find_symbol(self, text):
        return "005930" if text == "삼성전자" else None


class _Response:
    def __init__(self):
        self.msg = None

    async def send_message(self, content=None, **kw):
        self.msg = content or "(embed)"

    async def defer(self):
        pass


class _Interaction:
    def __init__(self, user_id, channel_id):
        self.user = type("U", (), {"id": user_id})()
        self.channel_id = channel_id
        self.response = _Response()


def _tree_with_commands(core, allowed=100, channel=999):
    import discord
    from discord import app_commands

    from trader.discord_bot import TraderBot

    bot = TraderBot(core, BotConfig("T", channel, frozenset({allowed})))
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    bot._register_commands(tree)
    return {c.name: c for c in tree.get_commands()}


def test_슬래시_명령이_모두_등록된다():
    commands = _tree_with_commands(_CommandCore())
    assert set(commands) == {"상태", "예수금", "요약", "차트", "감시", "알림"}
    # 주문을 내는 조작은 일부러 넣지 않는다 (계정 탈취 시 피해 제한)
    assert "청산" not in commands and "삭제" not in commands


def test_허용되지_않은_사용자의_명령은_코어에_닿지_않는다():
    from discord import app_commands

    core = _CommandCore()
    watch = _tree_with_commands(core)["감시"]
    choice = app_commands.Choice(name="시작", value="start")

    other = _Interaction(101, 999)  # 다른 사용자
    asyncio.run(watch.callback(other, choice))
    assert "권한이 없습니다" in other.response.msg
    assert core.calls == []

    wrong_channel = _Interaction(100, 111)  # 다른 채널
    asyncio.run(watch.callback(wrong_channel, choice))
    assert "채널에서만" in wrong_channel.response.msg
    assert core.calls == []


def test_허용된_사용자의_명령은_코어_요청으로_이어진다():
    from discord import app_commands

    core = _CommandCore()
    commands = _tree_with_commands(core)

    ok = _Interaction(100, 999)
    asyncio.run(
        commands["감시"].callback(ok, app_commands.Choice(name="시작", value="start"))
    )
    assert core.calls == [("running", True)]

    asyncio.run(
        commands["알림"].callback(
            _Interaction(100, 999), app_commands.Choice(name="에러만", value="에러만")
        )
    )
    assert core.calls[-1] == ("notify", "에러만")


def test_없는_종목은_차트를_요청하지_않는다():
    core = _CommandCore()
    chart = _tree_with_commands(core)["차트"]
    missing = _Interaction(100, 999)
    asyncio.run(chart.callback(missing, "없는종목"))
    assert "없습니다" in missing.response.msg
    assert core.calls == []

    asyncio.run(chart.callback(_Interaction(100, 999), "삼성전자"))
    assert core.calls == [("chart", "005930")]


# ── 발송 경로 (웹훅 제거 후 봇이 유일한 경로) ──────────────────


class _FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, files=None):
        self.sent.append({"content": content, "embed": embed, "files": files})
        return type("M", (), {"id": 1})()


def _bot_with_channel():
    from trader.discord_bot import TraderBot

    bot = TraderBot(_CommandCore(), BotConfig("T", 999, frozenset({100})))
    channel = _FakeChannel()
    bot._channel = channel
    return bot, channel


def test_텍스트와_embed와_이미지를_봇이_보낸다(tmp_path):
    bot, channel = _bot_with_channel()
    png = tmp_path / "c.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")

    async def scenario():
        assert await bot.send_text("체결 알림") is True
        assert (
            await bot.send_embed({"title": "T", "description": "D", "color": 1}) is True
        )
        assert await bot.send_images([str(png)], "차트") is True

    asyncio.run(scenario())
    assert channel.sent[0]["content"] == "체결 알림"
    assert channel.sent[1]["embed"].title == "T"
    assert len(channel.sent[2]["files"]) == 1 and channel.sent[2]["content"] == "차트"


def test_채널이_준비되지_않으면_조용히_버린다():
    """연결 전·재연결 중 발송은 실패해도 매매를 막지 않는다 (로그·DB 에는 남는다)."""
    from trader.discord_bot import TraderBot

    bot = TraderBot(_CommandCore(), BotConfig("T", 999, frozenset({100})))
    assert bot.ready is False

    async def scenario():
        assert await bot.send_text("x") is False
        assert await bot.send_embed({"title": "x"}) is False
        assert await bot.send_images(["없는파일.png"]) is False

    asyncio.run(scenario())


def test_긴_메시지는_잘라서_보낸다():
    bot, channel = _bot_with_channel()
    asyncio.run(bot.send_text("가" * 3000))
    assert len(channel.sent[0]["content"]) == 1900


def test_코어는_봇이_없어도_알림_없이_동작한다(core):
    """봇 미설정 상태에서 로그·요약이 예외 없이 처리되어야 한다."""
    core._bot = None
    core._notify_level = "전체"
    core._log("005930", "체결", "테스트")
    asyncio.run(core.send_daily_summary())
    assert core._notice_batch == []
