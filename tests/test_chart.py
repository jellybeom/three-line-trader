"""복기 차트 — 계산 함수, 렌더링, 조회 파싱, 전송, 코어 파이프라인 테스트.

렌더링 테스트는 실제 PNG 를 생성하므로 다른 테스트보다 느리다 (~2초).
"""

import asyncio
import warnings

import pytest

from trader.chart import Bar, Fill, day_low_steps, render_daily, render_minute, sma

warnings.filterwarnings(
    "ignore", message="Glyph"
)  # 컨테이너에 한글 폰트가 없을 때 소음 방지

P_LINES = (10_000.0, 9_500.0, 9_000.0)


def _bars(n: int, minute: bool = False, start_price: float = 10_000) -> list[Bar]:
    bars, price = [], start_price
    for i in range(n):
        if minute:
            day, slot = divmod(i, 130)
            hh, mm = divmod(9 * 60 + slot * 3, 60)
            key = f"202607{21 + day:02d}{hh:02d}{mm:02d}00"
        else:
            key = f"2026{4 + i // 30:02d}{1 + i % 30:02d}"
        o = price
        c = price * (1 + (0.004 if i % 3 else -0.005))
        bars.append(
            Bar(
                key,
                o,
                max(o, c) * 1.002,
                min(o, c) * 0.998,
                c,
                1000 + i,
                (1000 + i) * c,
            )
        )
        price = c
    return bars


# ── 계산 함수 ──────────────────────────────────────────────────


def test_이동평균은_데이터가_모자란_구간에_선을_긋지_않는다():
    line = sma([1, 2, 3, 4, 5], 3)
    assert line == [None, None, 2.0, 3.0, 4.0]
    assert sma([1, 2], 5) == [None, None]


def test_바닥대비_계단은_날짜가_바뀌면_리셋된다():
    bars = [
        Bar("20260724090000", 100, 101, 95, 100),
        Bar("20260724090300", 100, 101, 92, 100),  # 그날 최저 갱신
        Bar("20260724090600", 100, 101, 96, 100),  # 갱신 없음 — 92 유지
        Bar("20260725090000", 100, 101, 99, 100),  # 새 날 — 99 로 리셋
    ]
    assert day_low_steps(bars) == [95, 92, 92, 99]


# ── 렌더링 (실제 PNG 생성) ─────────────────────────────────────


def _assert_png(path: str) -> None:
    with open(path, "rb") as f:
        assert f.read(8) == b"\x89PNG\r\n\x1a\n"


def test_일봉_차트_생성(tmp_path):
    fills = [
        Fill("2026-07-24 09:31:00", "매수", 10_000),
        Fill("2026-07-24 14:10:00", "매도", 10_300),
    ]
    path = render_daily(
        tmp_path / "d.png",
        "테스트(005930)",
        _bars(180),
        P_LINES,
        fills,
        kospi=_bars(180, start_price=2_600),
    )
    _assert_png(path)


def test_KOSPI_데이터가_없어도_일봉_차트는_생성된다(tmp_path):
    path = render_daily(
        tmp_path / "d.png", "테스트", _bars(70), P_LINES, [], kospi=None
    )
    _assert_png(path)


def test_3분봉_차트_생성(tmp_path):
    path = render_minute(
        tmp_path / "m.png",
        "테스트 3분봉",
        _bars(260, minute=True),
        P_LINES,
        [Fill("2026-07-21 10:00:00", "매수", 10_000)],
    )
    _assert_png(path)


# ── Broker 차트 응답 파싱 ──────────────────────────────────────


@pytest.fixture
def auth():
    from trader.kiwoom import KiwoomAuth

    a = KiwoomAuth(appkey="k", secretkey="s", mock=True)
    a._token, a._expires_at = "t", __import__("datetime").datetime(2099, 1, 1)
    return a


def _chart_response(rows):
    class R:
        status_code = 200

        def json(self):
            return {"return_code": 0, "stk_dt_pole_chart_qry": rows}

    return R()


