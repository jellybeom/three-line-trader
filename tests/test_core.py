"""core 단위 테스트 — 스텁 broker 로 '판단 → 주문 → 체결통보 → 확정' 흐름과
예수금 방어 정책을 네트워크 없이 검증한다.
"""

import asyncio

import pytest

from trader.core import Core
from trader.state_machine import Params, Position, State
from trader.store import Store
from trader.ui import bus

P = Params(
    line1=10_000, line2=9_000, line3=8_000, buy1_amount=1_000_000, buy2_amount=900_000
)


class StubBroker:
    """주문을 기록만 하고 주문번호를 돌려주는 가짜 broker."""

    def __init__(self, deposit: float = 100_000_000):
        self.deposit_value = deposit
        self.orders: list[tuple[str, str, int]] = []  # (side, symbol, qty)
        self._seq = 0

    def buy(self, symbol, qty):
        self._seq += 1
        self.orders.append(("매수", symbol, qty))
        return f"ORD{self._seq}"

    def sell(self, symbol, qty):
        self._seq += 1
        self.orders.append(("매도", symbol, qty))
        return f"ORD{self._seq}"

    def deposit(self):
        return self.deposit_value


@pytest.fixture
def core(tmp_path):
    """연결된 상태의 코어 (스텁 broker 주입, 감시 중)."""
    c = Core(bus.Bus())
    c._store = Store(tmp_path / "t.db")
    c._date = "2026-07-20"
    c._broker = StubBroker()
    c._running = True
    yield c
    c._store.close()


def register(c: Core, pos: Position = Position()) -> None:
    c._store.register_symbol(c._date, "005930", "삼성전자", P, pos)
    c._entries["005930"] = {"name": "삼성전자", "params": P, "pos": pos, "price": 0}


async def tick(c: Core, price: float) -> None:
    from trader.watcher import Tick

    await c._on_tick(Tick("005930", price, ""))


async def fill(c: Core, order_no: str, qty: int, price: float) -> None:
    await c._on_fill_values(
        {
            "9203": order_no,
            "9001": "A005930",
            "913": "체결",
            "911": str(qty),
            "910": str(price),
            "902": "0",
        }
    )


# ── 정상 흐름: 주문 → pending → 체결 확정 ──────────────────────


def test_매수_판단시_주문이_나가고_체결_전까지_pending(core):
    register(core)
    asyncio.run(tick(core, 9_950))
    assert core._broker.orders == [("매수", "005930", 100)]
    pos = core._entries["005930"]["pos"]
    assert pos.pending is True and pos.state is State.WAITING  # 아직 전이 전
    assert asyncio.run(tick_returns_none(core)) is None  # pending 중 추가 판단 없음


async def tick_returns_none(core):
    from trader.state_machine import decide

    e = core._entries["005930"]
    return decide(e["pos"], e["params"], 8_000)  # 3선 이탈 가격조차 무시되어야 함


def test_체결통보_수신시_상태_확정과_기록(core):
    register(core)
    asyncio.run(tick(core, 9_950))
    asyncio.run(fill(core, "ORD1", 100, 9_960))  # 슬리피지: 지시 9,950 → 체결 9,960
    pos = core._entries["005930"]["pos"]
    assert pos.state is State.BUY1 and pos.pending is False
    assert pos.avg_price == 9_960  # 평단은 실제 체결가 기준
    _, _, restored, _ = core._store.load_all(core._date)["005930"]
    assert restored == pos  # DB 에도 확정 상태 저장


def test_다른_주문번호의_체결통보는_무시(core):
    register(core)
    asyncio.run(tick(core, 9_950))
    asyncio.run(fill(core, "UNKNOWN", 100, 9_960))  # 수동 주문 등
    assert core._entries["005930"]["pos"].pending is True  # 여전히 대기


