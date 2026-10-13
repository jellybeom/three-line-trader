"""최대 종목 수 관문 — 2026-10-12 초과 진입 사고의 재발 방지.

사고: 최대 7종목인데 10종목을 보유했다. 1차 매수 주문을 넣은 종목은 체결 통보가
올 때까지 상태가 '대기' 라서 자리 계산에서 빠졌다. 한 자리가 비자 보류 중이던
4종목이 체결 전 1초 사이에 모두 관문을 통과했다 (09:58:08 손절 → 09:58:09~10 매수 4건).
"""

import asyncio

import pytest

from trader.broker import BrokerError
from trader.core import Core
from trader.state_machine import Params, Position, State, occupies_slot
from trader.store import Store
from trader.ui import bus
from trader.watcher import Tick

P = Params(
    line1=10_000, line2=9_000, line3=8_000, buy1_amount=1_000_000, buy2_amount=900_000
)
HELD = Position(state=State.BUY1, avg_price=10_000, total_bought=100, remaining=100)


class Broker:
    def __init__(self, deposit=100_000_000):
        self.deposit_value = deposit
        self.orders = []
        self.fail_buy = set()

    def buy(self, symbol, qty):
        if symbol in self.fail_buy:
            raise BrokerError("주문 거부")
        self.orders.append(("매수", symbol, qty))
        return f"ORD{len(self.orders)}"

    def sell(self, symbol, qty):
        self.orders.append(("매도", symbol, qty))
        return f"ORD{len(self.orders)}"

    def deposit(self):
        return self.deposit_value

    def holdings(self):
        return {}

    def holdings_detail(self):
        return {}


@pytest.fixture
def core(tmp_path):
    c = Core(bus.Bus())
    c._store = Store(tmp_path / "t.db")
    c._date = "2026-10-12"
    c._broker = Broker()
    c._running = True
    yield c
    c._store.close()


def _add(c, symbol, pos=None):
    pos = pos or Position()
    c._store.register_symbol(c._date, symbol, symbol, P, pos)
    c._entries[symbol] = {"name": symbol, "params": P, "pos": pos, "price": 0}


def _held(c, n):
    for i in range(n):
        _add(c, f"H{i:05d}", HELD)


def _bought(c):
    return [s for side, s, _ in c._broker.orders if side == "매수"]


async def _fill(c, order_no, symbol, qty, price):
    await c._on_fill_values(
        {
            "9203": order_no,
            "9001": "A" + symbol,
            "913": "체결",
            "911": str(qty),
            "910": str(price),
            "902": "0",
        }
    )


# ── 자리 계산 ────────────────────────────────────────────────


def test_체결_대기_중인_1차_매수도_자리를_차지한다():
    assert occupies_slot(Position(state=State.WAITING, pending=True))
    assert not occupies_slot(Position(state=State.WAITING))
    assert not occupies_slot(Position(state=State.CLOSED))
    for state in (State.BUY1, State.BUY1_TP1, State.BUY2, State.BUY2_TP2):
        assert occupies_slot(
            Position(state=state, avg_price=1, remaining=1, total_bought=1)
        )


def test_사고_재현_한_자리에_대기_4종목이_몰려도_1종목만_들어간다(core):
    """체결 통보가 오기 전에 4종목의 신호가 연달아 들어온 상황 그대로."""
    core._max_symbols = 7
    _held(core, 6)  # 한 자리 비었다
    waiting = ["019170", "125490", "012210", "066970"]
    for s in waiting:
        _add(core, s)

    async def burst():
        for s in waiting:  # 체결 통보 없이 연달아
            await core._on_tick(Tick(s, 9_950, ""))

    asyncio.run(burst())

    assert _bought(core) == ["019170"]
    assert len(core.slot_holders()) == 7
    for s in waiting[1:]:
        assert core._entries[s]["pos"].state is State.WAITING  # 보류로 남는다
        assert not core._entries[s]["pos"].pending


def test_동시에_처리되는_틱도_빈자리를_두_번_쓰지_못한다(core, monkeypatch):
    """예수금 조회·주문 전송이 비동기라, 그 사이 다른 틱이 끼어들 수 있다
    (재연결 가격 보정 루프와 실시간 틱이 겹치는 경우)."""
    core._max_symbols = 3
    _held(core, 2)
    for s in ("A00001", "A00002", "A00003"):
        _add(core, s)

    real = core._get_deposit

    async def slow_deposit():
        await asyncio.sleep(0.01)  # 이 사이에 다른 종목 판정이 끼어든다
        return await real()

    monkeypatch.setattr(core, "_get_deposit", slow_deposit)

    async def together():
        await asyncio.gather(
            *(core._on_tick(Tick(s, 9_950, "")) for s in ("A00001", "A00002", "A00003"))
        )

    asyncio.run(together())

    assert len(_bought(core)) == 1


