"""주문 실패 재시도 — 2026-11-09 셀바스헬스케어 재발 방지.

사고: 매도(kt10001)가 HTTP 502 로 1시간 넘게 4회 연속 실패했다. 같은 시각 다른 종목
주문은 성공했다. 프로그램은 3회째에 '계좌 상태를 직접 확인하세요' 만 보냈고, 로그에는
몇 주를 왜 팔려 했는지도, 키움이 무슨 응답을 줬는지도 남지 않았다.
"""

import asyncio

import pytest

from trader.broker import Broker, BrokerError
from trader.core import Core
from trader.state_machine import Params, Position, State
from trader.store import Store
from trader.ui import bus
from trader.watcher import Tick

P = Params(
    line1=10_000, line2=9_000, line3=8_000, buy1_amount=1_000_000, buy2_amount=900_000
)
HELD = Position(state=State.BUY1, avg_price=10_000, total_bought=100, remaining=100)
STOP = 7_900  # 3선 아래 → 전량 손절 매도


class FakeBroker:
    def __init__(self):
        self.orders = []
        self.fail_sell = 0  # 앞으로 실패시킬 매도 횟수
        self.fail_buy = 0
        self.detail = {}
        self.detail_error = None

    def _maybe_fail(self, side):
        attr = "fail_sell" if side == "매도" else "fail_buy"
        if getattr(self, attr):
            setattr(self, attr, getattr(self, attr) - 1)
            raise BrokerError(
                "kt10001 응답이 JSON 이 아님 (HTTP 502) — 502 Bad Gateway"
            )

    def buy(self, symbol, qty):
        self._maybe_fail("매수")
        self.orders.append(("매수", symbol, qty))
        return f"ORD{len(self.orders)}"

    def sell(self, symbol, qty):
        self._maybe_fail("매도")
        self.orders.append(("매도", symbol, qty))
        return f"ORD{len(self.orders)}"

    def deposit(self):
        return 100_000_000

    def holdings(self):
        return {}

    def holdings_detail(self):
        if self.detail_error:
            raise self.detail_error
        return self.detail

    def open_orders(self, symbol=None):
        return []


class FakeBot:
    def __init__(self):
        self.embeds = []

    async def send_embed(self, data):
        self.embeds.append(data)
        return True

    async def send_text(self, text):
        return True


@pytest.fixture
def core(tmp_path):
    c = Core(bus.Bus())
    c._store = Store(tmp_path / "t.db")
    c._date = "2026-11-09"
    c._broker = FakeBroker()
    c._bot = FakeBot()
    c._notify_level = "전체"
    c._running = True
    yield c
    c._store.close()


def _add(c, symbol, pos):
    c._store.register_symbol(c._date, symbol, "셀바스헬스케어", P, pos)
    c._entries[symbol] = {"name": "셀바스헬스케어", "params": P, "pos": pos, "price": 0}


def _logs(c):
    return " ".join(str(r) for r in c._store.day_log(c._date))


async def _tick(c, symbol, price):
    c._order_fail.get(symbol, {})["until"] = 0.0  # 30초 쿨다운을 건너뛴다
    await c._on_tick(Tick(symbol, price, ""))
    for _ in range(5):  # 즉시 알림 태스크가 돌게 한다
        await asyncio.sleep(0)


def _alerts(c, word):
    return [e for e in c._bot.embeds if word in str(e)]


# ── 진단 기록 ────────────────────────────────────────────────


def test_JSON_아닌_응답은_본문_앞부분을_남긴다(monkeypatch):
    class Resp:
        status_code = 502
        text = (
            "<html><head><title>502 Bad Gateway</title></head>"
            "<body><center><h1>502 Bad Gateway</h1></center></body></html>"
        )

        def json(self):
            raise ValueError("not json")

    broker = Broker.__new__(Broker)
    broker._lock = __import__("threading").Lock()
    broker._last_call = 0.0
    monkeypatch.setattr(broker, "_post", lambda *a: Resp())

    with pytest.raises(BrokerError) as info:
        broker._request_once("/p", "kt10001", {})

    msg = str(info.value)
    assert "응답이 JSON 이 아님 (HTTP 502)" in msg
    assert "502 Bad Gateway" in msg and "<" not in msg  # 태그는 걷는다


def test_실패_로그에_무엇을_하려다_실패했는지_남긴다(core):
    _add(core, "208370", HELD)
    core._broker.fail_sell = 1

    asyncio.run(_tick(core, "208370", STOP))

    log = _logs(core)
    assert "매도 100주" in log and "전량 손절" in log and "주문 실패(1회)" in log


# ── 수동 매도 알림 ───────────────────────────────────────────


