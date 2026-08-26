"""core 단위 테스트 — 스텁 broker 로 '판단 → 주문 → 체결통보 → 확정' 흐름과
예수금 방어 정책을 네트워크 없이 검증한다.
"""

import asyncio
import time
from datetime import datetime

import pytest

from trader.broker import BrokerError
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

    def holdings(self):
        return getattr(self, "holdings_result", {})

    def holdings_detail(self):
        """{종목: (보유수량, 매도가능수량)} — 기본은 둘이 같다고 본다."""
        detail = getattr(self, "holdings_detail_result", None)
        if detail is not None:
            return detail
        return {s: (q, q) for s, q in self.holdings().items()}


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
    _, _, restored, _, *_ = core._store.load_all(core._date)["005930"]
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


def test_1차_매수_시점_예수금_부족이면_대기로_남는다(core):
    """당일 종료시키면 자금이 회수돼도 되살아나지 못한다 — 대기로 두고 재시도한다."""
    core._broker.deposit_value = 100  # 부족 상황
    register(core)
    asyncio.run(tick(core, 9_950))
    assert core._broker.orders == []  # 주문이 나가지 않음
    assert core._entries["005930"]["pos"].state is State.WAITING


def test_자금이_생기면_보류된_종목이_재진입한다(core):
    core._broker.deposit_value = 100
    register(core)
    asyncio.run(tick(core, 9_950))
    assert core._broker.orders == []

    core._broker.deposit_value = 10**9  # 매도 등으로 자금 회수
    core._invalidate_deposit()
    asyncio.run(tick(core, 9_940))
    assert core._broker.orders == [("매수", "005930", 100)]
    assert core._entries["005930"]["pos"].pending is True


def test_보류_중_3선_아래로_가면_그때_종료된다(core):
    core._broker.deposit_value = 100
    register(core)
    asyncio.run(tick(core, 9_950))  # 1선 이탈이지만 자금 부족 → 보류
    asyncio.run(tick(core, 7_900))  # 3선 아래 → 진입 금지 확정
    assert core._entries["005930"]["pos"].state is State.CLOSED
    assert core._broker.orders == []


def test_2차_매수_시점_부족이면_1차물량_유지하고_보류(core):
    register(core)
    asyncio.run(tick(core, 10_000))
    asyncio.run(fill(core, "ORD1", 100, 10_000))  # 1차 매수 완료
    core._broker.deposit_value = 100  # 이후 부족
    asyncio.run(tick(core, 9_000))  # 2차 매수 조건
    pos = core._entries["005930"]["pos"]
    assert pos.state is State.BUY1 and pos.remaining == 100  # 1차 물량 유지
    assert core._broker.orders == [("매수", "005930", 100)]  # 2차 주문은 나가지 않음
    asyncio.run(tick(core, 8_900))  # 재시도에도 추가 주문 없음 (deposit 재호출도 차단)
    assert [o[0] for o in core._broker.orders] == ["매수"]


def test_보류_상태에서도_손절은_동작(core):
    register(core)
    asyncio.run(tick(core, 10_000))
    asyncio.run(fill(core, "ORD1", 100, 10_000))
    core._broker.deposit_value = 100
    asyncio.run(tick(core, 9_000))  # 2차 보류
    asyncio.run(tick(core, 7_900))  # 3선 갭 이탈 → 매도는 예수금 무관하게 동작
    assert core._broker.orders[-1] == ("매도", "005930", 100)


def test_2차_보류는_자금이_생기면_풀린다(core):
    register(core)
    asyncio.run(tick(core, 10_000))
    asyncio.run(fill(core, "ORD1", 100, 10_000))
    core._broker.deposit_value = 100
    asyncio.run(tick(core, 9_000))  # 2선 이탈이지만 자금 부족 → 보류
    assert len(core._broker.orders) == 1

    core._broker.deposit_value = 10**9
    core._invalidate_deposit()
    asyncio.run(tick(core, 8_990))
    assert core._broker.orders[-1][0] == "매수"


# ── 최대 종목 수 제한 ──────────────────────────────────────────


def test_최대_종목_수_도달시_추가_진입은_대기로_보류된다(core):
    """자리가 나면 들어갈 수 있어야 하므로 종료시키지 않는다 (2026-07-28 실측 개선)."""
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
    assert core._entries["000660"]["pos"].state is State.WAITING  # 보류 (종료 아님)
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


# ── 주문 실패 쿨다운 / 체결 대기 복구 ──────────────────────────


class RejectingBroker(StubBroker):
    """모든 주문을 거부하는 broker — 실패 반복 방지 검증용."""

    def buy(self, symbol, qty):
        self.orders.append(("매수", symbol, qty))
        raise BrokerError("주문가능금액 부족")

    def sell(self, symbol, qty):
        self.orders.append(("매도", symbol, qty))
        raise BrokerError("거부")


def test_주문_실패해도_틱마다_재주문하지_않는다(core):
    core._broker = RejectingBroker()
    register(core)
    for _ in range(50):  # 같은 조건의 틱이 연속으로 들어와도
        asyncio.run(tick(core, 9_950))
    assert len(core._broker.orders) == 1  # 주문 시도는 1회뿐


def test_연속_실패시_당일_해당_종목_주문_차단(core):
    core._broker = RejectingBroker()
    register(core)
    for _ in range(3):
        asyncio.run(tick(core, 9_950))
        core._order_fail["005930"]["until"] = 0  # 쿨다운만 만료 (실패 누적은 유지)
    assert "005930" in core._order_blocked
    asyncio.run(tick(core, 9_950))
    assert len(core._broker.orders) == 3  # 차단 후에는 시도조차 하지 않음


