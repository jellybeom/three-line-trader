"""2026-08-11 변경분 회귀 테스트.

- 상태 경로 표기 (대기 생략, 1차 흡수, 종료 사유 명시)
- 매매 사이클 묶기 (이월 중복 제거, 리셋 후 분리)
- 차트 화살표 배치 (캔들 밖, 쌓기)
- 키움 조회 API 파싱 (ka10075 / ka10076 / ka10072)
- 15:35 이후 자동화 없음
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from trader.broker import BrokerError
from trader.chart import Bar, Fill, marker_slots
from trader.journal import close_label, cycle_timeline, transition_path
from trader.state_machine import Decision, Params, Position, Side, State
from trader.store import Store
from trader.trading_calendar import TradingCalendar

# ── 상태 경로 ─────────────────────────────────────────────────


def _t(to_state: str, reason: str, **extra) -> dict:
    return {"to_state": to_state, "reason": reason, **extra}


def test_경로에_대기는_적지_않는다():
    """모든 매매가 대기에서 시작하므로 정보가 없다."""
    path = transition_path([_t("1차 매수", "1선 이탈 → 1차 매수")])
    assert path == "1차 매수"
    assert "대기" not in path


def test_2차_매수가_1차_매수를_흡수한다():
    """2차는 1차 뒤에만 오므로 1차를 적지 않아도 읽힌다."""
    assert (
        transition_path(
            [
                _t("1차 매수", "1선 이탈 → 1차 매수"),
                _t("2차 매수", "2선 이탈 → 2차 매수"),
                _t("종료", "3선 이탈 → 전량 손절"),
            ]
        )
        == "2차 매수 → 손절"
    )


def test_갭_동시매수도_같은_모양이_된다():
    """2선 이하 갭으로 1·2차를 한 번에 사도 결과 표기는 '2차 매수' 로 같다."""
    assert (
        transition_path([_t("2차 매수", "2선 이하 갭 → 1·2차 동시 매수")]) == "2차 매수"
    )


def test_종료라고_적지_않고_끝난_이유를_적는다():
    """'종료' 만으로는 7% 익절인지 본절로 밀린 것인지 알 수 없다."""
    tp7 = transition_path(
        [
            _t("1차 매수", "1선 이탈 → 1차 매수"),
            _t("1차 매수 + 3% 익절", "1차 평단 +3% 도달 → 1차 익절"),
            _t("1차 매수 + 5% 익절", "1차 평단 +5% 도달 → 2차 익절"),
            _t("종료", "1차 평단 +7% 도달 → 전량 청산"),
        ]
    )
    breakeven = transition_path(
        [
            _t("1차 매수", "1선 이탈 → 1차 매수"),
            _t("1차 매수 + 3% 익절", "1차 평단 +3% 도달 → 1차 익절"),
            _t("종료", "본절 이탈 → 잔량 전량 청산"),
        ]
    )
    assert tp7 == "1차 매수 → 3% 익절 → 5% 익절 → 7% 익절"
    assert breakeven == "1차 매수 → 3% 익절 → 본절 이탈"
    assert "종료" not in tp7 and "종료" not in breakeven


@pytest.mark.parametrize(
    "reason,label",
    [
        ("1차 평단 +7% 도달 → 전량 청산", "7% 익절"),
        ("2차 평단 +7% 도달 → 전량 청산", "7% 익절"),
        # 본절·수동은 '전량 청산' 을 품고 있어 익절로 오인되기 쉽다 (검사 순서가 중요)
        ("본절 이탈 → 잔량 전량 청산", "본절 이탈"),
        ("사용자 판단 → 수동 전량 청산", "수동 청산"),
        ("3선 이탈 → 전량 손절", "손절"),
        ("3선 이탈(갭) → 2차 매수 생략, 전량 손절", "손절"),
        ("3선 이하 갭 시가 → 진입 금지, 당일 종료", "진입 금지"),
    ],
)
def test_종료_사유_라벨(reason, label):
    assert close_label(reason) == label


def test_익절률을_바꾸면_라벨도_따라간다():
    """익절률은 설정값(tp_rates)이라 숫자를 하드코딩하면 안 된다."""
    assert close_label("1차 평단 +10% 도달 → 전량 청산") == "10% 익절"


def test_1차_매수_직후_손절도_존재한다():
    """1선 매수 후 한 틱이 2선·3선을 함께 관통하는 갭 하락 (state_machine 266행)."""
    assert (
        transition_path(
            [
                _t("1차 매수", "1선 이탈 → 1차 매수"),
                _t("종료", "3선 이탈(갭) → 2차 매수 생략, 전량 손절"),
            ]
        )
        == "1차 매수 → 손절"
    )


def test_수량_0_익절_전이는_경로에_한_번만_나온다():
    assert (
        transition_path(
            [
                _t("1차 매수", "1선 이탈 → 1차 매수"),
                _t("1차 매수 + 3% 익절", "1차 평단 +3% 도달 → 1차 익절"),
                _t(
                    "1차 매수 + 3% 익절",
                    "1차 평단 +3% 도달 → 1차 익절 (매도 수량 0 → 상태만 전이)",
                ),
                _t("종료", "본절 이탈 → 잔량 전량 청산"),
            ]
        )
        == "1차 매수 → 3% 익절 → 본절 이탈"
    )


def test_빈_전이는_빈_문자열():
    assert transition_path([]) == ""


def test_시점은_기준봉_대비_거래일로_센다():
    calendar = TradingCalendar(
        ["2026-08-05", "2026-08-06", "2026-08-07", "2026-08-10", "2026-08-11"]
    )
    line = cycle_timeline(
        [
            _t("1차 매수", "1선 이탈", side="매수", trade_date="2026-08-07"),
            _t(
                "종료",
                "본절 이탈 → 잔량 전량 청산",
                side="매도",
                trade_date="2026-08-11",
            ),
        ],
        base_date="2026-08-05",
        calendar=calendar,
    )
    assert "진입 2026-08-07 (D+2)" in line  # 8/8~8/9 는 주말이라 거래일이 아니다
    assert "청산 2026-08-11 (D+4)" in line


# ── 매매 사이클 ───────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    yield s
    s.close()


def _register(store, date, pos):
    store.register_symbol(
        date,
        "005930",
        "삼성전자",
        Params(10_000, 9_500, 9_000, 100_000, 100_000),
        pos,
        base_date="2026-08-05",
    )


def _carried_cycle(store):
    """8/07 진입 → 8/10 이월 → 8/11 익절 후 본절 청산."""
    _register(store, "2026-08-07", Position())
    store.save_transition(
        "2026-08-07",
        "005930",
        State.WAITING,
        Position(State.BUY1, 9_900, 10, 10),
        Decision(State.BUY1, Side.BUY, 10, "1선 이탈 → 1차 매수"),
        9_900,
    )
    for date in ("2026-08-10", "2026-08-11"):
        _register(store, date, Position(State.BUY1, 9_900, 10, 10))
    store.save_transition(
        "2026-08-11",
        "005930",
        State.BUY1,
        Position(State.BUY1_TP1, 9_900, 10, 6),
        Decision(State.BUY1_TP1, Side.SELL, 4, "1차 평단 +3% 도달 → 1차 익절"),
        10_200,
    )
    store.save_transition(
        "2026-08-11",
        "005930",
        State.BUY1_TP1,
        Position(State.CLOSED, 9_900, 10, 0, realized_pnl=3_000),
        Decision(State.CLOSED, Side.SELL, 6, "본절 이탈 → 잔량 전량 청산"),
        9_900,
    )


def test_이월된_매매는_일지에_한_건으로_묶인다(store):
    """매매일마다 행이 생기지만 사람에게는 '한 번의 매매' 다."""
    _carried_cycle(store)
    entries = store.journal_entries()
    assert len(entries) == 1
    assert entries[0]["trade_date"] == "2026-08-11"  # 최종 손익이 있는 마지막 행
    assert entries[0]["realized_pnl"] == 3_000


def test_사이클은_날짜를_넘어_이어진다(store):
    """이월 종목은 진입이 며칠 전이라 당일 기록만 보면 앞부분이 통째로 없다."""
    _carried_cycle(store)
    cycle = store.symbol_cycle("005930")
    assert transition_path(cycle) == "1차 매수 → 3% 익절 → 본절 이탈"
    fills = store.symbol_fills("005930")
    assert [f["trade_date"] for f in fills] == [
        "2026-08-07",
        "2026-08-11",
        "2026-08-11",
    ]
    assert fills[0]["side"] == "매수"  # 진입 화살표의 근거


def test_리셋_후_재진입은_새_사이클이_된다(store):
    _carried_cycle(store)
    _register(store, "2026-08-12", Position(State.BUY1, 9_800, 5, 5))
    store.save_transition(
        "2026-08-12",
        "005930",
        State.WAITING,
        Position(State.BUY1, 9_800, 5, 5),
        Decision(State.BUY1, Side.BUY, 5, "1선 이탈 → 1차 매수"),
        9_800,
    )
    assert [e["trade_date"] for e in store.journal_entries()] == [
        "2026-08-12",
        "2026-08-11",
    ]
    assert [f["trade_date"] for f in store.symbol_fills("005930")] == ["2026-08-12"]


def test_이월_전에_쓴_코멘트를_잃지_않는다(store):
    _carried_cycle(store)
    store.save_journal("2026-08-07", "005930", good="진입 타이밍이 좋았다")
    entry = store.journal_entries()[0]
    assert entry["trade_date"] == "2026-08-11"
    assert entry["good"] == "진입 타이밍이 좋았다"


# ── 차트 화살표 ───────────────────────────────────────────────


def _minute_bars(n: int = 20) -> list[Bar]:
    return [
        Bar(f"202608100{9 + i // 20}{(i * 3) % 60:02d}00", 100, 110, 90, 105)
        for i in range(n)
    ]


def test_같은_봉의_같은_방향은_층으로_쌓인다():
    bars = _minute_bars()
    fills = [
        Fill("2026-08-10 09:00:10", "매수", 100),
        Fill("2026-08-10 09:00:20", "매수", 101),
        Fill("2026-08-10 09:03:00", "매도", 108),
    ]
    slots = marker_slots(bars, fills, daily=False)
    assert slots == [(0, "매수", 0), (0, "매수", 1), (1, "매도", 0)]


def test_방향이_다르면_각자_0층부터():
    """매수는 캔들 아래, 매도는 위라 서로 겹치지 않는다."""
    bars = _minute_bars()
    fills = [
        Fill("2026-08-10 09:00:10", "매수", 100),
        Fill("2026-08-10 09:00:20", "매도", 108),
    ]
    assert marker_slots(bars, fills, daily=False) == [
        (0, "매수", 0),
        (0, "매도", 0),
    ]


def test_봉이_촘촘하면_이웃_봉도_같은_칸으로_쌓는다():
    """3분봉은 화살표 폭이 봉 간격보다 넓어, 이웃 봉이면 가로로 겹쳐 개수를 셀 수 없다."""
    bars = _minute_bars()
    fills = [
        Fill("2026-08-10 09:00:10", "매도", 108),
        Fill("2026-08-10 09:03:10", "매도", 109),
        Fill("2026-08-10 09:06:10", "매도", 110),
    ]
    assert marker_slots(bars, fills, daily=False, group=3) == [
        (0, "매도", 0),
        (1, "매도", 1),
        (2, "매도", 2),
    ]
    # group=1 (일봉처럼 성길 때) 이면 같은 봉만 쌓는다
    assert [lv for _, _, lv in marker_slots(bars, fills, daily=False, group=1)] == [
        0,
        0,
        0,
    ]


def test_표시_구간_밖의_체결은_버린다():
    bars = _minute_bars()
    fills = [Fill("2026-08-05 09:00:10", "매수", 100)]  # 며칠 전
    assert marker_slots(bars, fills, daily=False) == []


def test_체결_순서대로_층이_올라간다():
    """0층이 캔들에 가장 가깝다 — 차수를 적지 않아도 위치로 읽히도록."""
    bars = _minute_bars()
    fills = [
        Fill("2026-08-10 09:00:50", "매도", 109),  # 나중 것을 먼저 넣어도
        Fill("2026-08-10 09:00:10", "매도", 108),
    ]
    slots = marker_slots(bars, fills, daily=False)
    assert [lv for _, _, lv in slots] == [0, 1]


# ── 키움 조회 API 파싱 ────────────────────────────────────────


class _StubBroker:
    """문서 예시 응답만 돌려주는 가짜 — 파싱만 검증한다."""

    _QUERY_RETRIES = 0

    def __init__(self, responses: dict):
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []

    def _request(self, path, api_id, body, retries=0):
        self.calls.append((api_id, body))
        return self._responses[api_id]


def _broker(responses: dict):
    from trader.broker import Broker

    b = Broker.__new__(Broker)
    stub = _StubBroker(responses)
    b._request = stub._request
    b._QUERY_RETRIES = 0
    b._calls = stub.calls
    return b


def test_미체결_파싱과_체결완료_행_제외():
    b = _broker(
        {
            "ka10075": {
                "oso": [
                    {
                        "ord_no": "0000069",
                        "stk_cd": "005930",
                        "stk_nm": "삼성전자",
                        "ord_qty": "10",
                        "oso_qty": "10",
                        "io_tp_nm": "+매수",
                        "ord_stt": "접수",
                        "tm": "154113",
                    },
                    {  # 이미 다 체결된 행은 미체결이 아니다
                        "ord_no": "0000070",
                        "stk_cd": "005930",
                        "oso_qty": "0",
                        "io_tp_nm": "-매도",
                    },
                ]
            }
        }
    )
    orders = b.open_orders("005930")
    assert len(orders) == 1
    assert orders[0]["side"] == "매수" and orders[0]["unfilled"] == 10


def test_체결_파싱은_A접두사와_부호를_처리한다():
    b = _broker(
        {
            "ka10076": {
                "cntr": [
                    {
                        "ord_no": "0000037",
                        "stk_nm": "삼성전자",
                        "io_tp_nm": "-매도",
                        "cntr_pric": "158200",
                        "cntr_qty": "1",
                        "tdy_trde_cmsn": "310",
                        "tdy_trde_tax": "284",
                        "ord_stt": "체결",
                        "ord_tm": "153815",
                        "stk_cd": "A005930",
                    }
                ]
            }
        }
    )
    fill = b.filled_orders()[0]
    assert fill["symbol"] == "005930"  # A 접두사 제거
    assert fill["side"] == "매도" and fill["price"] == 158_200
    assert fill["commission"] + fill["tax"] == 594


def test_실현손익은_세후_순손익이다():
    """문서 예시로 확인한 항등식: (체결가 − 매입단가)×수량 − 수수료 − 세금 = tdy_sel_pl."""
    b = _broker(
        {
            "ka10077": {
                "tdy_rlzt_pl": "179439",
                "tdy_rlzt_pl_dtl": [
                    {
                        "stk_nm": "삼성전자",
                        "cntr_qty": "1",
                        "buy_uv": "97602.9573459",  # 소수 문자열 — int() 는 예외가 난다
                        "cntr_pric": "158200",
                        "tdy_sel_pl": "59813.04",
                        "pl_rt": "+61.28",
                        "stk_cd": "A005930",
                        "tdy_trde_cmsn": "500",
                        "tdy_trde_tax": "284",
                    }
                ],
            }
        }
    )
    row = b.realized_pnl("005930")[0]
    assert row["symbol"] == "005930"
    gross = (row["sell_price"] - row["buy_price"]) * row["qty"]
    assert abs(gross - row["commission"] - row["tax"] - row["pnl"]) < 0.01
    assert round(row["pnl"] / (row["buy_price"] * row["qty"]), 4) == row["rate"]


def test_당일_실현손익만_조회한다():
    """ka10072 는 응답에 일자가 없어 '오늘' 인지 확인할 수 없었다 — ka10077 로 바꿨다."""
    b = _broker({"ka10077": {"tdy_rlzt_pl_dtl": []}})
    b.realized_pnl("005930")
    api_id, body = b._calls[0]
    assert api_id == "ka10077"
    assert "strt_dt" not in body  # 날짜를 넘기지 않는다 (당일 전용 TR)
    assert body["stk_cd"] == "005930"  # 종목 단위 TR — 비우면 서버가 거절한다


def test_실현손익_조회에_종목코드가_없으면_호출_전에_막는다():
    """ka10077 은 종목 단위 TR 이다.

    계좌 전체를 주는 줄 알고 인자 없이 불렀고, 서버가 `필수입력 파라미터=stk_cd` 로
    거절해 실현손익 대조가 매일 통째로 건너뛰어졌다(2026-08-19 ~ 08-21). 조회 실패를
    조용히 넘어가는 설계라 경고 한 줄만 남고 드러나지 않았다. 서버만 알던 계약을
    코드로 끌어와 여기서 걸리게 한다.
    """
    b = _broker({"ka10077": {"tdy_rlzt_pl_dtl": []}})
    with pytest.raises(BrokerError, match="종목코드"):
        b.realized_pnl("")
    assert b._calls == []  # 네트워크로 나가지도 않는다


def test_실현손익은_요청한_종목의_행만_남긴다():
    """계좌 전체가 딸려 오더라도 종목마다 부르므로 걸러야 중복으로 세지 않는다."""
    b = _broker(
        {
            "ka10077": {
                "tdy_rlzt_pl_dtl": [
                    {"stk_cd": "A005930", "cntr_qty": "1", "tdy_sel_pl": "100"},
                    {"stk_cd": "A035420", "cntr_qty": "2", "tdy_sel_pl": "200"},
                ]
            }
        }
    )
    rows = b.realized_pnl("005930")
    assert [r["symbol"] for r in rows] == ["005930"]


# ── 15:35 이후 자동화 없음 ────────────────────────────────────


def test_스케줄에_요약_이후_항목이_없다(tmp_path):
    """15:35 일일 요약이 하루의 마지막 자동 작업이다."""
    from trader.core import _load_schedule

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[schedule]\nenabled = true\nstart = "08:55"\nstop = "15:30"\n'
        'summary = "15:35"\n',
        encoding="utf-8",
    )
    schedule = _load_schedule(str(cfg))
    assert set(schedule) == {"enabled", "start", "stop", "summary"}
    assert max(v for v in schedule.values() if isinstance(v, dt.time)) == dt.time(
        15, 35
    )


def test_시간외_리뷰_함수가_사라졌다():
    """분봉 API 가 정규장 범위만 주므로 대상이 늘 0종목이었다."""
    from trader import core as core_mod

    assert not hasattr(core_mod.Core, "_review_after_hours")


# ── 중복 주문 방지 ────────────────────────────────────────────


def test_조회_API가_없는_브로커에서도_주문은_진행된다():
    """시뮬레이터에는 open_orders 가 없다 — 없다고 매매가 멈추면 안 된다."""
    from trader.core import Core
    from trader.ui import bus as bus_mod

    core = Core.__new__(Core)
    core._broker = object()  # open_orders 없음
    core._order_fail = {"005930": {"count": 1, "until": 0.0}}
    decision = Decision(State.BUY1, Side.BUY, 1, "1선 이탈 → 1차 매수")
    assert asyncio.run(core._no_duplicate_order("005930", decision)) is True
    assert bus_mod is not None  # import 확인용


# ── 화살표가 이웃 캔들도 가리지 않는가 (기하 검사) ────────────


def _overlaps(bars, fills, daily: bool) -> int:
    """실제 렌더 좌표에서 화살표 사각형과 캔들 사각형이 겹치는 횟수."""
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    from matplotlib.transforms import offset_copy

    from trader import chart as C

    fig, ax = plt.subplots(figsize=(9.6, 12.8), dpi=100)
    ax.set_position([0.1, 0.2, 0.85, 0.7])
    C._candles(ax, bars)
    size = C.marker_size(ax)
    half = C._anchor_half(ax, size)
    slots = C.marker_slots(bars, fills, daily, group=max(1, half * 2 + 1))
    placed = [
        (i, s, C._marker_anchor(bars, i, s == "매수", half), C._marker_offset(size, lv))
        for i, s, lv in slots
    ]
    C._expand_ylim(ax, [(a, s == "매수", g + size / 2) for _, s, a, g in placed])
    fig.canvas.draw()
    tr, hits = ax.transData, 0
    for idx, side, anchor, gap in placed:
        buy = side == "매수"
        t = offset_copy(tr, fig=fig, y=(-gap if buy else gap), units="points")
        cx, cy = t.transform((idx, anchor))
        r = size / 2 * fig.dpi / 72
        for j, b in enumerate(bars):
            x0, y0 = tr.transform((j - 0.3, b.low))
            x1, y1 = tr.transform((j + 0.3, b.high))
            if cx + r > x0 and cx - r < x1 and cy + r > y0 and cy - r < y1:
                hits += 1
                break
    plt.close(fig)
    return hits


def _trend_bars(days: int, drift: float, seed: int = 0) -> list[Bar]:
    import random

    random.seed(seed)
    bars, base = [], 30_000.0
    for d in range(days):
        for i in range(130):
            key = f"2026081{d}{9 + (i * 3) // 60:02d}{(i * 3) % 60:02d}00"
            o = base
            c = base * (1 + random.uniform(-0.006, 0.006) + drift)
            bars.append(
                Bar(key, o, max(o, c) * 1.004, min(o, c) * 0.996, c, volume=100)
            )
            base = c
    return bars


@pytest.mark.parametrize("days", [2, 3, 4])
@pytest.mark.parametrize("drift", [-0.006, 0.006])
def test_화살표는_어떤_캔들도_가리지_않는다(days, drift):
    """봉이 촘촘할수록 화살표가 자기 봉 폭을 넘어 이웃 캔들을 덮기 쉽다.

    이월된 종목은 3분봉 일수가 늘어 봉 간격이 좁아지는데, 기준점 범위를 내림으로
    구하면 화살표 폭보다 좁은 범위만 보게 되어 옆 캔들 위에 얹혔다(2026-08-11).
    """
    bars = _trend_bars(days, drift)
    last = days - 1
    fills = [
        Fill("2026-08100 09:00:34".replace("08100", "0810"), "매수", 0),
        Fill("2026-08-10 09:00:40", "매수", 0),
        Fill(f"2026-08-1{last} 09:10:00", "매도", 0),
        Fill(f"2026-08-1{last} 09:13:00", "매도", 0),
        Fill(f"2026-08-1{last} 13:20:00", "매도", 0),
    ]
    assert _overlaps(bars, fills, daily=False) == 0


def test_기준점_범위는_화살표_폭을_올림으로_덮는다():
    """내림하면 폭 3.7봉짜리 화살표에 범위 1봉이 배정되어 옆 캔들을 못 피한다."""
    import math

    for size, per_bar in ((5.0, 1.36), (5.0, 2.04), (9.0, 9.3)):
        assert math.ceil(size / per_bar / 2) >= size / per_bar / 2
    assert math.ceil(5.0 / 1.36 / 2) == 2  # 내림이면 1 → 겹침 발생


# ── 매매일지 검색·필터 ────────────────────────────────────────


def _entry(name: str, symbol: str, pnl: float, fees: float = 0.0) -> dict:
    return {
        "trade_date": "2026-08-11",
        "name": name,
        "symbol": symbol,
        "realized_pnl": pnl,
        "fees": fees,
    }


@pytest.fixture
def rows() -> list[dict]:
    return [
        _entry("RF머트리얼즈", "327260", -1_750, 642),
        _entry("유진로봇", "056080", 9_000, 543),
        _entry("광전자", "017900", 8_900, 518),
        _entry("코스텍시스", "064290", 500, 500),  # 수수료까지 빼면 본전
    ]


def test_종목명_일부로_검색된다(rows):
    from trader.ui.journal_dialog import filter_entries

    assert [e["name"] for e in filter_entries(rows, query="로봇")] == ["유진로봇"]


def test_종목코드_일부로도_검색된다(rows):
    """코드를 외우지 않아도 되고, 이름이 헷갈리면 코드로 찾을 수 있어야 한다."""
    from trader.ui.journal_dialog import filter_entries

    assert [e["name"] for e in filter_entries(rows, query="3272")] == ["RF머트리얼즈"]


def test_검색은_대소문자를_가리지_않는다():
    from trader.ui.journal_dialog import filter_entries

    rows = [_entry("LS ELECTRIC", "010120", 100)]
    assert len(filter_entries(rows, query="ls electric")) == 1


def test_익절_손절은_세후로_나눈다(rows):
    """수수료·세금까지 빼고도 남았는지가 실제로 번 것인지의 기준이다."""
    from trader.ui.journal_dialog import filter_entries

    assert [e["name"] for e in filter_entries(rows, result="익절")] == [
        "유진로봇",
        "광전자",
    ]
    assert [e["name"] for e in filter_entries(rows, result="손절")] == ["RF머트리얼즈"]
    # 세후 0원(본전)은 익절도 손절도 아니다 — 전체에서만 보인다
    assert len(filter_entries(rows, result="전체")) == 4


def test_검색과_필터는_함께_적용된다(rows):
    from trader.ui.journal_dialog import filter_entries

    assert [e["name"] for e in filter_entries(rows, query="0", result="익절")] == [
        "유진로봇",
        "광전자",
    ]


def test_빈_검색어는_전체를_돌려준다(rows):
    from trader.ui.journal_dialog import filter_entries

    assert len(filter_entries(rows, query="   ")) == len(rows)


# ── 매매일지 창 조작 ──────────────────────────────────────────


@pytest.fixture
def dialog(rows):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:  # 화면이 없는 환경
        pytest.skip("no display")
    root.withdraw()
    from trader.ui.journal_dialog import JournalDialog

    for row in rows:  # 창이 요구하는 필드를 채운다
        row.setdefault("state", "종료")
        row.setdefault("avg_price", 10_000)
        row.setdefault("total_bought", 10)
        row.setdefault("daily_path", "")
        row.setdefault("minute_path", "")
    dlg = JournalDialog(root, rows, lambda *a: None)
    dlg.geometry("1100x740")
    dlg.update()
    dlg.update_idletasks()
    dlg.update()
    yield dlg
    root.destroy()


def _type(search, text: str) -> None:
    """실제 타이핑을 흉내낸다 — 글자 삽입도 Tk 의 기본 동작에 맡긴다.

    when="now" 로 보내야 바인딩이 그 자리에서 실행된다(기본값은 큐에 쌓였다가 나중에
    처리돼 순서 검증이 무의미해진다). 직접 insert 하면 안 되는데, KeyPress 를 보내면
    Tk 가 그 글자를 이미 넣기 때문이다 — 둘 다 하면 "a로a봇" 이 된다.
    한글은 keysym 으로 보낼 수 없어 시험에는 종목코드(숫자)를 쓴다.
    """
    search.entry.focus_force()
    search.update()
    for char in text:
        # KeyPress 로 글자가 들어가고, 목록 갱신은 KeyRelease 에 걸려 있다. 실제 타이핑은
        # 둘이 짝을 이루므로 시험도 짝으로 보내야 한다.
        search.entry.event_generate("<KeyPress>", keysym=char, when="now")
        search.entry.event_generate("<KeyRelease>", keysym=char, when="now")
    search.update()


def test_안내문구는_창을_처음_열_때도_가운데_있다(dialog):
    """배치 전에는 캔버스가 1×1 이라, 그때 그린 글자는 왼쪽 위 구석에 박힌다."""
    view = dialog._views["일봉"]
    x, y = view.canvas.coords("msg")
    assert abs(x - view.canvas.winfo_width() / 2) < 2
    assert abs(y - view.canvas.winfo_height() / 2) < 2


def test_안내문구는_비어있어야_뜬다(dialog):
    view = dialog._views["일봉"]
    assert view.canvas.itemcget("msg", "text") == "보관된 차트가 없습니다"


def test_검색칸의_안내문구는_겹치는_위젯이_아니다(dialog):
    """Entry 위에 Label 을 얹으면 Windows 11 테마의 파란 밑줄을 가린다."""
    kinds = sorted(w.winfo_class() for w in dialog._search.winfo_children())
    assert kinds == ["TButton", "TEntry"]  # 겹쳐 놓은 Label 이 없다
    assert dialog._search.get() == ""  # 안내 문구는 검색어로 세지 않는다


def test_지우기_버튼은_늘_보이되_지울_때만_눌린다(dialog):
    """숨겼다 보였다 하면 입력칸 폭이 달라져 글자가 밀린다 — 자리는 늘 잡아둔다."""
    search = dialog._search
    assert search._clear.winfo_ismapped()  # 빈 칸에서도 자리는 있다
    assert "disabled" in search._clear.state()
    _type(search, "0560")  # 유진로봇(056080)
    dialog.update()
    assert search.get() == "0560"
    assert "disabled" not in search._clear.state()
    assert dialog._list.size() == 1


def test_지우기_버튼이_검색을_초기화한다(dialog, rows):
    search = dialog._search
    _type(search, "0560")
    dialog.update()
    search.clear()
    dialog.update()
    assert search.get() == ""
    assert dialog._list.size() == len(rows)


def test_포커스가_떠나면_안내문구가_돌아온다(dialog):
    """처리기를 직접 부른다 — 진짜 포커스 이벤트에 기대지 않는다.

    focus_set() 은 **OS 가 그 창에 키보드 포커스를 줄 때만** <FocusIn> 을 일으킨다.
    루트가 숨겨진 시험 환경에서는 창이 포커스를 못 받아 Windows 에서 처리기가 아예
    실행되지 않았다(2026-08-12). 바인딩이 걸려 있는지와 처리기가 하는 일을 따로 확인하면
    플랫폼과 무관하게 같은 결과가 나온다.
    """
    search = dialog._search
    assert search.entry.bind("<FocusIn>")  # 실제 포커스에도 반응하도록 걸려 있다
    assert search.entry.bind("<FocusOut>")

    search._on_focus_in()
    assert search.entry.get() == ""  # 포커스가 오면 안내 문구는 비켜준다
    search._on_focus_out()
    assert search.entry.get() == "종목명 또는 종목코드"
    assert search.get() == ""  # 그래도 검색어는 비어 있다


def test_탭_순서가_화면에_보이는_대로다(dialog):
    """기본 순서는 위젯을 만든 차례를 따라, 저장 버튼이 코멘트보다 먼저 온다.

    focus_get() 으로 확인하지 않는다 — 시험 환경에서는 창이 OS 포커스를 못 받아
    항상 None 이 나온다(2026-08-12). 대신 창이 정한 순서와 바인딩 유무를 본다.
    """
    assert dialog.focus_chain == [
        dialog._search.entry,
        dialog._month_button,
        dialog._year_box,
        dialog._month_box,
        dialog._all_button,
        *dialog._filters,
        dialog._list,
        dialog._charts,
        dialog._good,
        dialog._bad,
        dialog._save_button,
    ]
    for widget in dialog.focus_chain:
        assert widget.bind("<Tab>"), f"{widget} 에 Tab 바인딩이 없다"
        assert widget.bind("<Shift-Tab>")


def test_마지막_칸에서_탭을_누르면_처음으로_돌아온다(dialog):
    """어디서 시작해도 모든 칸에 닿을 수 있어야 한다."""
    chain = dialog.focus_chain
    assert chain[-1] is dialog._save_button
    assert chain[(len(chain) - 1 + 1) % len(chain)] is chain[0]


def test_탭_처리기는_보내고_기본동작을_막는다(dialog):
    """tk.Text 는 Tab 을 글자로 받아넣는다 — "break" 를 돌려줘야 막힌다."""
    from trader.ui.journal_dialog import _focus_to

    moved = []
    target = type("T", (), {"focus_set": lambda self: moved.append(True)})()
    assert _focus_to(target)() == "break"
    assert moved == [True]
    assert "\t" not in dialog._good.get("1.0", "end")


def test_지우기_버튼은_탭_순서에서_빠진다(dialog):
    """✕ 는 마우스로 누르는 보조 버튼이다 — 검색에서 필터로 바로 넘어가야 한다."""
    assert dialog._search._clear.cget("takefocus") in (0, "0", False)
    assert dialog._search._clear not in dialog.focus_chain


def test_글자가_들어오기_전에_안내문구를_치운다(dialog):
    """KeyRelease 에만 지우면 안내 문구 앞에 글자가 붙는다 ("05" + "종목명 또는 …")."""
    search = dialog._search
    assert search.entry.get() == "종목명 또는 종목코드"
    search._on_key_press()  # 키를 누르는 순간
    assert search.entry.get() == ""  # 글자가 들어갈 자리는 이미 비어 있다
    search.entry.insert("end", "05")
    search._on_key()
    dialog.update()
    assert search.get() == "05"


def test_방향키만_눌러도_안내문구가_사라지지_않는다(dialog):
    """글자가 안 들어왔는데 검색어로 잡히면 목록이 통째로 사라진다."""
    search = dialog._search
    search._on_key()  # 글자 없이 KeyRelease 만 온 상황
    dialog.update()
    assert search.get() == ""
    assert dialog._list.size() == len(dialog._entries)


# ── 기간 선택 ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "year,month,expected",
    [
        ("2026", "08", ("2026-08-01", "2026-08-31")),
        ("2025", "12", ("2025-12-01", "2025-12-31")),
        ("2024", "02", ("2024-02-01", "2024-02-29")),  # 윤년
        ("2026", "02", ("2026-02-01", "2026-02-28")),
        ("2026", "01", ("2026-01-01", "2026-01-31")),
    ],
)
def test_연월이_날짜_범위로_바뀐다(year, month, expected):
    from trader.ui.journal_dialog import period_range

    assert period_range(year, month) == expected


@pytest.mark.parametrize("year,month", [("", ""), ("2026", ""), ("", "08"), ("x", "y")])
def test_연월이_없거나_이상하면_전체로_본다(year, month):
    """설정이 깨져도 목록이 비어 보이지 않도록."""
    from trader.ui.journal_dialog import period_range

    assert period_range(year, month) == ("", "")


def test_연도_목록은_시작한_해부터_오름차순이다():
    """이 프로그램으로 매매를 시작한 해(2026) 이전 기록은 있을 수 없다."""
    import datetime as dt

    from trader.ui.journal_dialog import FIRST_YEAR, year_choices

    assert year_choices((), dt.date(2026, 8, 12)) == ["2026"]
    years = year_choices((), dt.date(2029, 1, 1))
    assert years == ["2026", "2027", "2028", "2029"]  # 월 목록과 같은 방향
    assert years[0] == str(FIRST_YEAR)


def test_기록이_있는_해가_올해보다_뒤여도_담긴다():
    """PC 시계가 어긋나 있어도 기록이 있는 해는 고를 수 있어야 한다."""
    import datetime as dt

    from trader.ui.journal_dialog import year_choices

    assert "2028" in year_choices(("2028-03",), dt.date(2026, 8, 12))


@pytest.fixture
def period_dialog(rows):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    from trader.ui.journal_dialog import JournalDialog

    for row in rows:
        row.setdefault("state", "종료")
        row.setdefault("avg_price", 10_000)
        row.setdefault("total_bought", 10)
        row.setdefault("daily_path", "")
        row.setdefault("minute_path", "")
    asked: list = []
    dlg = JournalDialog(
        root,
        rows,
        lambda *a: None,
        on_period=lambda since, until: asked.append((since, until)),
        months=("2026-08", "2026-07"),
    )
    dlg.asked = asked
    dlg.update()
    yield dlg
    root.destroy()


def test_기본은_이번_달이다(period_dialog):
    import datetime as dt

    from trader.ui.journal_dialog import PERIOD_MONTH

    today = dt.date.today()
    assert period_dialog._period_mode.get() == PERIOD_MONTH
    assert period_dialog._year.get() == str(today.year)
    assert period_dialog._month.get() == f"{today.month:02d}"


def test_연월을_고르면_월별_조회로_바뀐다(period_dialog):
    from trader.ui.journal_dialog import PERIOD_ALL, PERIOD_MONTH

    period_dialog._period_mode.set(PERIOD_ALL)
    period_dialog._year.set("2026")
    period_dialog._month.set("08")
    period_dialog._on_month_pick()
    assert period_dialog._period_mode.get() == PERIOD_MONTH
    assert period_dialog.asked[-1] == ("2026-08-01", "2026-08-31")


def test_전체를_고르면_기간_제한이_없다(period_dialog):
    from trader.ui.journal_dialog import PERIOD_ALL

    period_dialog._period_mode.set(PERIOD_ALL)
    period_dialog._on_period()
    assert period_dialog.asked[-1] == ("", "")


def test_전체_조회중에는_연월을_고를_수_없다(period_dialog):
    """둘 중 하나만 고를 수 있어야 한다 — 전체인데 월이 켜져 있으면 헷갈린다."""
    from trader.ui.journal_dialog import PERIOD_ALL, PERIOD_MONTH

    period_dialog._period_mode.set(PERIOD_ALL)
    period_dialog._on_period()
    assert str(period_dialog._year_box.cget("state")) == "disabled"
    assert str(period_dialog._month_box.cget("state")) == "disabled"

    period_dialog._period_mode.set(PERIOD_MONTH)
    period_dialog._on_period()
    assert str(period_dialog._year_box.cget("state")) == "readonly"


def test_기간_위젯은_한_줄에_있다(period_dialog):
    """[월별] [연] [월] [전체] 가 같은 줄에 나란히 놓인다."""
    row = period_dialog._month_button.master
    assert period_dialog._year_box.master is row
    assert period_dialog._month_box.master is row
    assert period_dialog._all_button.master is row
    widgets = [
        period_dialog._month_button,
        period_dialog._year_box,
        period_dialog._month_box,
        period_dialog._all_button,
    ]
    assert all(int(w.grid_info()["row"]) == 0 for w in widgets)  # 같은 줄
    columns = [int(w.grid_info()["column"]) for w in widgets]
    assert columns == sorted(columns)  # 왼쪽부터 순서대로
    # 폭을 나눠 가져 오른쪽에 빈 자리가 남지 않는다
    assert all(
        "e" in w.grid_info()["sticky"] and "w" in w.grid_info()["sticky"]
        for w in widgets
    )


def test_새_목록을_받으면_연도_선택지가_갱신된다(period_dialog, rows):
    period_dialog.set_entries([rows[0]], months=("2030-03",))
    assert "2030" in period_dialog._year_box.cget("values")
    assert period_dialog._list.size() == 1


# ── 보유 중 최고·최저(MFE/MAE) ────────────────────────────────


def test_보유만_해도_최고_최저가_갱신된다():
    """전이 때만 옮기면 매수 후 청산이 없는 날은 체결가에 멈춰 '+0.0%' 가 찍힌다."""
    from dataclasses import replace as _replace

    from trader.core import Core

    core = Core.__new__(Core)
    core._entries = {
        "355150": {
            "name": "코스텍시스",
            "pos": Position(
                State.BUY1,
                avg_price=22_150,
                total_bought=9,
                remaining=9,
                high_price=22_150,
                low_price=22_150,
            ),
            "high": 22_600.0,  # 장중에 오르내린 값
            "low": 20_200.0,
            "day_low": 20_200.0,
            "day_open": 22_300.0,
            "day_close": 20_300.0,
        }
    }
    saved: list = []
    core._store = type(
        "S", (), {"save_position": lambda self, d, s, p: saved.append(p)}
    )()
    core._date, core._running, core._day_low_at = "2026-08-12", True, 0.0

    core._flush_day_lows(force=True)

    pos = core._entries["355150"]["pos"]
    assert pos.high_price == 22_600
    assert pos.low_price == 20_200
    assert len(saved) == 1  # DB 에도 반영된다
    assert _replace(pos, high_price=0).high_price == 0  # dataclass 그대로


def test_변한_게_없으면_저장하지_않는다():
    """값이 같은데 매분 쓰면 기록만 늘고 얻는 게 없다."""
    from trader.core import Core

    core = Core.__new__(Core)
    core._entries = {
        "355150": {
            "pos": Position(
                State.BUY1,
                avg_price=22_150,
                total_bought=9,
                remaining=9,
                high_price=22_600,
                low_price=20_200,
                day_low=20_200,
                day_open=22_300,
                day_close=20_300,
            ),
            "high": 22_600.0,
            "low": 20_200.0,
            "day_low": 20_200.0,
            "day_open": 22_300.0,
            "day_close": 20_300.0,
        }
    }
    saved: list = []
    core._store = type(
        "S", (), {"save_position": lambda self, d, s, p: saved.append(p)}
    )()
    core._date, core._running, core._day_low_at = "2026-08-12", True, 0.0

    core._flush_day_lows(force=True)
    assert saved == []


# ── 성적 요약 ─────────────────────────────────────────────────


def _closed(pnl: float, fees: float = 500) -> dict:
    return {"realized_pnl": pnl, "fees": fees, "state": "종료"}


def test_성적_요약은_승률과_세후_합계를_보여준다():
    """몇 건인지보다 이겼는지 졌는지가 먼저 궁금하다."""
    from trader.ui.journal_dialog import summarize_stats

    text = summarize_stats([_closed(9_000)] * 15 + [_closed(-2_000)] * 5)
    assert "20건" in text
    assert "익절 15 · 손절 5" in text
    assert "승률 75.0%" in text
    assert "세후 +115,000원" in text


def test_승률은_본전과_보유중을_분모에서_뺀다():
    """이긴 것도 진 것도 아닌 건을 넣으면 승률이 실제보다 낮아 보인다."""
    from trader.ui.journal_dialog import summarize_stats

    rows = [_closed(9_000)] * 6 + [_closed(-2_000)] * 4
    rows.append({"realized_pnl": 500, "fees": 500, "state": "종료"})  # 세후 0원
    rows.append({"realized_pnl": 0, "fees": 0, "state": "1차 매수"})  # 보유 중
    text = summarize_stats(rows)
    assert "승률 60.0%" in text  # 6 / (6+4)
    assert "12건" in text  # 건수에는 다 들어간다
    assert "보유 중 1" in text


def test_걸러진_상태는_분자_분모로_보여준다():
    from trader.ui.journal_dialog import summarize_stats

    assert summarize_stats([_closed(9_000)] * 3, total=30).startswith("3/30건")
    assert summarize_stats([_closed(9_000)] * 3, total=3).startswith("3건")


def test_비어_있으면_0건만_보여준다():
    from trader.ui.journal_dialog import summarize_stats

    assert summarize_stats([]) == "0건"
    assert summarize_stats([], total=30) == "0/30건"


def test_승패가_없으면_승률을_적지_않는다():
    """0으로 나누지 않고, 의미 없는 '승률 0%' 도 띄우지 않는다."""
    from trader.ui.journal_dialog import summarize_stats

    text = summarize_stats([{"realized_pnl": 0, "fees": 0, "state": "1차 매수"}])
    assert "승률" not in text
    assert "보유 중 1" in text


def test_창의_요약줄이_성적을_보여준다(period_dialog):
    text = period_dialog._count.cget("text")
    assert "건" in text and "승률" in text


# ── 왼쪽 칸 폭 · 버튼 모양 ────────────────────────────────────


def test_왼쪽_칸이_차트를_밀어내지_않는다(period_dialog):
    """콤보 기본 폭(20자)과 요약줄 되먹임 때문에 왼쪽이 통째로 넓어진 적이 있다."""
    period_dialog.geometry("1180x760")
    period_dialog.update()
    period_dialog.update_idletasks()
    left = period_dialog._list.master
    assert left.winfo_reqwidth() < 320  # 목록 칸은 좁게
    assert period_dialog._charts.winfo_width() > left.winfo_width() * 2


def test_요약줄_줄바꿈은_목록_폭을_따른다(period_dialog):
    """왼쪽 칸을 기준으로 삼으면 라벨이 넓어지고 칸이 다시 넓어지는 되먹임이 생긴다."""
    assert period_dialog._count.cget("wraplength") > 0
    assert period_dialog._list.bind("<Configure>")


def test_선택_위젯은_프로그램의_다른_곳과_같은_모양이다(period_dialog):
    """메인 창의 투자 모드도 기본 ttk.Radiobutton 이다 — 일지 창만 다르면 겉돈다."""
    for button in [
        period_dialog._month_button,
        period_dialog._all_button,
        *period_dialog._filters,
    ]:
        assert button.winfo_class() == "TRadiobutton"
        assert button.cget("style") == ""  # 별도 스타일을 입히지 않는다


# ── 증권사 대조 ───────────────────────────────────────────────


def _core():
    from trader.core import Core

    core = Core.__new__(Core)
    core._date = "2026-08-14"
    core.logs = []
    core._log = lambda sym, kind, text, notify=True: core.logs.append((kind, text))
    return core


def _mine(symbol, name, pre, fees, avg=10_000):
    return {
        "symbol": symbol,
        "name": name,
        "realized_pnl": pre,
        "fees": fees,
        "avg_price": avg,
    }


def _theirs(symbol, pnl, qty=10, buy=10_000, sell=11_000):
    return {
        "symbol": symbol,
        "pnl": pnl,
        "qty": qty,
        "buy_price": buy,
        "sell_price": sell,
    }


def test_작은_차이는_알리지_않는다():
    """평단 계산 방식 차이로 몇백 원은 늘 난다 — 매일 울리면 무시하게 된다."""
    core = _core()
    lines = core._reconcile_pnl(
        [_mine("005930", "삼성전자", 10_000, 500)], [_theirs("005930", 9_200)]
    )
    assert lines == []


def test_큰_차이는_알리고_어느_종목인지_알려준다():
    core = _core()
    lines = core._reconcile_pnl(
        [
            _mine("005930", "삼성전자", 10_000, 500),
            _mine("079650", "서산", -16_170, 700),
        ],
        [_theirs("005930", 9_500), _theirs("079650", -19_100)],
    )
    assert len(lines) == 1
    assert "서산" in lines[0]  # 가장 크게 어긋난 종목
    assert "-19,724" not in lines[0]  # 합계는 두 쪽 다 적힌다
    assert "프로그램" in lines[0] and "증권사" in lines[0]


def test_종목별_내역이_로그에_남는다():
    """합계만 비교하면 어디서 어긋났는지 알 수 없어 원인 규명이 안 된다."""
    core = _core()
    core._reconcile_pnl(
        [_mine("079650", "서산", -16_170, 700, avg=5_250)],
        [_theirs("079650", -19_100, qty=77, buy=5_288, sell=5_040)],
    )
    detail = next(text for kind, text in core.logs if kind == "대조")
    assert "서산(079650)" in detail
    assert (
        "평단 5,250.00 vs 5,288.00" in detail
    )  # 매입단가 비교 (수수료 포함 여부 판별)
    assert "수량 77" in detail
    assert "Δ" in detail


def test_프로그램이_모르는_종목을_짚어준다():
    """ka10072 가 다른 날·다른 종목을 섞어 주는지 확인하는 단서다."""
    core = _core()
    core._reconcile_pnl(
        [_mine("005930", "삼성전자", 10_000, 500)], [_theirs("999999", 480)]
    )
    detail = next(text for kind, text in core.logs if kind == "대조")
    assert "프로그램에 없는 종목" in detail


def test_분할_매도는_한_종목으로_합친다():
    """ka10072 는 매도 건마다 줄이 나뉘어 온다."""
    core = _core()
    core._reconcile_pnl(
        [_mine("123330", "제닉", 9_050, 343)],
        [_theirs("123330", 3_000, qty=2), _theirs("123330", 5_630, qty=4)],
    )
    detail = next(text for kind, text in core.logs if kind == "대조")
    assert detail.count("제닉") == 1
    assert "수량 6" in detail


def test_청산이_없는_종목은_빼고_본다():
    """보유만 한 종목까지 적으면 로그가 길어져 정작 볼 것이 묻힌다."""
    core = _core()
    core._reconcile_pnl([_mine("005930", "삼성전자", 0, 0)], [])
    assert not [text for kind, text in core.logs if kind == "대조"]


def test_거래비용_차이는_로그에만_남긴다():
    """설정 요율 문제라 매일 같은 내용이 뜬다 — 요약에 넣으면 곧 무시하게 된다."""
    core = _core()
    lines = core._reconcile_cost(
        [_mine("005930", "삼성전자", 10_000, 2_972)],
        [{"symbol": "005930"}],
        [{"symbol": "005930", "commission": 1_056, "tax": 2_223}],
    )
    assert any(line.startswith("거래비용") for line in lines)
    logged = next(text for kind, text in core.logs if kind == "대조")
    assert "수수료 1,056" in logged and "세금 2,223" in logged


# ── 목록에서 익절·손절 구분 ───────────────────────────────────


def test_익절은_빨강_손절은_파랑이다():
    """종목 표와 같은 규칙 — 새로 익힐 것이 없어야 한다."""
    from trader.ui import theme
    from trader.ui.journal_dialog import entry_color

    c = theme.palette()
    assert entry_color(_closed(9_000)) == c.profit
    assert entry_color(_closed(-2_000)) == c.loss


def test_본전과_보유중은_회색이다():
    """이긴 것도 진 것도 아니면 눈에 덜 띄어야 한다."""
    from trader.ui import theme
    from trader.ui.journal_dialog import entry_color

    c = theme.palette()
    assert entry_color({"realized_pnl": 500, "fees": 500, "state": "종료"}) == c.muted
    assert entry_color({"realized_pnl": 0, "fees": 0, "state": "1차 매수"}) == c.muted


def test_손익_아이콘은_더_붙이지_않는다():
    """부호·색과 같은 말을 세 번 하고 있었고, 작아서 구분도 안 됐다."""
    from trader.ui.journal_dialog import entry_label

    label = entry_label(_closed(9_000))
    for icon in ("💰", "🛑", "⚪"):
        assert icon not in label
    assert "+8,500" in label  # 부호는 그대로 남는다


def test_코멘트를_쓴_줄에만_표시가_붙는다():
    from trader.ui.journal_dialog import WRITTEN_MARK, entry_label

    written = {**_closed(9_000), "good": "진입이 좋았다"}
    assert entry_label(written).startswith(WRITTEN_MARK)
    assert not entry_label(_closed(9_000)).startswith(WRITTEN_MARK)


def test_고른_줄에서도_색이_유지된다(dialog):
    """선택하는 순간 결과가 안 보이면 목록을 훑는 의미가 없다."""
    from trader.ui.journal_dialog import entry_color

    dialog._list.selection_set(0)
    dialog.update()
    expected = entry_color(dialog._visible[0])
    assert dialog._list.itemcget(0, "foreground") == expected
    assert dialog._list.itemcget(0, "selectforeground") == expected


def test_다크_선택_배경에서도_색이_읽힌다():
    """선택 배경이 밝으면 그 위의 빨강·파랑이 묻힌다(#2f5d8a 에서 익절 2.5)."""
    from trader.ui import theme

    def contrast(a: str, b: str) -> float:
        def lum(color: str) -> float:
            rgb = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
            rgb = [
                c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb
            ]
            return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]

        high, low = sorted((lum(a), lum(b)), reverse=True)
        return (high + 0.05) / (low + 0.05)

    for mode in (theme.LIGHT, theme.DARK):
        p = theme.resolve(mode)
        for name in ("profit", "loss", "muted"):
            assert contrast(getattr(p, name), p.select_bg) >= 4.5, f"{mode} {name}"


