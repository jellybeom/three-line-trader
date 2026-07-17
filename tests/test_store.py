"""store 단위 테스트 — 저장/복원, 장애 복구, 트랜잭션 원자성을 검증한다.

모든 테스트는 pytest 의 tmp_path (임시 폴더) 에 실제 SQLite 파일을 만들어
프로그램 재시작(= Store 를 닫고 다시 여는 것)까지 그대로 재현한다.
"""

import pytest

from trader.state_machine import (
    Params,
    Position,
    State,
    apply_fill,
    decide,
    mark_pending,
)
from trader.store import Store

P = Params(
    line1=10_000, line2=9_000, line3=8_000, buy1_amount=1_000_000, buy2_amount=900_000
)
D = "2026-07-17"  # 기준 매매일


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "trader.db")
    yield s
    s.close()


def reopen(store: Store, tmp_path) -> Store:
    """프로그램 비정상 종료 후 재시작을 재현한다."""
    store.close()
    return Store(tmp_path / "trader.db")


# ── 등록 / 복원 ────────────────────────────────────────────────


def test_등록_후_복원하면_설정과_포지션이_그대로(store, tmp_path):
    store.register_symbol(D, "005930", "삼성전자", P)
    store = reopen(store, tmp_path)
    name, params, pos = store.load_all(D)["005930"]
    assert name == "삼성전자"
    assert params == P  # 3선, 수량, 익절률/비중, 버퍼 전부 일치
    assert pos == Position()  # 신규 등록 기본값은 '대기'
    store.close()


def test_종목코드_앞자리_0이_보존됨(store):
    store.register_symbol(D, "005930", "삼성전자", P)
    assert "005930" in store.load_all(D)  # INTEGER 였다면 5930 이 됐을 것


def test_오버나이트_상태로_등록하면_그_상태로_복원(store, tmp_path):
    overnight = Position(
        state=State.BUY1_TP1, avg_price=10_000, total_bought=100, remaining=60
    )
    store.register_symbol(D, "005930", "삼성전자", P, overnight)
    store = reopen(store, tmp_path)
    _, _, pos = store.load_all(D)["005930"]
    assert pos == overnight
    store.close()


def test_재등록하면_설정이_대체되고_이전_상태가_이벤트에_남음(store):
    store.register_symbol(D, "005930", "삼성전자", P)
    p2 = Params(
        line1=11_000,
        line2=10_000,
        line3=9_000,
        buy1_amount=550_000,
        buy2_amount=500_000,
    )
    store.register_symbol(D, "005930", "삼성전자", p2)
    _, params, _ = store.load_all(D)["005930"]
    assert params == p2
    events = store.fetch_events("005930")
    assert len(events) == 2
    assert events[1]["from_state"] == "대기"  # 덮어쓰기 전 상태 감사 기록


def test_삭제하면_포지션도_함께_지워지고_이력은_남음(store):
    store.register_symbol(D, "005930", "삼성전자", P)
    store.delete_symbol(D, "005930")
    assert store.load_all(D) == {}
    kinds = [e["kind"] for e in store.fetch_events("005930")]
    assert kinds == ["등록", "삭제"]  # events 는 append-only


# ── 전이 저장 / 장애 복구 ──────────────────────────────────────


def test_전이_저장은_포지션과_이벤트를_함께_남김(store):
    store.register_symbol(D, "005930", "삼성전자", P)
    pos = Position()
    d = decide(pos, P, 9_950)
    pos = apply_fill(mark_pending(pos), d, fill_price=9_950, fill_qty=d.qty)
    store.save_transition(D, "005930", State.WAITING, pos, d, price=9_950)

    _, _, restored = store.load_all(D)["005930"]
    assert restored.state is State.BUY1 and restored.avg_price == 9_950
    ev = store.fetch_events("005930")[-1]
    assert (ev["kind"], ev["from_state"], ev["to_state"]) == (
        "전이",
        "대기",
        "1차 매수",
    )
    assert (ev["side"], ev["qty"], ev["price"]) == ("매수", 100, 9_950)


def test_전체_사이클_후_재시작해도_최종_상태_복원(store, tmp_path):
    store.register_symbol(D, "005930", "삼성전자", P)
    pos, state_before = Position(), State.WAITING
    for price in [10_000, 10_300, 10_500, 10_700]:  # 매수 → 3% → 5% → 7% 청산
        d = decide(pos, P, price)
        pos = apply_fill(mark_pending(pos), d, price, d.qty)
        store.save_transition(D, "005930", state_before, pos, d, price)
        state_before = pos.state

    store = reopen(store, tmp_path)  # 장애 후 재시작
    _, _, restored = store.load_all(D)["005930"]
    assert restored.state is State.CLOSED and restored.remaining == 0
    assert len(store.fetch_events("005930")) == 5  # 등록 1 + 전이 4
    store.close()