def test_체결통보_미도착시_계좌_잔고로_대기_해제(core, monkeypatch):
    """pending 이 풀리지 않으면 손절·익절이 하루 종일 멈춘다 — 계좌를 근거로 복구한다."""
    import trader.core as core_mod

    register(core)
    asyncio.run(tick(core, 9_950))
    assert core._entries["005930"]["pos"].pending is True
    asyncio.run(tick(core, 7_000))  # 손절선인데도 pending 이라 판정이 멈춘 상태
    assert core._entries["005930"]["pos"].state is State.WAITING

    core._broker.holdings_result = {"005930": 60}  # 실제로는 60주만 체결
    monkeypatch.setattr(core_mod, "_PENDING_RECOVER_SEC", 0)
    asyncio.run(core._check_pending())

    pos = core._entries["005930"]["pos"]
    assert pos.pending is False and pos.remaining == 60 and pos.state is State.BUY1


def test_잔고가_비어있으면_종료로_복구(core, monkeypatch):
    import trader.core as core_mod

    register(core)
    asyncio.run(tick(core, 9_950))
    core._broker.holdings_result = {}
    monkeypatch.setattr(core_mod, "_PENDING_RECOVER_SEC", 0)
    asyncio.run(core._check_pending())
    assert core._entries["005930"]["pos"].state is State.CLOSED


# ── 거래비용 / MFE·MAE ─────────────────────────────────────────


def test_체결마다_거래비용이_누적된다(core):
    core._commission_rate, core._tax_rate = 0.001, 0.002
    register(core)
    asyncio.run(tick(core, 10_000))
    asyncio.run(fill(core, "ORD1", 100, 10_000))  # 매수 100만 × 0.1% = 1,000
    assert core._entries["005930"]["pos"].fees == 1_000
    asyncio.run(tick(core, 10_300))
    asyncio.run(fill(core, "ORD2", 40, 10_300))  # 매도 41.2만 × 0.3% = 1,236
    assert core._entries["005930"]["pos"].fees == 1_000 + 1_236


def test_보유_중_최고최저가가_기록된다(core):
    register(core)
    asyncio.run(tick(core, 10_000))
    asyncio.run(fill(core, "ORD1", 100, 10_000))
    for price in (10_200, 9_700, 10_100):  # 보유 중 등락
        asyncio.run(tick(core, price))
    asyncio.run(tick(core, 10_300))
    asyncio.run(fill(core, "ORD2", 40, 10_300))
    pos = core._entries["005930"]["pos"]
    assert pos.high_price == 10_300 and pos.low_price == 9_700


def test_진입_전_가격은_최저가에_반영되지_않는다(core):
    register(core)
    asyncio.run(tick(core, 12_000))  # 아직 대기 — 추적 대상 아님
    asyncio.run(tick(core, 10_000))
    asyncio.run(fill(core, "ORD1", 100, 10_000))
    asyncio.run(tick(core, 10_500))
    asyncio.run(tick(core, 10_600))
    asyncio.run(fill(core, "ORD2", 40, 10_600))
    pos = core._entries["005930"]["pos"]
    assert pos.low_price == 10_000  # 12,000 은 기록되지 않음


# ── 최소 진입 수량 경고 ────────────────────────────────────────


def _logs(core):
    from trader.ui import bus as b

    out = []
    while not core._bus.events.empty():
        e = core._bus.events.get_nowait()
        if isinstance(e, b.LogLine):
            out.append(e.text)
    return out


def test_소량_진입_종목은_등록_시_경고한다(core):
    """1차 수량이 3주 미만이면 40/50/10 분할 익절이 사실상 불가능하다."""
    from trader.state_machine import Params as P2

    high = P2(
        line1=180_000,
        line2=170_000,
        line3=160_000,
        buy1_amount=250_000,
        buy2_amount=250_000,
    )  # 1주밖에 못 산다
    core._running = False  # 등록은 감시 중지 상태에서만 가능
    asyncio.run(
        core._handle_command(bus.Register("010120", "LS ELECTRIC", high, Position()))
    )
    assert any("단계 익절이 어렵습니다" in t for t in _logs(core))


def test_충분한_수량이면_경고하지_않는다(core):
    core._running = False
    asyncio.run(core._handle_command(bus.Register("005930", "삼성전자", P, Position())))
    assert not any("단계 익절" in t for t in _logs(core))


# ── 알림 묶음 발송 ─────────────────────────────────────────────
def test_체결_알림은_묶지_않고_즉시_나간다(core):
    sent = []

    class FakeBot:
        async def send_text(self, text):
            sent.append(text)
            return True

        async def send_embed(self, embed):
            sent.append(embed.get("title", ""))
            return True

    core._bot = FakeBot()
    core._notify_level = "전체"
    register(core)
    asyncio.run(tick(core, 10_000))
    asyncio.run(fill(core, "ORD1", 100, 10_000))
    asyncio.run(asyncio.sleep(0.05))
    assert any("매수" in t for t in sent)
    assert core._notice_batch == []


# ── 보류 로그 억제 / 주문 수량 여유 ────────────────────────────


def test_보류_로그는_구간이_바뀔_때만_남는다(core):
    """틱마다 남기면 하루 수백 건이 된다 (2026-07-29 실측 107건)."""
    from trader.ui import bus as b

    core._max_symbols = 0  # 무조건 보류
    register(core)
    for price in (9_950, 9_940, 9_930, 9_920):  # 모두 1선~2선 구간
        asyncio.run(tick(core, price))
    logs = []
    blocked = []
    while not core._bus.events.empty():
        e = core._bus.events.get_nowait()
        if isinstance(e, b.LogLine) and e.kind == "보류":
            logs.append(e.text)
        elif isinstance(e, b.Blocked):
            blocked.append(e)
    assert len(logs) == 1  # 같은 구간이 이어지면 한 번만
    assert len(blocked) == 4  # 화면 표시는 매번 갱신


def test_구간이_바뀌면_다시_기록한다(core):
    from trader.ui import bus as b

    core._max_symbols = 0
    register(core)
    asyncio.run(tick(core, 9_950))  # 1선~2선
    asyncio.run(tick(core, 8_500))  # 2선~3선 — 상황이 달라졌으니 다시 남긴다
    logs = [e.text for e in _drain(core._bus) if getattr(e, "kind", "") == "보류"]
    assert len(logs) == 2