def test_일봉_파싱은_필드명_후보를_탐색하고_오름차순_정렬(auth, monkeypatch):
    from trader.broker import Broker

    rows = [  # 서버는 최신순으로 준다
        {
            "dt": "20260724",
            "open_pric": "+2500",
            "high_pric": "2600",
            "low_pric": "-2400",
            "cur_prc": "2550",
            "trde_qty": "1000",
            "trde_prica": "255",
        },
        {
            "dt": "20260723",
            "open_pric": "2400",
            "high_pric": "2500",
            "low_pric": "2300",
            "cur_prc": "+2500",
            "trde_qty": "900",
            "trde_prica": "225",
        },
    ]
    monkeypatch.setattr(
        "trader.broker.requests.post", lambda *a, **k: _chart_response(rows)
    )
    bars = Broker(auth).daily_chart("005930")
    assert [b[0] for b in bars] == ["20260723", "20260724"]
    assert bars[1][1:5] == (2500.0, 2600.0, 2400.0, 2550.0)  # 부호는 절댓값 처리


def test_분봉_파싱은_시각만_오면_날짜와_결합(auth, monkeypatch):
    from trader.broker import Broker

    rows = [
        {
            "dt": "20260724",
            "cntr_tm": "093000",
            "cur_prc": "100",
            "open_pric": "99",
            "high_pric": "101",
            "low_pric": "98",
            "trde_qty": "10",
        }
    ]
    monkeypatch.setattr(
        "trader.broker.requests.post", lambda *a, **k: _chart_response(rows)
    )
    bars = Broker(auth).minute_chart("005930")
    assert bars[0][0] == "20260724093000"


def test_계좌_요약_파싱(auth, monkeypatch):
    """실측(2026-08-01) 응답 — 영웅문 [국내잔고] 화면과 값이 일치했다."""
    from trader.broker import Broker

    def fake_post(url, headers=None, json=None, timeout=None):
        class R:
            status_code = 200

            def json(self):
                return {
                    "return_code": 0,
                    "tot_pur_amt": "000000000307914",
                    "tot_evlt_amt": "000000000315425",
                    "tot_evlt_pl": "000000000006801",
                    "tot_prft_rt": "2.21",
                    "prsm_dpst_aset_amt": "000000001344071",
                }

        return R()

    monkeypatch.setattr("trader.broker.requests.post", fake_post)
    summary = Broker(auth).account_summary()
    assert summary == {
        "purchase": 307_914,
        "value": 315_425,
        "pnl": 6_801,
        "rate": 2.21,
        "asset": 1_344_071,
    }


def test_지수는_100배_스케일을_보정한다(auth, monkeypatch):
    """실측 2026-07-24: KOSPI 응답 669062 → 실제 지수 6,690.62."""
    from trader.broker import Broker

    rows = [
        {
            "dt": "20260724",
            "open_pric": "667000",
            "high_pric": "670000",
            "low_pric": "665000",
            "cur_prc": "669062",
        }
    ]
    monkeypatch.setattr(
        "trader.broker.requests.post", lambda *a, **k: _chart_response(rows)
    )
    bars = Broker(auth).index_daily()
    assert bars[0][4] == 6690.62
    assert bars[0][2] == 6700.0


def test_거래대금_단위는_자동으로_원으로_보정된다(auth, monkeypatch):
    """영웅문 표기처럼 백만원 단위로 와도 종가×거래량 대조로 10^n 스케일을 맞춘다."""
    from trader.broker import Broker

    rows = [
        {
            "dt": f"202607{d:02d}",
            "open_pric": "2500",
            "high_pric": "2600",
            "low_pric": "2400",
            "cur_prc": "2500",
            "trde_qty": "1000000",
            "trde_prica": "2500",
        }  # 실제 25억원(2500×100만)이 '백만원' 단위 2,500 으로 옴
        for d in range(1, 11)
    ]
    monkeypatch.setattr(
        "trader.broker.requests.post", lambda *a, **k: _chart_response(rows)
    )
    bars = Broker(auth).daily_chart("005930")
    assert bars[-1][6] == 2_500_000_000  # 원 단위로 복원


def test_거래대금이_이미_원_단위면_그대로_둔다(auth, monkeypatch):
    from trader.broker import Broker

    rows = [
        {
            "dt": f"202607{d:02d}",
            "cur_prc": "2500",
            "open_pric": "2500",
            "high_pric": "2600",
            "low_pric": "2400",
            "trde_qty": "1000",
            "trde_prica": "2500000",
        }
        for d in range(1, 11)
    ]
    monkeypatch.setattr(
        "trader.broker.requests.post", lambda *a, **k: _chart_response(rows)
    )
    assert Broker(auth).daily_chart("005930")[-1][6] == 2_500_000