def test_전체_사이클_익절까지(core):
    register(core)
    for i, price in enumerate([10_000, 10_300, 10_500, 10_700], start=1):
        asyncio.run(tick(core, price))
        asyncio.run(fill(core, f"ORD{i}", core._broker.orders[-1][2], price))
    pos = core._entries["005930"]["pos"]
    assert pos.state is State.CLOSED and pos.remaining == 0
    assert pos.realized_pnl == 44_000
    sides = [o[0] for o in core._broker.orders]
    assert sides == ["매수", "매도", "매도", "매도"]


# ── 예수금 방어 ────────────────────────────────────────────────


def test_1차_매수_시점_예수금_부족이면_주문없이_종료(core):
    core._broker.deposit_value = 100  # 부족 상황
    register(core)
    asyncio.run(tick(core, 9_950))
    assert core._broker.orders == []  # 주문이 나가지 않음
    assert core._entries["005930"]["pos"].state is State.CLOSED


def test_2차_매수_시점_부족이면_1차물량_유지하고_차단(core):
    register(core)
    asyncio.run(tick(core, 10_000))
    asyncio.run(fill(core, "ORD1", 100, 10_000))  # 1차 매수 완료
    core._broker.deposit_value = 100  # 이후 부족
    asyncio.run(tick(core, 9_000))  # 2차 매수 조건
    pos = core._entries["005930"]["pos"]
    assert pos.state is State.BUY1 and pos.remaining == 100  # 1차 물량 유지
    assert "005930" in core._buy2_blocked
    asyncio.run(tick(core, 8_900))  # 재시도에도 추가 주문 없음 (deposit 재호출도 차단)
    assert [o[0] for o in core._broker.orders] == ["매수"]


def test_차단_상태에서도_손절은_동작(core):
    register(core)
    asyncio.run(tick(core, 10_000))
    asyncio.run(fill(core, "ORD1", 100, 10_000))
    core._broker.deposit_value = 100
    asyncio.run(tick(core, 9_000))  # 2차 차단
    asyncio.run(tick(core, 7_900))  # 3선 갭 이탈 → 매도는 예수금 무관하게 동작
    assert core._broker.orders[-1] == ("매도", "005930", 100)


# ── 최대 종목 수 제한 ──────────────────────────────────────────


def test_최대_종목_수_도달시_추가_진입은_주문없이_종료(core):
    core._max_symbols = 1
    register(core)  # 005930
    core._store.register_symbol(core._date, "000660", "하이닉스", P)
    core._entries["000660"] = {
        "name": "하이닉스",
        "params": P,
        "pos": Position(),
        "price": 0,
    }
    asyncio.run(tick(core, 10_000))
    asyncio.run(fill(core, "ORD1", 100, 10_000))  # 1슬롯 점유
    from trader.watcher import Tick as T

    asyncio.run(core._on_tick(T("000660", 9_950, "")))  # 2번째 진입 시도
    assert core._entries["000660"]["pos"].state is State.CLOSED  # 주문 없이 종료
    assert core._broker.orders == [("매수", "005930", 100)]  # 추가 주문 없음


def test_슬롯이_비면_다시_진입_가능(core):
    core._max_symbols = 1
    register(core)
    core._store.register_symbol(core._date, "000660", "하이닉스", P)
    core._entries["000660"] = {
        "name": "하이닉스",
        "params": P,
        "pos": Position(),
        "price": 0,
    }
    for i, price in enumerate(
        [10_000, 7_900], start=1
    ):  # 매수 → 3선 갭 손절로 슬롯 반환
        asyncio.run(tick(core, price))
        asyncio.run(fill(core, f"ORD{i}", 100, price))
    from trader.watcher import Tick as T

    asyncio.run(core._on_tick(T("000660", 9_950, "")))  # 이제 진입 가능
    assert core._broker.orders[-1] == ("매수", "000660", 100)


# ── 감시 중지 / 재연결 보정 ────────────────────────────────────


def test_감시_중지_상태에서는_시세만_표시하고_판단_없음(core):
    register(core)
    core._running = False
    asyncio.run(tick(core, 9_950))
    assert core._broker.orders == []
    assert core._entries["005930"]["pos"].state is State.WAITING