def _drain(b):
    out = []
    while not b.events.empty():
        out.append(b.events.get_nowait())
    return out


def test_예수금이_빠듯하면_주문하지_않고_보류한다(core):
    """직전 체결이 증권사 여력에 반영되기 전이면 꽉 채운 주문은 거부된다."""
    register(core)
    need = 100 * 10_000  # 1차 매수 100주 × 10,000
    core._broker.deposit_value = need * 1.01  # 여유 2% 에 못 미침
    asyncio.run(tick(core, 10_000))
    assert core._broker.orders == []
    assert core._entries["005930"]["pos"].state is State.WAITING

    core._broker.deposit_value = need * 1.05
    core._invalidate_deposit()
    asyncio.run(tick(core, 9_990))
    assert core._broker.orders != []


# ── 매도 체결통보 유실 복구 (2026-07-30 실측) ──────────────────


def test_매도_복구는_매도가능수량을_기준으로_한다(core, monkeypatch):
    """보유수량은 매도 직후에도 남아 보인다 — 그걸 믿으면 같은 물량을 또 팔려 한다."""
    import trader.core as core_mod

    register(
        core,
        Position(state=State.BUY1_TP1, avg_price=10_000, total_bought=10, remaining=10),
    )
    asyncio.run(tick(core, 9_900))  # 본절(평단) 이탈 → 잔량 전량 청산
    assert core._entries["005930"]["pos"].pending is True

    # 계좌: 보유 10주로 보이지만 매도가능은 0주 (이미 팔렸거나 주문이 걸려 있음)
    core._broker.holdings_detail_result = {"005930": (10, 0)}
    monkeypatch.setattr(core_mod, "_PENDING_RECOVER_SEC", 0)
    asyncio.run(core._check_pending())

    pos = core._entries["005930"]["pos"]
    assert pos.state is State.CLOSED and pos.remaining == 0

    before = len(core._broker.orders)
    asyncio.run(tick(core, 9_000))  # 더 떨어져도 재매도 시도가 없어야 한다
    assert len(core._broker.orders) == before


def test_매도가능수량_부족_오류는_즉시_주문을_중단한다(core):
    """재시도해도 성공하지 않는 오류 — 30초 쿨다운으로 3번 반복할 이유가 없다."""
    from trader.broker import BrokerError

    class Rejecting(StubBroker):
        def sell(self, symbol, qty):
            raise BrokerError(
                "kt10001 실패: (800033:매도가능수량이 부족합니다. 0주 매도가능)"
            )

    core._broker = Rejecting()
    register(
        core,
        Position(state=State.BUY1_TP1, avg_price=10_000, total_bought=10, remaining=10),
    )
    asyncio.run(tick(core, 9_900))
    assert "005930" in core._order_blocked  # 1회로 즉시 차단


def test_삭제_로그는_한_줄만_남는다(core):
    """감사 행과 화면 로그가 각각 남아 복원 시 두 줄로 보이던 문제."""
    register(core)
    core._running = False
    asyncio.run(core._handle_command(bus.Delete("005930")))
    rows = core._store.recent_events(core._date)
    assert len([r for r in rows if r[2] == "삭제"]) == 1


def test_늦게_도착한_체결통보는_보정을_요청한다(core, monkeypatch):
    """살아 있는 주문을 죽은 것으로 오판해 정리한 뒤 체결통보가 오는 경우."""
    import trader.core as core_mod
    from trader.ui import bus as b

    register(
        core,
        Position(state=State.BUY1_TP1, avg_price=10_000, total_bought=10, remaining=10),
    )
    asyncio.run(tick(core, 9_900))  # 본절 이탈 → 전량 청산 주문
    order_no = f"ORD{core._broker._seq}"

    core._broker.holdings_detail_result = {"005930": (10, 0)}
    monkeypatch.setattr(core_mod, "_PENDING_RECOVER_SEC", 0)
    asyncio.run(core._check_pending())  # 강제 정리
    assert order_no in core._recovered

    asyncio.run(fill(core, order_no, 10, 9_880))  # 뒤늦게 체결통보 도착
    texts = [e.text for e in _drain(core._bus) if isinstance(e, b.LogLine)]
    assert any("뒤늦게 도착" in t and "보정" in t for t in texts)
    assert order_no not in core._recovered


def test_체결시_판정가가_함께_기록된다(core):
    """매매 동작에는 영향을 주지 않고 기록만 추가된다."""
    register(core)
    asyncio.run(tick(core, 9_950))  # 1선 이탈 → 판정가 9,950
    asyncio.run(fill(core, "ORD1", 100, 9_980))  # 실제 체결은 9,980

    _, fills = core._store.daily_report(core._date)
    assert fills[0]["trigger_price"] == 9_950
    assert fills[0]["price"] == 9_980

    row = core._store.slippage_report()[0]
    assert row["cost_rate"] < 0  # 판정보다 비싸게 매수 → 손해로 집계
    assert (
        core._entries["005930"]["pos"].avg_price == 9_980
    )  # 평단은 실제 체결가 그대로


def test_진입_전에도_당일_최저가가_기록된다(core):
    """1선에 못 미친 종목의 근접도를 알아야 설정 조정 근거가 생긴다."""
    register(core)
    for price in (10_500, 10_200, 10_800):  # 1선(10,000)에 못 미치는 흐름
        asyncio.run(tick(core, price))
    assert core._entries["005930"]["day_low"] == 10_200
    assert core._entries["005930"]["pos"].state is State.WAITING  # 진입은 없다

    core._flush_day_lows(force=True)
    rows, _ = core._store.daily_report(core._date)
    assert rows[0]["day_low"] == 10_200
    assert rows[0]["line1"] == 10_000


def test_최저가_기록은_판정에_영향을_주지_않는다(core):
    register(core)
    asyncio.run(tick(core, 10_500))
    asyncio.run(tick(core, 9_950))  # 1선 이탈 → 정상 진입
    assert core._broker.orders == [("매수", "005930", 100)]