def test_차트는_통합코드를_먼저_조회한다(auth, monkeypatch):
    """영웅문 통합차트와 값을 맞추려면 KRX 단독이 아니라 통합(_AL) 시세여야 한다."""
    from trader.broker import Broker

    codes = []

    def fake_post(url, headers=None, json=None, timeout=None):
        codes.append(json["stk_cd"])
        return _chart_response(
            [
                {
                    "dt": "20260515",
                    "open_pric": "51300",
                    "high_pric": "52200",
                    "low_pric": "43550",
                    "cur_prc": "44600",
                    "trde_qty": "2319434",
                }
            ]
        )

    monkeypatch.setattr("trader.broker.requests.post", fake_post)
    bars = Broker(auth).daily_chart("475150")
    assert codes == ["475150_AL"]  # 통합 코드로 한 번에 성공 → 추가 호출 없음
    assert bars[0][1] == 51300 and bars[0][4] == 44600  # 실측 HTS 값과 동일


def test_통합코드가_비면_원래_코드로_되돌린다(auth, monkeypatch):
    """NXT 미상장 종목 등에서 접미사가 통하지 않아도 차트가 나와야 한다."""
    from trader.broker import Broker

    codes = []

    def fake_post(url, headers=None, json=None, timeout=None):
        code = json["stk_cd"]
        codes.append(code)
        if code.endswith("_AL"):
            return _chart_response([])
        return _chart_response(
            [
                {
                    "dt": "20260515",
                    "open_pric": "50900",
                    "high_pric": "50900",
                    "low_pric": "43550",
                    "cur_prc": "44450",
                    "trde_qty": "1084635",
                }
            ]
        )

    monkeypatch.setattr("trader.broker.requests.post", fake_post)
    bars = Broker(auth).daily_chart("475150")
    assert codes == ["475150_AL", "475150"]
    assert bars[0][4] == 44450


def test_시가가_고저_범위를_벗어나면_다른_후보나_종가를_쓴다(auth, monkeypatch):
    """실측 2026-07-27: 시가만 어긋나 캔들이 갭으로 시작하고 양봉/음봉이 뒤집혔다."""
    from trader.broker import Broker

    rows = [
        {
            "dt": "20260727",
            "open_pric": "99999",  # 범위 밖 — 오독으로 판단
            "high_pric": "2600",
            "low_pric": "2400",
            "cur_prc": "2550",
        }
    ]
    monkeypatch.setattr(
        "trader.broker.requests.post", lambda *a, **k: _chart_response(rows)
    )
    bar = Broker(auth).daily_chart("005930")[0]
    assert bar[1] == 2550.0  # 종가로 대체(도지)
    assert bar[2] == 2600.0 and bar[3] == 2400.0  # 고저는 그대로


def test_정상_시가는_그대로_사용된다(auth, monkeypatch):
    from trader.broker import Broker

    rows = [
        {
            "dt": "20260727",
            "open_pric": "2500",
            "high_pric": "2600",
            "low_pric": "2400",
            "cur_prc": "2450",
        }
    ]
    monkeypatch.setattr(
        "trader.broker.requests.post", lambda *a, **k: _chart_response(rows)
    )
    bar = Broker(auth).daily_chart("005930")[0]
    assert bar[1] == 2500.0 and bar[4] == 2450.0  # 시가 > 종가 → 음봉


def test_종가가_없는_행은_건너뛴다(auth, monkeypatch):
    from trader.broker import Broker

    rows = [{"dt": "20260724", "cur_prc": ""}, {"dt": "20260723", "cur_prc": "100"}]
    monkeypatch.setattr(
        "trader.broker.requests.post", lambda *a, **k: _chart_response(rows)
    )
    assert len(Broker(auth).daily_chart("005930")) == 1


# ── 코어 파이프라인 ────────────────────────────────────────────


def _rows(n, minute=False):
    out = []
    for i in range(n):
        if minute:
            day, slot = divmod(i, 130)
            hh, mm = divmod(9 * 60 + slot * 3, 60)
            key = f"202607{23 + day:02d}{hh:02d}{mm:02d}00"
            out.append(
                {
                    "cntr_tm": key,
                    "open_pric": "100",
                    "high_pric": "101",
                    "low_pric": "99",
                    "cur_prc": "100",
                    "trde_qty": "10",
                }
            )
        else:
            out.append(
                {
                    "dt": f"2026{4 + i // 30:02d}{1 + i % 30:02d}",
                    "open_pric": "100",
                    "high_pric": "101",
                    "low_pric": "99",
                    "cur_prc": "100",
                    "trde_qty": "10",
                    "trde_prica": "1",
                }
            )
    return out