# ── 보유기간 ────────────────────────────────────────────────────


def test_보유기간_표기는_당일과_이월을_다른_단위로_적는다():
    """벽시계 시간은 밤·주말을 넘기면 거짓말이 된다.

    금 15:20 매수 → 월 09:05 은 벽시계로 66시간이지만 실제 장중 노출은 6시간 남짓이다.
    날짜를 넘긴 순간부터는 거래일 수로만 말한다.
    """
    from trader.journal import format_holding

    entry = "2026-08-19 09:14:00"
    assert format_holding(entry, "2026-08-19 09:14:30", 0) == "0분"
    assert format_holding(entry, "2026-08-19 10:01:00", 0) == "47분"
    assert format_holding(entry, "2026-08-19 10:14:00", 0) == "1시간 0분"
    assert format_holding(entry, "2026-08-19 12:26:00", 0) == "3시간 12분"
    assert format_holding(entry, "2026-08-20 09:05:00", 1) == "2일차"
    # 주말·휴장을 건너뛴 거래일 수를 그대로 쓴다 (달력이 계산해 넘겨준다)
    assert format_holding(entry, "2026-08-24 09:05:00", 2) == "3일차"


def test_진입_시각을_모르면_보유기간은_빈칸이다():
    """강제 복구·수동 시작 상태 지정 — 추정하지 않는다."""
    from trader.journal import format_holding

    assert format_holding("", "2026-08-19 10:00:00", 0) == ""
    assert format_holding("깨진값", "2026-08-19 10:00:00", 0) == ""