def test_주문이_실패하면_예약한_자리를_돌려준다(core):
    core._max_symbols = 2
    _held(core, 1)
    _add(core, "B00001")
    _add(core, "B00002")
    core._broker.fail_buy.add("B00001")

    async def run():
        await core._on_tick(Tick("B00001", 9_950, ""))  # 거부됨
        await core._on_tick(Tick("B00002", 9_950, ""))

    asyncio.run(run())

    assert _bought(core) == ["B00002"]  # 실패한 종목이 자리를 붙잡지 않는다
    assert not core._entering and not core._in_flight


def test_예수금_부족으로_걸러져도_자리를_돌려준다(core):
    core._max_symbols = 2
    _held(core, 1)
    _add(core, "C00001")
    core._broker.deposit_value = 0

    asyncio.run(core._on_tick(Tick("C00001", 9_950, "")))

    assert _bought(core) == []
    assert core.slot_holders() == {"H00000"}
    assert not core._entering and not core._in_flight


def test_체결되면_자리는_보유_상태로_넘어가고_다음_종목은_막힌다(core):
    core._max_symbols = 2
    _held(core, 1)
    _add(core, "D00001")
    _add(core, "D00002")

    async def run():
        await core._on_tick(Tick("D00001", 9_950, ""))
        await _fill(core, "ORD1", "D00001", 100, 9_950)
        await core._on_tick(Tick("D00002", 9_950, ""))

    asyncio.run(run())

    assert core._entries["D00001"]["pos"].state is State.BUY1
    assert _bought(core) == ["D00001"]


def test_청산으로_자리가_나면_다시_한_종목만_들어간다(core):
    core._max_symbols = 2
    _add(core, "E00001", HELD)
    _add(core, "E00002", HELD)
    for s in ("E00003", "E00004", "E00005"):
        _add(core, s)

    async def run():
        await core._on_tick(Tick("E00001", 7_900, ""))  # 3선 아래 → 전량 손절
        await _fill(core, "ORD1", "E00001", 100, 7_900)
        for s in ("E00003", "E00004", "E00005"):
            await core._on_tick(Tick(s, 9_950, ""))

    asyncio.run(run())

    assert _bought(core) == ["E00003"]
    assert len(core.slot_holders()) == 2


def test_같은_종목_주문이_접수_중이면_겹쳐_내지_않는다(core, monkeypatch):
    """수동 청산과 손절 판정이 동시에 들어와도 매도는 한 번만 나간다."""
    _add(core, "F00001", HELD)
    real = core._broker.sell

    def slow_sell(symbol, qty):
        import time

        time.sleep(0.02)
        return real(symbol, qty)

    monkeypatch.setattr(core._broker, "sell", slow_sell)

    async def together():
        await asyncio.gather(
            core._on_tick(Tick("F00001", 7_900, "")),
            core._on_tick(Tick("F00001", 7_900, "")),
        )

    asyncio.run(together())

    assert [o for o in core._broker.orders if o[0] == "매도"] == [
        ("매도", "F00001", 100)
    ]


def test_체결_대기_중인_매수_금액은_예수금에서_빼고_본다(core):
    """동시에 나간 매수들이 같은 잔고를 중복으로 쓰지 않게 한다."""
    core._max_symbols = 10
    core._broker.deposit_value = 1_500_000  # 1종목(약 100만원)분만 있다
    _add(core, "G00001")
    _add(core, "G00002")

    async def run():
        await core._on_tick(Tick("G00001", 9_950, ""))
        await core._on_tick(Tick("G00002", 9_950, ""))  # 캐시는 아직 150만원

    asyncio.run(run())

    assert _bought(core) == ["G00001"]


def test_초과가_생기면_에러로_크게_알린다(core):
    """관문을 거치지 않는 새 경로가 생겨도 조용히 넘어가지 않는다 (마지막 감시선)."""
    core._max_symbols = 1
    _add(core, "J00001")

    async def run():
        await core._on_tick(Tick("J00001", 9_950, ""))
        _held(core, 1)  # 관문 밖에서 보유가 늘어난 상황을 만든다 (예: 수동 편집)
        await _fill(core, "ORD1", "J00001", 100, 9_950)

    asyncio.run(run())

    logs = core._store.day_log(core._date) if hasattr(core._store, "day_log") else []
    text = " ".join(str(r) for r in logs)
    assert "최대 종목 수 초과 감지" in text
