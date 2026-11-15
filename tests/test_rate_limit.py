"""계좌 TR 유량제어 — 2026-11-12 18시부터 초당 1회 (키움 공지 2026-11-09, 종전 5회).

대상은 kt000xx · kt500xx. 우리가 쓰는 것은 예수금(kt00001)과 잔고(kt00018)다.
예전에는 모든 REST 호출에 0.25초 간격만 두어, 같은 계좌 TR 이 1초 안에 여러 번 나갈 수
있었다. 그리고 매수 직전 예수금 조회가 실패하면 예외가 틱 처리 밖으로 새어 실시간
연결 세션을 끊었다.
"""

import asyncio
import itertools
import threading
import time

import pytest

from trader.broker import Broker, BrokerError, RateLimitError, is_account_tr
from trader.core import Core
from trader.state_machine import Params, Position, State
from trader.store import Store
from trader.ui import bus
from trader.watcher import Tick

FAST = 0.2  # 테스트용 계좌 TR 간격 (실제 1.2초)


class _Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code = status
        self._data = data
        self.text = text

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


def _broker(monkeypatch, responder=None):
    """네트워크 없이 발송 시각만 기록하는 Broker."""
    b = Broker(auth=None)
    monkeypatch.setattr(Broker, "_ACCOUNT_MIN_INTERVAL", FAST)
    monkeypatch.setattr(Broker, "_MIN_INTERVAL", 0.01)
    sent = []

    def post(path, api_id, body):
        sent.append((time.monotonic(), api_id, dict(body)))
        if responder:
            return responder(api_id, body, len(sent))
        return _Resp(data={"return_code": 0, "ord_no": "1", "ord_alow_amt": "1000"})

    monkeypatch.setattr(b, "_post", post)
    return b, sent


def _gaps(sent, api_id):
    times = [t for t, a, _ in sent if a == api_id]
    return [b - a for a, b in itertools.pairwise(times)]


def test_공지_대상_TR_만_계좌_TR_로_본다():
    assert is_account_tr("kt00001") and is_account_tr("kt00018")
    assert is_account_tr("kt50020")
    # 주문과 시세·미체결 조회는 대상이 아니다
    for api_id in ("kt10000", "kt10001", "ka10001", "ka10075", "ka10076"):
        assert not is_account_tr(api_id)


def test_같은_계좌_TR_은_간격을_두고_나간다(monkeypatch):
    b, sent = _broker(monkeypatch)
    for _ in range(3):
        b._request_once("/acnt", "kt00018", {})

    assert all(g >= FAST * 0.95 for g in _gaps(sent, "kt00018"))