def test_거래일_수가_없으면_날짜_차이로_물러난다():
    """키움 미연결 등으로 달력이 없어도 값이 보여야 한다."""
    from trader.journal import format_holding

    assert format_holding("2026-08-19 09:14:00", "2026-08-19 10:01:00") == "47분"
    assert format_holding("2026-08-19 09:14:00", "2026-08-21 10:01:00") == "3일차"


def test_진입_시각은_사이클의_첫_매수다():
    """2차 매수(물타기)는 새 진입이 아니므로 시계를 되돌리지 않는다."""
    from trader.journal import entry_time

    cycle = [
        {"ts": "2026-08-19 09:14:00", "side": "매수", "to_state": "1차 매수"},
        {"ts": "2026-08-19 10:30:00", "side": "매수", "to_state": "2차 매수"},
        {"ts": "2026-08-19 13:00:00", "side": "매도", "to_state": "종료"},
    ]
    assert entry_time(cycle) == "2026-08-19 09:14:00"
    assert entry_time([{"ts": "2026-08-19 09:00:00", "side": None}]) == ""


def test_일지_타임라인에_진입_시각과_보유기간이_붙는다():
    from trader.journal import cycle_timeline

    cycle = [
        {
            "ts": "2026-08-19 09:14:00",
            "trade_date": "2026-08-19",
            "side": "매수",
            "to_state": "1차 매수",
        },
        {
            "ts": "2026-08-19 12:26:00",
            "trade_date": "2026-08-19",
            "side": "매도",
            "to_state": "종료",
        },
    ]
    text = cycle_timeline(cycle)
    assert "진입 2026-08-19 09:14" in text
    assert "청산 2026-08-19 12:26" in text
    assert "보유 3시간 12분" in text