class ChartStubBroker:
    def daily_chart(self, symbol, count=180):
        from trader.broker import Broker

        return Broker._parse_bars(
            Broker.__new__(Broker), {"list": _rows(180)}, minute=False
        )

    def minute_chart(self, symbol, interval=3):
        from trader.broker import Broker

        return Broker._parse_bars(
            Broker.__new__(Broker), {"list": _rows(260, minute=True)}, minute=True
        )

    def index_daily(self, code="001", count=180):
        from trader.broker import BrokerError

        raise BrokerError("지수 TR 미지원")  # KOSPI 생략 경로 검증


def test_차트_요청이_PNG_생성과_ChartReady_이벤트로_이어진다(tmp_path):
    from trader.core import Core
    from trader.state_machine import Params, Position, State
    from trader.store import Store
    from trader.ui import bus

    b = bus.Bus()
    core = Core(b, db_dir=str(tmp_path))
    core._date = "2026-07-24"
    core._store = Store(str(tmp_path / "t.db"))
    core._broker = ChartStubBroker()
    params = Params(line1=101, line2=100, line3=99, buy1_amount=110, buy2_amount=110)
    pos = Position(state=State.BUY1, avg_price=100, total_bought=10, remaining=10)
    core._store.register_symbol(core._date, "005930", "삼성전자", params, pos)
    core._entries["005930"] = {
        "name": "삼성전자",
        "params": params,
        "pos": pos,
        "price": 100,
        "memo": "",
        "high": 101,
        "low": 99,
    }

    asyncio.run(core._chart_task("005930", to_ui=True))
    events = []
    while not b.events.empty():
        events.append(b.events.get_nowait())
    ready = [e for e in events if isinstance(e, bus.ChartReady)]
    assert ready, "ChartReady 이벤트가 없음"
    for path in (ready[0].daily_path, ready[0].minute_path):
        with open(path, "rb") as f:
            assert f.read(4) == b"\x89PNG"
    core._store.close()


def test_종료_체결시_Discord_로_차트가_자동_전송된다(tmp_path):
    """매매가 있었던 종목이 '종료' 되면 복기 차트 2장이 자동 발송되어야 한다."""
    from trader.core import Core
    from trader.state_machine import Params, Position, State
    from trader.store import Store
    from trader.ui import bus

    sent = []

    class FakeBot:
        """봇 발송 인터페이스 (async) — 실제 채널 없이 호출만 기록한다."""

        async def send_text(self, text):
            sent.append(("text", [text]))
            return True

        async def send_embed(self, embed):
            sent.append(("embed", [embed]))
            return True

        async def send_images(self, paths, caption="", thread_key=None):
            sent.append(("images", list(paths), thread_key))  # 한 메시지에 여러 장
            return True

    class Broker(ChartStubBroker):
        def sell(self, s, q):
            return "ORD2"

        def deposit(self):
            return 10**9

        def holdings(self):
            return {}

        def holdings_detail(self):
            return {}

    b = bus.Bus()
    core = Core(b, db_dir=str(tmp_path))
    core._date = "2026-07-24"
    core._store = Store(str(tmp_path / "t.db"))
    core._broker = Broker()
    core._bot = FakeBot()
    core._notify_level = "매매만 (시스템 제외)"
    core._running = True
    params = Params(line1=101, line2=100, line3=99, buy1_amount=110, buy2_amount=110)
    pos = Position(state=State.BUY2, avg_price=100, total_bought=10, remaining=10)
    core._store.register_symbol(core._date, "005930", "삼성전자", params, pos)
    core._entries["005930"] = {
        "name": "삼성전자",
        "params": params,
        "pos": pos,
        "price": 100,
        "memo": "",
        "high": 101,
        "low": 99,
    }

    async def scenario():
        from trader.watcher import Tick

        await core._on_tick(Tick("005930", 98, ""))  # 3선 이탈 → 전량 손절 주문
        await core._on_fill_values(
            {
                "9203": "ORD2",
                "9001": "A005930",
                "913": "체결",
                "911": "10",
                "910": "98",
                "902": "0",
            }
        )
        for _ in range(50):  # 백그라운드 차트 작업 완료 대기
            await asyncio.sleep(0.1)
            if any(kind == "images" for kind, *_ in sent):
                break

    asyncio.run(scenario())
    batches = [p for kind, p, *_ in sent if kind == "images"]
    assert len(batches) == 1, sent  # 사진 2장이 '한 번의' 발송으로
    assert len(batches[0]) == 2
    assert any("daily" in p for p in batches[0]) and any(
        "minute" in p for p in batches[0]
    )
    core._store.close()