def test_감시_중_시가와_종가가_기록된다(core):
    """관심종목 평균 등락률(벤치마크)의 재료 — 진입하지 않은 종목도 필요하다."""
    register(core)
    for price in (10_500, 10_200, 10_800):
        asyncio.run(tick(core, price))
    e = core._entries["005930"]
    assert e["day_open"] == 10_500 and e["day_close"] == 10_800

    core._flush_day_lows(force=True)
    rows, _ = core._store.daily_report(core._date)
    assert rows[0]["day_open"] == 10_500 and rows[0]["day_close"] == 10_800


def test_편집시_태그를_비워도_기존_값이_유지된다(core):
    """태그는 기준봉 시점의 판단이라, 3선만 손볼 때 지워지면 안 된다."""
    core._running = False
    asyncio.run(
        core._handle_command(
            bus.Register(
                "005930",
                "삼성전자",
                P,
                Position(),
                tags="테마주",
                base_date="2026-08-05",
            )
        )
    )
    asyncio.run(
        core._handle_command(bus.Register("005930", "삼성전자", P, None, edit=True))
    )  # 태그 없이 편집
    _, _, _, _, tags, base_date = core._store.load_all(core._date)["005930"]
    assert tags == "테마주" and base_date == "2026-08-05"


# ── 태그 전달 / 삭제 알림 / 등록 알림 (2026-08-08) ─────────────


def test_태그와_기준봉이_UI_로_전달된다(core):
    """저장은 되는데 편집 창이 비어 보이던 버그 — PositionUpdate 에 실려야 한다."""
    from trader.ui import bus as b

    core._running = False
    asyncio.run(
        core._handle_command(
            bus.Register(
                "005930",
                "삼성전자",
                P,
                Position(),
                memo="메모",
                tags="테마주,상한가",
                base_date="2026-08-05",
            )
        )
    )
    updates = [e for e in _drain(core._bus) if isinstance(e, b.PositionUpdate)]
    assert updates[-1].tags == "테마주,상한가"
    assert updates[-1].base_date == "2026-08-05"
    assert updates[-1].memo == "메모"


def test_복원_후에도_태그가_UI_로_전달된다(core):
    """재시작 경로(load_all → _emit_position)에서도 빠지지 않아야 한다."""
    from trader.ui import bus as b

    core._store.register_symbol(
        core._date,
        "005930",
        "삼성전자",
        P,
        memo="메모",
        tags="섹터주",
        base_date="2026-08-04",
    )
    core._load_date(core._date)
    core._emit_date_loaded()
    updates = [e for e in _drain(core._bus) if isinstance(e, b.PositionUpdate)]
    assert updates[-1].tags == "섹터주" and updates[-1].base_date == "2026-08-04"


def test_일괄_등록은_종목별_알림을_보내지_않는다(core):
    """CSV 는 결과 embed 로 한 번에 알린다 — 종목마다 또 보내면 중복이다."""
    sent = []

    class FakeBot:
        async def send_embed(self, embed):
            sent.append(embed.get("title", ""))
            return True

        async def send_text(self, text):
            sent.append(text)
            return True

    core._bot = FakeBot()
    core._notify_level = "전체"
    core._running = False

    asyncio.run(
        core._handle_command(
            bus.Register("005930", "삼성전자", P, Position(), quiet=True)
        )
    )
    asyncio.run(core._flush_notices())
    assert sent == [], "일괄 등록인데 종목별 알림이 나갔다"

    # 화면 로그와 DB 기록은 그대로 남는다
    assert "005930" in core._entries
    assert [r for r in core._store.recent_events(core._date) if r[2] == "등록"]


def test_정상_등록은_Discord_로_알리지_않는다(core):
    """편성 결과는 08:55 개장 브리핑으로 갈음한다."""
    bot = _RecordingBot()
    core._bot = bot
    core._notify_level = "전체"
    rows = (
        {
            "symbol": "005930",
            "name": "삼성전자",
            "tags": "테마주",
            "base_date": "2026-08-05",
            "memo": "",
            "qty": 10,
        },
    )

    asyncio.run(core._handle_command(bus.RegistrationNotice(rows, (), 2, 1)))
    asyncio.run(core._flush_notices())
    assert bot.sent == []

    # 화면 로그·DB 에는 결과가 남는다
    assert [r for r in core._store.recent_events(core._date) if "CSV 불러오기" in r[3]]


def test_등록_실패가_있으면_즉시_알린다(core):
    """사용자가 고쳐야 하는 문제라 브리핑까지 기다리면 늦다."""
    bot = _RecordingBot()
    core._bot = bot
    core._notify_level = "전체"
    rows = (
        {
            "symbol": "005930",
            "name": "삼성전자",
            "tags": "",
            "base_date": "",
            "memo": "",
            "qty": 10,
        },
    )

    asyncio.run(
        core._handle_command(
            bus.RegistrationNotice(
                rows, ("해성디에스(195870) 1선 > 2선 > 3선 위반",), 0, 0
            )
        )
    )
    asyncio.run(core._flush_notices())
    assert len(bot.sent) == 1
    assert "해성디에스" in bot.sent[0]["fields"][0]["value"]


def test_CSV_등록_알림은_한_번만_발송된다(core):
    """예전에는 결과 embed 와 건수 알림이 따로 나가 두 번 왔다."""
    sent = []

    class FakeBot:
        async def send_embed(self, embed):
            sent.append(embed)
            return True

        async def send_text(self, text):
            sent.append(text)
            return True

    core._bot = FakeBot()
    core._notify_level = "전체"
    asyncio.run(
        core._handle_command(
            bus.RegistrationNotice(
                (
                    {
                        "symbol": "005930",
                        "name": "삼성전자",
                        "tags": "테마주",
                        "base_date": "2026-08-05",
                        "memo": "",
                        "qty": 10,
                    },
                ),
                ("실패 1건",),
                2,
                1,
            )
        )
    )
    asyncio.run(core._flush_notices())

    assert len(sent) == 1
    assert "등록 1종목" in sent[0]["title"]
    assert "3선 미입력 2종목" in sent[0]["footer"]["text"]
    assert "중복 제외 1종목" in sent[0]["footer"]["text"]