def test_여러_스레드가_동시에_불러도_간격을_지킨다(monkeypatch):
    b, sent = _broker(monkeypatch)
    threads = [
        threading.Thread(target=b._request_once, args=("/acnt", "kt00001", {}))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    gaps = _gaps(sent, "kt00001")
    assert len(gaps) == 3 and all(g >= FAST * 0.95 for g in gaps)


def test_계좌_조회가_줄_서_있어도_주문은_기다리지_않는다(monkeypatch):
    """계좌 TR 대기를 전체 잠금 안에서 하면 그동안 손절 주문까지 막힌다."""
    b, _ = _broker(monkeypatch)
    monkeypatch.setattr(Broker, "_ACCOUNT_MIN_INTERVAL", 1.0)
    b._request_once("/acnt", "kt00018", {})  # 문을 한 번 쓴다
    waiting = threading.Thread(target=b._request_once, args=("/acnt", "kt00018", {}))
    waiting.start()  # 1초 대기에 들어간다
    time.sleep(0.05)

    started = time.monotonic()
    b._request_once("/ordr", "kt10001", {})
    elapsed = time.monotonic() - started
    waiting.join()

    assert elapsed < 0.3, elapsed


def test_다른_계좌_TR_끼리는_서로_기다리지_않는다(monkeypatch):
    b, _ = _broker(monkeypatch)
    monkeypatch.setattr(Broker, "_ACCOUNT_MIN_INTERVAL", 1.0)
    started = time.monotonic()
    b._request_once("/acnt", "kt00001", {})
    b._request_once("/acnt", "kt00018", {})

    assert time.monotonic() - started < 0.3


def test_횟수_초과_응답은_RateLimitError_다(monkeypatch):
    b, _ = _broker(
        monkeypatch, lambda *a: _Resp(429, {"return_code": 5, "return_msg": "초과"})
    )
    with pytest.raises(RateLimitError):
        b._request_once("/acnt", "kt00018", {})

    b, _ = _broker(
        monkeypatch,
        lambda *a: _Resp(
            200, {"return_code": 5, "return_msg": "허용된 요청 개수를 초과하였습니다"}
        ),
    )
    with pytest.raises(RateLimitError):
        b._request_once("/acnt", "kt00018", {})

    b, _ = _broker(monkeypatch, lambda *a: _Resp(429, None, "Too Many Requests"))
    with pytest.raises(RateLimitError):
        b._request_once("/acnt", "kt00018", {})


def test_일반_거부는_RateLimitError_가_아니다(monkeypatch):
    b, _ = _broker(
        monkeypatch,
        lambda *a: _Resp(
            200, {"return_code": 20, "return_msg": "매수증거금이 부족합니다"}
        ),
    )
    with pytest.raises(BrokerError) as info:
        b._request_once("/ordr", "kt10000", {})
    assert not isinstance(info.value, RateLimitError)


def test_횟수_초과면_제한_창만큼_쉬고_다시_조회한다(monkeypatch):
    """제한이 TR 별이 아니라 계좌 TR 합산이어도, 거부되면 쉬었다 다시 해서 맞춘다."""

    def responder(api_id, body, n):
        if n == 1:
            return _Resp(429, {"return_code": 5, "return_msg": "초과"})
        return _Resp(data={"return_code": 0})

    b, sent = _broker(monkeypatch, responder)
    b._request("/acnt", "kt00018", {}, retries=2)

    assert len(sent) == 2
    assert sent[1][0] - sent[0][0] >= FAST * 0.95


def test_주문은_횟수_초과여도_재시도하지_않는다(monkeypatch):
    """응답만 잃고 접수됐을 수 있다 — 주문 재시도는 코어가 잔고를 확인한 뒤에만 한다."""
    b, sent = _broker(
        monkeypatch, lambda *a: _Resp(429, {"return_code": 5, "return_msg": "초과"})
    )
    with pytest.raises(BrokerError):
        b.sell("005930", 1)
    assert len(sent) == 1


def test_주문가능금액이_0원이면_0원으로_받아들인다(monkeypatch):
    """예전에는 0 이면 조회 방식을 바꿔가며 2~3번 더 불렀다 — 초당 1회에서는 곧 거부다."""
    key = Broker._DEPOSIT_KEYS[0]
    b, sent = _broker(monkeypatch, lambda *a: _Resp(data={"return_code": 0, key: "0"}))
    b._deposit_hint = ("2", key)

    assert b.deposit() == 0.0
    assert len(sent) == 1
    assert b._deposit_hint == ("2", key)


def test_필드가_사라졌을_때만_조회_방식을_다시_찾는다(monkeypatch):
    key = Broker._DEPOSIT_KEYS[0]

    def responder(api_id, body, n):
        if n == 1:
            return _Resp(data={"return_code": 0})  # 힌트 필드가 없다
        return _Resp(data={"return_code": 0, key: "5000"})

    b, sent = _broker(monkeypatch, responder)
    b._deposit_hint = ("3", "없는필드")

    assert b.deposit() == 5000.0
    assert all(g >= FAST * 0.95 for g in _gaps(sent, "kt00001"))


# ── 코어: 예수금 조회 ────────────────────────────────────────

P = Params(
    line1=10_000, line2=9_000, line3=8_000, buy1_amount=1_000_000, buy2_amount=900_000
)


class FakeBroker:
    def __init__(self):
        self.orders = []
        self.deposit_calls = 0
        self.deposit_error = None
        self.delay = 0.0

    def deposit(self):
        self.deposit_calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.deposit_error:
            raise self.deposit_error
        return 100_000_000

    def buy(self, symbol, qty):
        self.orders.append(("매수", symbol, qty))
        return f"ORD{len(self.orders)}"

    def sell(self, symbol, qty):
        self.orders.append(("매도", symbol, qty))
        return f"ORD{len(self.orders)}"

    def holdings(self):
        return {}

    def holdings_detail(self):
        return {}

    def open_orders(self, symbol=None):
        return []


@pytest.fixture
def core(tmp_path):
    c = Core(bus.Bus())
    c._store = Store(tmp_path / "t.db")
    c._date = "2026-11-13"
    c._broker = FakeBroker()
    c._running = True
    yield c
    c._store.close()


def _add(c, symbol, pos=None):
    pos = pos or Position()
    c._store.register_symbol(c._date, symbol, symbol, P, pos)
    c._entries[symbol] = {"name": symbol, "params": P, "pos": pos, "price": 0}


def test_예수금_조회가_실패해도_예외가_틱_밖으로_새지_않는다(core):
    """새면 실시간 연결 세션이 끊기고 같은 묶음의 체결통보까지 잃는다."""
    _add(core, "005930")
    core._broker.deposit_error = RateLimitError("kt00001 실패 (HTTP 429)")

    asyncio.run(core._on_tick(Tick("005930", 9_950, "")))  # 예외 없이 끝나야 한다

    assert core._broker.orders == []  # 잔고를 모르는 채 사지 않는다
    assert core._entries["005930"]["pos"].state is State.WAITING
    assert not core._entering and not core._in_flight  # 자리 예약도 돌려준다
    logs = " ".join(str(r) for r in core._store.day_log(core._date))
    assert "예수금 조회 실패" in logs


def test_조회_실패_직후에는_틱마다_다시_부르지_않는다(core):
    _add(core, "005930")
    core._broker.deposit_error = BrokerError("kt00001 실패")

    async def run():
        for _ in range(5):
            await core._on_tick(Tick("005930", 9_950, ""))

    asyncio.run(run())

    assert core._broker.deposit_calls == 1


def test_실패가_풀리면_다음_틱에_매수한다(core):
    _add(core, "005930")
    core._broker.deposit_error = BrokerError("kt00001 실패")

    async def run():
        await core._on_tick(Tick("005930", 9_950, ""))
        core._broker.deposit_error = None
        core._deposit_fail = (0.0, "")  # 재조회 대기(2초)가 지났다
        await core._on_tick(Tick("005930", 9_950, ""))

    asyncio.run(run())

    assert [o[:2] for o in core._broker.orders] == [("매수", "005930")]


def test_동시에_들어온_예수금_조회는_한_번만_부른다(core):
    core._broker.delay = 0.05

    async def run():
        return await asyncio.gather(*(core._get_deposit() for _ in range(5)))

    values = asyncio.run(run())

    assert values == [100_000_000] * 5
    assert core._broker.deposit_calls == 1


def test_조회_중에_캐시를_버리면_그_값은_캐시에_남기지_않는다(core):
    """매도 체결 전에 떠난 조회가 체결 뒤에 돌아와 옛 잔고를 캐시에 넣으면 안 된다."""
    core._broker.delay = 0.05

    async def run():
        task = asyncio.ensure_future(core._get_deposit())
        await asyncio.sleep(0.01)
        core._invalidate_deposit()  # 매도 체결
        await task
        assert core._deposit_cache is None
        await core._get_deposit()

    asyncio.run(run())

    assert core._broker.deposit_calls == 2
    