def test_시간외_봉_판별():
    """20:05 리뷰 — 15:30 이후 봉이 있는 종목만 재전송 대상."""
    today = "20260724"
    rows_krx_only = [
        (f"{today}152700", 1, 1, 1, 1, 1, 1),
        (f"{today}153000", 1, 1, 1, 1, 1, 1),
    ]
    rows_nxt = rows_krx_only + [(f"{today}160300", 1, 1, 1, 1, 1, 1)]
    check = lambda rows: any(r[0][:8] == today and r[0][8:12] > "1530" for r in rows)
    assert check(rows_krx_only) is False
    assert check(rows_nxt) is True


def test_Discord_명령으로_요청하면_Discord로_전송된다(tmp_path):
    """명령을 낸 자리에서 결과를 봐야 한다 — UI 창만 뜨면 원격 조회의 의미가 없다."""
    from trader.core import Core
    from trader.state_machine import Params, Position, State
    from trader.store import Store
    from trader.ui import bus

    sent = []

    class FakeBot:
        async def send_images(self, paths, caption="", thread_key=None):
            sent.append((list(paths), caption, thread_key))
            return True

        def set_blocked(self, *a):
            pass

    b = bus.Bus()
    core = Core(b, db_dir=str(tmp_path))
    core._date = "2026-08-06"
    core._store = Store(str(tmp_path / "t.db"))
    core._broker = ChartStubBroker()
    core._bot = FakeBot()
    params = Params(line1=101, line2=100, line3=99, buy1_amount=110, buy2_amount=110)
    pos = Position(state=State.BUY1, avg_price=100, total_bought=10, remaining=10)
    core._store.register_symbol(core._date, "005930", "삼성전자", params, pos)
    core._entries["005930"] = {
        "name": "삼성전자",
        "params": params,
        "pos": pos,
        "price": 100,
        "memo": "",
        "high": 101,
        "low": 99,
        "day_low": 99,
    }

    async def scenario():
        core.request_chart("005930", to_discord=True)
        await core._drain_commands()
        for _ in range(50):
            await asyncio.sleep(0.1)
            if sent:
                break

    asyncio.run(scenario())
    assert sent, "Discord 로 전송되지 않음"
    paths, caption, thread_key = sent[0]
    assert len(paths) == 2 and "삼성전자" in caption

    events = []
    while not b.events.empty():
        events.append(b.events.get_nowait())
    assert not [
        e for e in events if isinstance(e, bus.ChartReady)
    ]  # UI 창은 뜨지 않는다
    core._store.close()


def test_UI_버튼은_기존대로_창으로_뜬다(tmp_path):
    from trader.core import Core
    from trader.state_machine import Params, Position, State
    from trader.store import Store
    from trader.ui import bus

    b = bus.Bus()
    core = Core(b, db_dir=str(tmp_path))
    core._date = "2026-08-06"
    core._store = Store(str(tmp_path / "t.db"))
    core._broker = ChartStubBroker()
    params = Params(line1=101, line2=100, line3=99, buy1_amount=110, buy2_amount=110)
    pos = Position(state=State.BUY1, avg_price=100, total_bought=10, remaining=10)
    core._store.register_symbol(core._date, "005930", "삼성전자", params, pos)
    core._entries["005930"] = {
        "name": "삼성전자",
        "params": params,
        "pos": pos,
        "price": 100,
        "memo": "",
        "high": 101,
        "low": 99,
        "day_low": 99,
    }

    async def scenario():
        core.request_chart("005930")  # to_discord 기본값 False
        await core._drain_commands()
        for _ in range(50):
            await asyncio.sleep(0.1)
            if any(isinstance(e, bus.ChartReady) for e in list(b.events.queue)):
                return

    asyncio.run(scenario())
    assert any(isinstance(e, bus.ChartReady) for e in list(b.events.queue))
    core._store.close()