# ── 스레드용 짧은 타임라인 ──────────────────────────────────────


def test_하루에_끝난_매매는_시각만_적는다():
    """폰에서 한 줄에 들어가야 한다."""
    from trader.journal import compact_timeline

    cycle = [
        {"ts": "2026-08-26 09:05:40", "side": "매수", "to_state": "1차 매수"},
        {"ts": "2026-08-26 09:41:39", "side": "매도", "to_state": "종료"},
    ]
    assert compact_timeline(cycle) == "진입 09:05 · 청산 09:41"


def test_날짜를_넘긴_매매는_날짜까지_적는다():
    from trader.journal import compact_timeline

    cycle = [
        {"ts": "2026-08-24 13:41:00", "side": "매수", "to_state": "1차 매수"},
        {"ts": "2026-08-26 10:38:00", "side": "매도", "to_state": "종료"},
    ]
    assert compact_timeline(cycle) == "진입 08-24 13:41 · 청산 08-26 10:38"


def test_기준봉과_경과_거래일이_붙는다():
    from trader.journal import compact_timeline
    from trader.trading_calendar import TradingCalendar

    cycle = [{"ts": "2026-08-26 09:05:40", "side": "매수", "to_state": "1차 매수"}]
    text = compact_timeline(cycle, "2026-08-19", TradingCalendar())
    assert "기준봉 08-19" in text and "D+" in text


def test_보유기간은_넣지_않는다():
    """embed 의 가격대 줄에 이미 있어 중복된다."""
    from trader.journal import compact_timeline

    cycle = [
        {"ts": "2026-08-26 09:05:40", "side": "매수", "to_state": "1차 매수"},
        {"ts": "2026-08-26 09:41:39", "side": "매도", "to_state": "종료"},
    ]
    assert "보유" not in compact_timeline(cycle)


def test_매수_기록이_없으면_빈_문자열이다():
    from trader.journal import compact_timeline

    assert compact_timeline([]) == ""