# ── 관심종목 편성은 조용히, 개장 브리핑으로 대체 (2026-08-08) ──


class _RecordingBot:
    def __init__(self):
        self.sent = []

    async def send_embed(self, embed):
        self.sent.append(embed)
        return True

    async def send_text(self, text):
        self.sent.append(text)
        return True

    def set_blocked(self, *a):
        pass


def test_등록_편집_삭제는_Discord_로_알리지_않는다(core):
    """저녁에 몰아서 하는 편성 작업이라 건건이 알리면 소음이 된다."""
    bot = _RecordingBot()
    core._bot = bot
    core._notify_level = "전체"
    core._running = False

    asyncio.run(core._handle_command(bus.Register("005930", "삼성전자", P, Position())))
    asyncio.run(
        core._handle_command(bus.Register("005930", "삼성전자", P, None, edit=True))
    )
    asyncio.run(core._handle_command(bus.Delete("005930")))
    asyncio.run(core._flush_notices())
    assert bot.sent == []

    # 화면 로그와 DB 기록은 그대로 남는다
    kinds = {r[2] for r in core._store.recent_events(core._date)}
    assert {"등록", "편집", "삭제"} <= kinds


def test_감시_시작하면_개장_브리핑이_나간다(core):
    bot = _RecordingBot()
    core._bot = bot
    core._notify_level = "전체"
    core._running = False
    core._store.set_setting("funds_total", "2025000")
    core._max_symbols = 5
    asyncio.run(
        core._handle_command(
            bus.Register(
                "005930",
                "삼성전자",
                P,
                Position(),
                tags="테마주",
                base_date="2026-08-05",
            )
        )
    )

    asyncio.run(core._handle_command(bus.SetRunning(True)))
    asyncio.run(core._flush_notices())

    briefing = [
        e for e in bot.sent if isinstance(e, dict) and "감시 시작" in e["title"]
    ]
    assert briefing, "브리핑이 없음"
    body = briefing[0]["description"]
    assert "`#테마주` 1" in body
    assert "총 2,025,000원" in body and "최대 5종목" in body


def test_관심종목_조회는_태그와_메모를_보여준다(core):
    core._running = False
    for sym, name, tags, memo in (
        ("005930", "삼성전자", "테마주", "메모A"),
        ("000660", "하이닉스", "섹터주", ""),
    ):
        asyncio.run(
            core._handle_command(
                bus.Register(
                    sym,
                    name,
                    P,
                    Position(),
                    memo=memo,
                    tags=tags,
                    base_date="2026-08-05",
                )
            )
        )

    embed = core.watchlist_embed()
    body = embed["description"]
    assert "삼성전자" in body and "하이닉스" in body
    assert "`#테마주`" in body and "📝 메모A" in body
    assert "1/1 쪽" in embed["footer"]["text"]

    filtered = core.watchlist_embed(tag="섹터주")
    assert (
        "하이닉스" in filtered["description"]
        and "삼성전자" not in filtered["description"]
    )


def test_이월해도_태그와_기준봉이_따라간다(core):
    """빠뜨리면 이월된 날 청산된 매매가 태그 집계에서 통째로 누락된다."""
    core._running = False
    asyncio.run(
        core._handle_command(
            bus.Register(
                "005930",
                "삼성전자",
                P,
                Position(
                    state=State.BUY1, avg_price=9_900, total_bought=20, remaining=20
                ),
                tags="테마주,KOSPI상승장",
                base_date="2026-08-07",
            )
        )
    )
    asyncio.run(core._handle_command(bus.CarryOver("005930")))

    # 다음 영업일 리스트에 그대로 남아야 한다
    nxt = [d for d in core._store.recent_trade_dates(5) if d != core._date][0]
    _, _, pos, _, tags, base_date = core._store.load_all(nxt)["005930"]
    assert tags == "테마주,KOSPI상승장"
    assert base_date == "2026-08-07"
    assert pos.remaining == 20  # 포지션도 함께 넘어간다


# ── 포지션만 이월 (2026-08-10) ────────────────────────────────


def test_포지션만_이월하면_대상날짜의_3선과_메모는_유지된다(core):
    """다음 매매일에 이미 잡아둔 3선은 그날 판단이므로 덮어쓰면 안 된다."""
    from trader.state_machine import Params as P2

    core._running = False
    held = Position(state=State.BUY1, avg_price=9_900, total_bought=20, remaining=20)
    asyncio.run(
        core._handle_command(
            bus.Register(
                "005930",
                "삼성전자",
                P,
                held,
                memo="오늘메모",
                tags="테마주",
                base_date="2026-08-05",
            )
        )
    )

    tomorrow = core._next_trade_date()
    next_params = P2(
        line1=11_000,
        line2=10_500,
        line3=9_800,
        buy1_amount=200_000,
        buy2_amount=200_000,
    )
    core._store.register_symbol(
        tomorrow,
        "005930",
        "삼성전자",
        next_params,
        memo="내일메모",
        tags="상한가",
        base_date="2026-08-05",
    )

    asyncio.run(core._handle_command(bus.CarryPosition("005930")))

    _, params, pos, memo, tags, _ = core._store.load_all(tomorrow)["005930"]
    assert params.line1 == 11_000 and memo == "내일메모" and tags == "상한가"  # 그대로
    assert pos.state is State.BUY1 and pos.avg_price == 9_900  # 포지션만 덮어씀
    assert pos.remaining == 20 and pos.total_bought == 20


def test_대상날짜에_없는_종목은_포지션_이월을_거부한다(core):
    """어느 3선 설정을 쓸지 알 수 없으므로 조용히 만들지 않는다."""
    core._running = False
    held = Position(state=State.BUY1, avg_price=9_900, total_bought=20, remaining=20)
    asyncio.run(core._handle_command(bus.Register("005930", "삼성전자", P, held)))

    asyncio.run(core._handle_command(bus.CarryPosition("005930")))
    assert "005930" not in core._store.load_all(core._next_trade_date())

    texts = [r[3] for r in core._store.recent_events(core._date)]
    assert any("등록되지 않은 종목" in t for t in texts)