def test_계단_지표는_당일_저가_대비_3_5_7퍼센트다():
    """익절 트리거(평단 대비)와는 기준점이 다른 지표다 — 숫자만 같을 뿐이다.

    10% 이상 선은 캔들에서 멀어 y축을 늘리고 캔들을 아래로 눌러 제외했다.
    """
    from trader.chart import _STEP_PCTS, day_low_steps

    assert _STEP_PCTS == (0.03, 0.05, 0.07)

    bars = [
        Bar("20260807090000", 100, 101, 100, 100),
        Bar("20260807090300", 100, 101, 90, 95),
    ]
    lows = day_low_steps(bars)
    assert lows == [100, 90]  # 계단의 바닥은 '그날 누적 최저가' 이지 평단이 아니다


def test_종료_차트는_매매일지_스레드로_간다(tmp_path):
    """복기할 때 보는 것이라 답글 다는 자리 바로 위에 있어야 채널을 오가지 않는다.

    `/차트` 로 직접 부른 것은 지금 보려는 것이므로 평소대로 알림 채널로 간다.
    """
    sent = []

    class Bot:
        async def send_images(self, paths, caption="", thread_key=None):
            sent.append(thread_key)
            return True

    from trader.core import Core
    from trader.store import Store
    from trader.ui import bus

    core = Core(bus.Bus(), db_dir=str(tmp_path))
    core._date = "2026-08-27"
    core._store = Store(str(tmp_path / "t.db"))
    core._bot = Bot()
    asyncio.run(core._send_chart_images("005930", ("a.png", "b.png"), to_thread=True))
    asyncio.run(core._send_chart_images("005930", ("a.png", "b.png")))

    assert sent[0] == (core._date, "005930")  # 자동 차트 → 스레드
    assert sent[1] is None  # 직접 요청 → 알림 채널
    core._store.close()


def test_차트_제목의_손익은_사이클_전체다(tmp_path):
    """차트는 사이클에 한 번 나온다 — "이 매매로 최종 얼마를 벌었나" 를 답해야 한다.

    스키마 v12 에서 손익을 날짜별로 쪼갠 뒤라 pos.realized_pnl 은 청산일 하루치뿐이다.
    화요일 1차 익절 +4,000, 수요일 전량 매도 +6,000 이면 차트에 +6,000 만 찍혀
    성적을 과소평가하게 된다(2026-09-03 발견). 문서·종료 알림과 같은 값이어야 한다.
    """
    from trader.core import Core
    from trader.state_machine import Decision, Params, Position, Side, State
    from trader.store import Store
    from trader.ui import bus

    core = Core(bus.Bus(), db_dir=str(tmp_path))
    core._store = Store(str(tmp_path / "t.db"))
    params = Params(
        line1=5_000, line2=4_500, line3=4_000, buy1_amount=500_000, buy2_amount=500_000
    )
    for date in ("2026-09-01", "2026-09-02"):
        core._store.register_symbol(date, "005930", "삼성전자", params)
    core._store.save_transition(  # 화: 1차 익절 +4,000
        "2026-09-01",
        "005930",
        State.BUY1,
        Position(State.BUY1_TP1, 5_000, 100, 60, realized_pnl=4_000, fees=100),
        Decision(State.BUY1_TP1, Side.SELL, 40, "1차 익절"),
        5_100,
        5_100,
    )
    core._store.save_transition(  # 수: 전량 매도 +6,000
        "2026-09-02",
        "005930",
        State.BUY1_TP1,
        Position(State.CLOSED, 5_000, 100, 0, realized_pnl=6_000, fees=150),
        Decision(State.CLOSED, Side.SELL, 60, "본절 이탈"),
        5_100,
        5_100,
    )
    core._date = "2026-09-02"

    realized, fees = core._store.cycle_totals("005930", core._date)

    assert realized - fees == 9_750  # 4,000+6,000 − 250, 하루치 5,850 이 아니다
    core._store.close()


def test_차트_손익은_워커_스레드에서_계산하지_않는다():
    """_build_charts 는 워커 스레드에서 돈다 — SQLite 는 스레드 전용이다."""
    import inspect

    from trader.core import Core

    source = inspect.getsource(Core._build_charts)
    assert "self._store" not in source
    assert "cycle_net" in source  # 호출부가 넘겨준 값을 그대로 쓴다