def test_주문_전송_직후_죽어도_pending이_복원됨(store, tmp_path):
    """체결 확인 전 크래시: pending=True 로 복원되어 중복 주문을 막는다.
    실제 체결 여부 확인(잔고 대조)은 코어 시작 시 reconcile 의 몫이다."""
    store.register_symbol(D, "005930", "삼성전자", P)
    pos = mark_pending(Position())  # 매수 주문 전송 직후
    store.save_position(D, "005930", pos)

    store = reopen(store, tmp_path)
    _, _, restored = store.load_all(D)["005930"]
    assert restored.pending is True
    store.close()


def test_손상된_포지션_행은_복원_시점에_즉시_실패(store):
    store.register_symbol(D, "005930", "삼성전자", P)
    # 외부에서 DB 를 잘못 건드린 상황: 익절 상태인데 잔량 0
    store._conn.execute(
        "UPDATE positions SET state=?, avg_price=10000, total_bought=100, remaining=0 "
        "WHERE trade_date=? AND symbol='005930'",
        (State.BUY1_TP1.value, D),
    )
    store._conn.commit()
    with pytest.raises(ValueError, match="005930"):
        store.load_all(D)


# ── 관리자 리셋 / 일반 로그 / 주문 기록 ─────────────────────────


def test_관리자_리셋은_대기로_되돌리고_이벤트를_남김(store):
    closed = Position(
        state=State.CLOSED, avg_price=10_000, total_bought=100, remaining=0
    )
    store.register_symbol(D, "005930", "삼성전자", P, closed)
    new_pos = store.admin_reset(D, "005930", closed)
    assert new_pos == Position()
    ev = store.fetch_events("005930")[-1]
    assert ev["kind"] == "리셋" and ev["to_state"] == "대기"


def test_관리자_리셋은_종료가_아니면_거부되고_DB도_불변(store):
    holding = Position(
        state=State.BUY1, avg_price=10_000, total_bought=100, remaining=100
    )
    store.register_symbol(D, "005930", "삼성전자", P, holding)
    with pytest.raises(ValueError):
        store.admin_reset(D, "005930", holding)
    _, _, pos = store.load_all(D)["005930"]
    assert pos == holding  # 실패한 리셋이 DB 를 바꾸지 않음


def test_일반_로그와_기간_필터_조회(store):
    store.register_symbol(D, "005930", "삼성전자", P)
    store.log(D, "005930", "에러", "WebSocket 재연결")
    assert store.fetch_events("005930")[-1]["reason"] == "WebSocket 재연결"
    assert store.fetch_events("005930", since="2099-01-01") == []


def test_구버전_스키마_DB는_명확한_에러로_안내(tmp_path):
    db = tmp_path / "trader.db"
    s = Store(db)
    s._conn.execute("PRAGMA user_version=1")  # 구버전 DB 상황 재현
    s._conn.commit()
    s.close()
    with pytest.raises(RuntimeError, match="스키마 버전 불일치"):
        Store(db)


def test_같은_종목도_매매일이_다르면_독립(store):
    store.register_symbol("2026-07-17", "005930", "삼성전자", P)
    p_next = Params(
        line1=11_000,
        line2=10_000,
        line3=9_000,
        buy1_amount=1_100_000,
        buy2_amount=1_000_000,
    )
    store.register_symbol(
        "2026-07-18",
        "005930",
        "삼성전자",
        p_next,
        Position(state=State.BUY1, avg_price=10_000, total_bought=100, remaining=60),
    )
    _, params_17, pos_17 = store.load_all("2026-07-17")["005930"]
    _, params_18, pos_18 = store.load_all("2026-07-18")["005930"]
    assert params_17 == P and pos_17 == Position()  # 전날 리스트는 그대로
    assert params_18 == p_next and pos_18.remaining == 60  # 다음날은 오버나이트 상태
    assert store.list_dates() == ["2026-07-18", "2026-07-17"]  # 최신순


def test_실현손익이_저장되고_복원됨(store, tmp_path):
    pos = Position(state=State.CLOSED, realized_pnl=44_000)
    store.register_symbol(D, "005930", "삼성전자", P, pos)
    store = reopen(store, tmp_path)
    _, _, restored = store.load_all(D)["005930"]
    assert restored.realized_pnl == 44_000
    store.close()


def test_전역_설정_저장과_재시작_복원(store, tmp_path):
    store.set_setting("funds_total", "10000000")
    store.set_setting("funds_total", "20000000")  # 덮어쓰기
    store = reopen(store, tmp_path)
    assert store.get_setting("funds_total") == "20000000"
    assert store.get_setting("없는키", "기본값") == "기본값"
    store.close()


def test_주문_기록과_체결_갱신(store):
    order_id = store.record_order("005930", "매수", 100)
    store.update_order(
        order_id, "체결", fill_price=9_950, fill_qty=100, broker_order_no="A123"
    )
    row = store._conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    assert (row["status"], row["fill_price"], row["broker_order_no"]) == (
        "체결",
        9_950,
        "A123",
    )