def test_감시_중에는_포지션_이월도_막힌다(core):
    core._running = True
    register(
        core, Position(state=State.BUY1, avg_price=9_900, total_bought=20, remaining=20)
    )
    asyncio.run(core._handle_command(bus.CarryPosition("005930")))
    assert "005930" not in core._store.load_all(core._next_trade_date())


# ── 거래일 달력 (2026-08-10) ──────────────────────────────────


def test_거래일_달력으로_기준봉_경과일을_센다(core):
    """공휴일이 끼면 주말만 빼는 계산과 달라진다."""
    core._calendar.replace(["2026-08-13", "2026-08-14", "2026-08-18", "2026-08-19"])
    core._date = "2026-08-18"
    assert core.base_days("2026-08-14") == 1  # 8/17 대체휴일 제외
    assert core.base_days("2026-08-13") == 2
    assert core.base_days("") is None


def test_달력은_지수_일봉에서_받아_저장된다(core):
    """재시작 후 키움 연결 전에도 D+n 이 맞게 보이도록 저장해 둔다."""

    class Broker(StubBroker):
        def index_daily(self, code="001", count=180):
            return [
                ("20260813", 1, 1, 1, 1, 0, 0),
                ("20260814", 1, 1, 1, 1, 0, 0),
                ("20260818", 1, 1, 1, 1, 0, 0),
            ]

    core._broker = Broker()
    asyncio.run(core.refresh_calendar())
    assert core._calendar.days == ["2026-08-13", "2026-08-14", "2026-08-18"]
    assert (
        core._store.get_setting("trading_days", "")
        == "2026-08-13,2026-08-14,2026-08-18"
    )

    fresh = Core(bus.Bus(), db_dir=str(core._db_dir))
    fresh._store = core._store
    fresh._load_calendar()
    assert len(fresh._calendar) == 3  # 연결 없이도 복원된다


def test_달력_조회_실패는_매매에_영향을_주지_않는다(core):
    from trader.broker import BrokerError

    class Broker(StubBroker):
        def index_daily(self, code="001", count=180):
            raise BrokerError("지수 조회 실패")

    core._broker = Broker()
    asyncio.run(core.refresh_calendar())  # 예외가 새어 나오지 않는다
    assert len(core._calendar) == 0
    assert core.base_days("2026-08-07", "2026-08-10") == 1  # 주말 기준 근사로 동작


# ── 전역 설정 적용이 종목 선정 근거를 지우지 않는다 ─────────────


def test_전역설정_적용이_태그와_기준봉을_지우지_않는다(core, tmp_path):
    """[적용] 은 매수 금액·익절만 바꾼다 — 태그·기준봉은 왜 골랐는지의 기록이다.

    register_symbol 은 넘긴 값으로 덮어쓰므로 호출부가 빠뜨리면 DB 에서 지워진다.
    메모리에는 남아 화면이 멀쩡해 보이고, 매매일 전환·재시작 뒤에야 드러났다.
    저녁 루틴이 'CSV 불러오기 → [적용]' 이라 정상 운용하면 매번 밟는 경로다.
    """
    core._store.register_symbol(
        core._date,
        "005930",
        "삼성전자",
        P,
        Position(),
        memo="반도체",
        tags="테마주,상한가",
        base_date="2026-07-15",
    )
    core._load_date(core._date)

    core._apply_globals_to_waiting(
        500_000, 500_000, (0.03, 0.05, 0.07), (0.4, 0.5, 0.1)
    )

    _, params, _, memo, tags, base_date = core._store.load_all(core._date)["005930"]
    assert params.buy1_amount == 500_000  # 설정은 반영되고
    assert tags == "테마주,상한가"  # 선정 근거는 그대로다
    assert base_date == "2026-07-15"
    assert memo == "반도체"


def test_보유_중_종목은_전역설정_적용에서_제외된다(core):
    """진입 시점의 조건으로 끝까지 간다 — 도중에 익절 기준이 바뀌면 안 된다."""
    pos = Position(state=State.BUY1, avg_price=9_500, total_bought=10, remaining=10)
    core._store.register_symbol(core._date, "005930", "삼성전자", P, pos, tags="테마주")
    core._load_date(core._date)

    core._apply_globals_to_waiting(
        500_000, 500_000, (0.02, 0.04, 0.06), (0.4, 0.5, 0.1)
    )

    _, params, _, _, tags, _ = core._store.load_all(core._date)["005930"]
    assert params.buy1_amount == P.buy1_amount  # 손대지 않는다
    assert params.tp_rates == P.tp_rates
    assert tags == "테마주"


# ── 0주 매수 주문은 내보내지 않는다 ─────────────────────────────


def test_매수_수량이_0이면_주문하지_않고_보류한다(core):
    """금액이 체결가(+수수료)에 못 미치면 수량이 0 이 된다.

    그대로 보내면 증권사가 거부하고, 그 실패가 3회 쌓이면 당일 그 종목 주문이
    통째로 막힌다. 가격이 더 내려가면 1주가 되므로 '대기' 로 남겨 재판정한다.
    """
    params = Params(
        line1=10_000,
        line2=9_000,
        line3=8_000,
        buy1_amount=10_000,  # 1선과 같은 금액 — 수수료만큼 모자란다
        buy2_amount=9_000,
    )
    core._commission_rate = 0.00015
    core._store.register_symbol(core._date, "005930", "삼성전자", params)
    core._load_date(core._date)

    asyncio.run(tick(core, 10_000))  # 1선 정확히 터치

    assert core._broker.orders == []  # 주문이 나가지 않았다
    assert core._entries["005930"]["pos"].state is State.WAITING  # 되살아날 수 있다
    assert "005930" not in core._order_blocked

    asyncio.run(tick(core, 9_500))  # 가격이 내려가면 그때 1주가 된다
    assert core._broker.orders == [("매수", "005930", 1)]


