"""core 단위 테스트 — 스텁 broker 로 '판단 → 주문 → 체결통보 → 확정' 흐름과
예수금 방어 정책을 네트워크 없이 검증한다.
"""

import asyncio

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
                (),
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