def test_매도가_2회_연속_실패하면_즉시_수동_매도를_요청한다(core):
    _add(core, "208370", HELD)
    core._broker.fail_sell = 9
    core._broker.detail = {"208370": (100, 100)}

    async def run():
        await _tick(core, "208370", STOP)
        assert not _alerts(core, "수동 매도 필요")  # 1회는 일시 오류일 수 있다
        await _tick(core, "208370", STOP)

    asyncio.run(run())

    alerts = _alerts(core, "수동 매도 필요")
    assert len(alerts) == 1
    text = str(alerts[0])
    assert "매도 100주" in text and "-21.00%" in text and "보유 100주" in text


def test_수동_매도_알림은_하루_한_번이고_3회면_주문을_멈춘다(core):
    _add(core, "208370", HELD)
    core._broker.fail_sell = 9
    core._broker.detail = {"208370": (100, 100)}

    async def run():
        for _ in range(4):
            await _tick(core, "208370", STOP)

    asyncio.run(run())

    assert len(_alerts(core, "수동 매도 필요")) == 1
    assert "208370" in core._order_blocked
    assert "영웅문에서 직접 매도" in _logs(core)
    assert core._broker.orders == []


def test_알린_뒤_자동_매도가_성공하면_직접_팔지_말라고_다시_알린다(core):
    """일시 오류(같은 날 파수AI: 2회 실패 후 성공)면 사람과 프로그램이 둘 다 팔 수 있다."""
    _add(core, "208370", HELD)
    core._broker.fail_sell = 2
    core._broker.detail = {"208370": (100, 100)}

    async def run():
        for _ in range(3):
            await _tick(core, "208370", STOP)

    asyncio.run(run())

    assert core._broker.orders == [("매도", "208370", 100)]
    assert _alerts(core, "직접 매도하지 마세요")


def test_매수_실패는_수동_매도를_요청하지_않는다(core):
    _add(core, "161390", Position())
    core._broker.fail_buy = 9

    async def run():
        for _ in range(2):
            await _tick(core, "161390", 9_950)

    asyncio.run(run())

    assert not _alerts(core, "수동 매도 필요")


# ── 재주문 전 잔고 확인 ──────────────────────────────────────


def test_실패했던_매도는_잔고가_그대로일_때만_다시_보낸다(core):
    _add(core, "208370", HELD)
    core._broker.fail_sell = 1
    core._broker.detail = {"208370": (100, 100)}

    async def run():
        await _tick(core, "208370", STOP)
        await _tick(core, "208370", STOP)

    asyncio.run(run())

    assert core._broker.orders == [("매도", "208370", 100)]


def test_실패로_보인_매도가_실제로_체결됐으면_다시_보내지_않는다(core):
    """502 인데 주문은 접수된 경우 — 그대로 다시 보내면 같은 물량이 두 번 나간다."""
    _add(core, "208370", HELD)
    core._broker.fail_sell = 1
    core._broker.detail = {}  # 계좌에서 이미 다 팔렸다

    async def run():
        await _tick(core, "208370", STOP)
        await _tick(core, "208370", STOP)

    asyncio.run(run())

    assert core._broker.orders == []
    assert "208370" in core._order_blocked
    assert _alerts(core, "재주문 중단")


def test_잔고를_확인하지_못하면_이번_재주문은_보내지_않는다(core):
    _add(core, "208370", HELD)
    core._broker.fail_sell = 1
    core._broker.detail_error = BrokerError("kt00018 실패")

    async def run():
        await _tick(core, "208370", STOP)
        await _tick(core, "208370", STOP)

    asyncio.run(run())

    assert core._broker.orders == []
    assert "208370" not in core._order_blocked  # 다음에 다시 확인한다
    assert "잔고 확인 실패" in _logs(core)


def test_실패로_보인_매수가_실제로_체결됐으면_다시_사지_않는다(core):
    _add(core, "161390", Position())
    core._broker.fail_buy = 1
    core._broker.detail = {"161390": (100, 100)}  # 이미 들어와 있다

    async def run():
        await _tick(core, "161390", 9_950)
        await _tick(core, "161390", 9_950)

    asyncio.run(run())

    assert core._broker.orders == []
    assert "161390" in core._order_blocked


def test_실패한_적_없으면_잔고를_조회하지_않는다(core):
    """평소 주문 경로에 조회를 끼워 넣지 않는다 — 느려지고 kt00018 유량만 쓴다."""
    _add(core, "208370", HELD)
    core._broker.detail_error = AssertionError("조회하면 안 된다")

    asyncio.run(_tick(core, "208370", STOP))

    assert core._broker.orders == [("매도", "208370", 100)]