# ── 보유기간 ────────────────────────────────────────────────────


def test_보유기간_시계는_첫_매수에서_시작해_2차매수로_리셋되지_않는다(core):
    """물타기는 새 진입이 아니다 — '언제부터 들고 있나' 의 답은 첫 매수다."""
    register(core)

    asyncio.run(tick(core, 9_950))  # 1차 매수
    asyncio.run(fill(core, "ORD1", 100, 9_950))
    entry = core._entries["005930"]["entry_ts"]
    assert entry  # 첫 체결 시각이 기록된다

    asyncio.run(tick(core, 8_950))  # 2차 매수 (물타기)
    asyncio.run(fill(core, "ORD2", 100, 8_950))
    assert core._entries["005930"]["entry_ts"] == entry  # 되돌아가지 않는다
    assert core._entries["005930"]["exit_ts"] == ""  # 아직 보유 중 → 시계가 돈다


def test_종료되면_보유기간이_고정되고_리셋하면_초기화된다(core):
    register(core)
    asyncio.run(tick(core, 9_950))
    asyncio.run(fill(core, "ORD1", 100, 9_950))
    asyncio.run(tick(core, 7_900))  # 3선 이탈 → 전량 손절
    asyncio.run(fill(core, "ORD2", 100, 7_900))

    e = core._entries["005930"]
    assert e["pos"].state is State.CLOSED
    assert e["exit_ts"]  # 청산 시각이 박혀 시계가 멈춘다
    frozen = core.holding_label("005930")

    asyncio.run(core._handle_command(bus.Reset("005930")))
    assert core._entries["005930"]["entry_ts"] == ""  # 새 사이클 → 처음으로
    assert core.holding_label("005930") == ""
    assert frozen  # 리셋 전에는 값이 있었다


def test_이월된_종목의_진입_시각은_며칠_전_매수에서_온다(core):
    """이월 종목은 그날 기록에 매수가 없다 — 사이클 전체를 봐야 답이 나온다."""
    import datetime

    core._date = datetime.date.today().isoformat()  # 체결 시각과 매매일을 맞춘다
    register(core)
    asyncio.run(tick(core, 9_950))
    asyncio.run(fill(core, "ORD1", 100, 9_950))
    entry = core._entries["005930"]["entry_ts"]

    core._running = False  # 이월은 감시 중지 후에만 허용된다
    asyncio.run(core._handle_command(bus.CarryOver("005930")))  # 다음 매매일로
    target = core._next_trade_date()
    core._load_date(target)

    assert core._entries["005930"]["entry_ts"] == entry
    assert core.holding_label("005930").endswith("일차")  # 날짜를 넘겼다


def test_진입_기록이_없는_종목은_보유기간이_빈칸이다(core):
    """등록 창에서 시작 상태를 직접 지정한 오버나이트 종목 — 추정하지 않는다."""
    pos = Position(state=State.BUY1, avg_price=9_500, total_bought=10, remaining=10)
    register(core, pos)
    core._load_date(core._date)

    assert core._entries["005930"]["entry_ts"] == ""
    assert core.holding_label("005930") == ""


# ── 실현손익 대조 (ka10077 은 종목 단위) ─────────────────────────


class _RealizedBroker(StubBroker):
    """종목별 실현손익을 돌려주는 스텁. 어떤 종목으로 불렸는지 기록한다."""

    def __init__(self, fail: set[str] | None = None):
        super().__init__()
        self.asked: list[str] = []
        self.fail = fail or set()

    def realized_pnl(self, symbol):
        self.asked.append(symbol)
        if not symbol:  # 실제 브로커와 같은 계약 — 빈 값은 호출 전에 막힌다
            raise BrokerError("realized_pnl 은 종목코드가 필요합니다")
        if symbol in self.fail:
            raise BrokerError(f"{symbol} 조회 실패")
        return [
            {
                "symbol": symbol,
                "name": symbol,
                "qty": 1,
                "buy_price": 100.0,
                "sell_price": 110.0,
                "pnl": 10.0,
                "rate": 0.1,
                "commission": 0.0,
                "tax": 0.0,
            }
        ]


def _sold(core, *symbols):
    """오늘 매도가 있었던 종목 목록을 흉내 내는 체결 기록."""
    return [
        {"symbol": s, "side": "매도", "qty": 1, "price": 110, "ts": ""} for s in symbols
    ] + [{"symbol": "999999", "side": "매수", "qty": 1, "price": 100, "ts": ""}]


def test_실현손익은_매도가_있었던_종목만_종목별로_조회한다(core):
    """ka10077 은 종목 단위 TR 이다 — 인자 없이 부르면 서버가 거절한다.

    계좌 전체를 주는 줄 알고 인자 없이 불러 2026-08-19 부터 실현손익 대조가 매일
    통째로 건너뛰어졌다. 매수만 한 종목은 실현손익이 없으니 부를 이유도 없다.
    """
    core._broker = _RealizedBroker()

    rows = asyncio.run(core._realized_by_symbol(_sold(core, "005930", "035420")))

    assert core._broker.asked == ["005930", "035420"]  # 매수만 한 종목은 빼고
    assert "" not in core._broker.asked  # 빈 종목코드로 부르지 않는다
    assert {r["symbol"] for r in rows} == {"005930", "035420"}


def test_한_종목이_실패해도_나머지_대조는_진행한다(core):
    core._broker = _RealizedBroker(fail={"035420"})

    rows = asyncio.run(core._realized_by_symbol(_sold(core, "005930", "035420")))

    assert [r["symbol"] for r in rows] == ["005930"]
    logged = core._store._conn.execute(
        "SELECT reason FROM events WHERE kind='경고'"
    ).fetchall()
    assert any("035420" in r["reason"] for r in logged)  # 어느 종목이 빠졌는지 남긴다


def test_전부_실패하면_대조를_건너뛴다(core):
    """일부만 받은 값으로 합계를 비교하면 없는 차이를 만들어 낸다."""
    core._broker = _RealizedBroker(fail={"005930", "035420"})

    assert (
        asyncio.run(core._realized_by_symbol(_sold(core, "005930", "035420"))) is None
    )


def test_매도가_없으면_조회하지_않는다(core):
    core._broker = _RealizedBroker()
    fills = [{"symbol": "005930", "side": "매수", "qty": 1, "price": 100, "ts": ""}]

    assert asyncio.run(core._realized_by_symbol(fills)) == []
    assert core._broker.asked == []


# ── 틱 정체 감시창 ──────────────────────────────────────────────


def test_종가_동시호가에는_틱_정체를_감시하지_않는다(core, monkeypatch):
    """15:20 부터는 체결 틱이 원래 오지 않는다.

    15:30 까지 열어 두면 90초마다 기계적으로 발동해 멀쩡한 세션을 끊고 재연결 가격
    보정을 반복한다 (2026-08-21 실측: 10분 동안 경고 6회 · REST 약 450건).
    """
    import trader.core as core_mod

    calls = []

    class Watcher:
        async def force_reconnect(self):
            calls.append("재연결")

    core._watcher = Watcher()
    core._last_ws_tick = time.monotonic() - 600  # 10분째 침묵

    class FakeNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 21, 15, 25, 0)  # 금요일 종가 동시호가

    monkeypatch.setattr(core_mod, "datetime", FakeNow)
    asyncio.run(core._check_tick_flow())
    assert calls == []  # 조용한 것이 정상이다


def test_접속매매_시간의_침묵은_여전히_잡는다(core, monkeypatch):
    import trader.core as core_mod

    calls = []

    class Watcher:
        async def force_reconnect(self):
            calls.append("재연결")

    core._watcher = Watcher()
    core._last_ws_tick = time.monotonic() - 600

    class FakeNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 21, 15, 19, 0)  # 15:20 직전

    monkeypatch.setattr(core_mod, "datetime", FakeNow)
    asyncio.run(core._check_tick_flow())
    assert calls == ["재연결"]


def test_대기_종목에는_예전_매매의_보유기간이_뜨지_않는다(core):
    """holding_spans 는 45일 안의 **마지막 사이클**을 집어 온다.

    며칠 전에 사고팔았던 종목을 오늘 관심종목으로 다시 등록하면 그때의 기간이 딸려와
    대기 종목에 '26분' 이 떴다(2026-08-26 실측). 지금 들고 있는 것으로 오해한다.
    """
    register(core)
    asyncio.run(tick(core, 9_950))
    asyncio.run(fill(core, "ORD1", 100, 9_950))
    asyncio.run(tick(core, 7_900))  # 3선 이탈 → 전량 손절
    asyncio.run(fill(core, "ORD2", 100, 7_900))
    assert core.holding_label("005930")  # 종료 직후에는 최종값이 남는다

    # 다음 매매일에 같은 종목을 다시 등록 (CSV 불러오기)
    later = core._next_trade_date()
    core._store.register_symbol(later, "005930", "삼성전자", P)
    core._load_date(later)

    assert core._entries["005930"]["pos"].state is State.WAITING
    assert core._entries["005930"]["entry_ts"] == ""
    assert core.holding_label("005930") == ""


def test_리셋한_종목도_보유기간이_사라진다(core):
    """종료 → 대기 리셋은 새 사이클이다."""
    register(core)
    asyncio.run(tick(core, 9_950))
    asyncio.run(fill(core, "ORD1", 100, 9_950))
    asyncio.run(tick(core, 7_900))
    asyncio.run(fill(core, "ORD2", 100, 7_900))

    asyncio.run(core._handle_command(bus.Reset("005930")))
    core._load_date(core._date)  # 재시작해도 되살아나지 않는다

    assert core.holding_label("005930") == ""


# ── 명령 분배 구조 ──────────────────────────────────────────────


def _command_types():
    """bus 에 정의된 '명령' dataclass 이름 전부 (이벤트는 제외)."""
    import inspect

    handled = set()
    for name in dir(bus):
        obj = getattr(bus, name)
        if inspect.isclass(obj) and obj.__module__ == bus.__name__:
            handled.add(name)
    return handled


def test_모든_명령이_어느_묶음엔가_속한다(core):
    """분배표에서 빠진 명령은 조용히 무시된다 — 눌러도 아무 일이 안 일어난다.

    _handle_command 는 21 가지를 한 함수에서 처리하다 303줄이 됐다. 묶음을 나눈 뒤로는
    새 명령을 추가할 때 어느 묶음인지 정하지 않으면 여기서 걸린다.
    """
    import inspect
    import re

    source = inspect.getsource(core._handle_command)
    routed = set(re.findall(r"bus\.(\w+)\(\)", source))

    groups = {}
    for method in (
        core._handle_position_command,
        core._handle_symbol_command,
        core._handle_journal_command,
        core._handle_system_command,
    ):
        groups[method.__name__] = set(
            re.findall(r"case bus\.(\w+)", inspect.getsource(method))
        )

    # 분배표에 적힌 것과 각 묶음이 실제로 처리하는 것이 일치해야 한다
    assert routed == set().union(*groups.values())
    # 한 명령이 두 묶음에 있으면 뒤엣것은 절대 실행되지 않는다
    seen = [n for names in groups.values() for n in names]
    assert len(seen) == len(set(seen)), "두 묶음에 중복된 명령이 있습니다"


def test_포지션을_바꾸는_명령은_한_묶음에만_있다(core):
    """잔량·평단에 닿는 명령이 흩어지면 어디를 조심해야 할지 알 수 없다."""
    import inspect
    import re

    position_commands = {"ManualSell", "CarryPosition", "CarryOver", "Reset"}
    for method in (
        core._handle_symbol_command,
        core._handle_journal_command,
        core._handle_system_command,
    ):
        names = set(re.findall(r"case bus\.(\w+)", inspect.getsource(method)))
        assert not (names & position_commands), method.__name__

    handled = set(
        re.findall(r"case bus\.(\w+)", inspect.getsource(core._handle_position_command))
    )
    assert position_commands <= handled
