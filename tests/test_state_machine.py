"""state_machine 단위 테스트 — README 상태 전이 표를 그대로 검증한다.

시나리오 헬퍼 run()으로 가격 시퀀스를 흘려보내면
decide → mark_pending → apply_fill 전체 사이클이 재현된다.
(테스트에서는 시장가 주문이 지시 가격 그대로 전량 체결된다고 가정)
"""

import pytest

from trader.state_machine import (
    Decision,
    Params,
    Position,
    Side,
    State,
    apply_fill,
    apply_transition,
    decide,
    mark_pending,
    reset,
)

# 기준 설정: 1선 10,000 / 2선 9,000 / 3선 8,000, 매수 금액 1차 100만 / 2차 90만
# → 1선 부근 체결 시 1차 100주, 2선 부근 체결 시 2차 100주가 되도록 맞춘 값
P = Params(
    line1=10_000, line2=9_000, line3=8_000, buy1_amount=1_000_000, buy2_amount=900_000
)


def run(pos: Position, prices: list[float], params: Params = P) -> Position:
    """가격 시퀀스를 순서대로 처리. 판정이 나오면 즉시 전량 체결로 가정."""
    for price in prices:
        d = decide(pos, params, price)
        if d is None:
            continue
        if d.side is None:  # 주문 없는 즉시 전이
            pos = apply_transition(pos, d)
        else:
            pos = apply_fill(mark_pending(pos), d, fill_price=price, fill_qty=d.qty)
    return pos


# ── 기본 전이: 대기 → 1차 매수 ──────────────────────────────────


def test_매수_수량은_트리거_체결가_기준_즉석_계산():
    d = decide(Position(), P, 9_500)  # 1,000,000 ÷ 9,500 = 105.26 → 105주
    assert d.qty == 105
    pos = run(Position(), [9_500])
    d2 = decide(pos, P, 8_900)  # 2차: 900,000 ÷ 8,900 = 101.1 → 101주
    assert d2.qty == 101


def test_대기에서_1선_이탈시_1차매수():
    d = decide(Position(), P, 10_000)  # 1선 정확히 터치도 포함 (이하)
    assert d == Decision(State.BUY1, Side.BUY, 100, d.reason)


def test_대기에서_1선_위면_아무것도_안함():
    assert decide(Position(), P, 10_001) is None


def test_1차매수_체결시_평단과_물량_기록():
    pos = run(Position(), [9_950])
    assert pos.state is State.BUY1
    assert pos.avg_price == 9_950
    assert pos.total_bought == pos.remaining == 100


# ── 1차 매수 라인: 익절 사다리 ──────────────────────────────────


def test_1차_익절_사다리_전체_경로():
    pos = run(Position(), [10_000])  # 평단 10,000
    pos = run(pos, [10_300])  # +3%
    assert pos.state is State.BUY1_TP1
    assert pos.remaining == 60  # 40% 매도
    pos = run(pos, [10_500])  # +5%
    assert pos.state is State.BUY1_TP2
    assert pos.remaining == 10  # 누적 90% 매도
    pos = run(pos, [10_700])  # +7%
    assert pos.state is State.CLOSED
    assert pos.remaining == 0  # 잔량(10%) 전량 청산


def test_익절_트리거_직전_가격은_전이_없음():
    pos = run(Position(), [10_000])
    assert decide(pos, P, 10_299) is None  # +3% 미만


def test_1차_3퍼_익절_상태에서_본절_이탈시_전량청산():
    pos = run(Position(), [10_000, 10_300])
    pos = run(pos, [10_000])  # 평단 터치 = 본절
    assert pos.state is State.CLOSED and pos.remaining == 0


def test_1차_5퍼_익절_상태에서_본절_이탈시_전량청산():
    pos = run(Position(), [10_000, 10_300, 10_500])
    pos = run(pos, [9_999])
    assert pos.state is State.CLOSED and pos.remaining == 0


# ── 2차 매수 라인 ──────────────────────────────────────────────


def test_2선_이탈시_2차매수_및_평단_재계산():
    pos = run(Position(), [10_000, 9_000])
    assert pos.state is State.BUY2
    assert pos.avg_price == 9_500  # (10000×100 + 9000×100) / 200
    assert pos.total_bought == pos.remaining == 200


def test_2차_익절은_새_평단_기준():
    pos = run(Position(), [10_000, 9_000])  # 새 평단 9,500
    assert decide(pos, P, 9_784) is None  # 9500×1.03 = 9785 미만
    pos = run(pos, [9_785])  # +3%
    assert pos.state is State.BUY2_TP1
    assert pos.remaining == 120  # 200주의 40% 매도


def test_2차_익절_사다리_전체_경로():
    pos = run(Position(), [10_000, 9_000, 9_785, 9_975])  # +3%, +5%
    assert pos.state is State.BUY2_TP2
    assert pos.remaining == 20
    pos = run(pos, [10_165])  # +7%
    assert pos.state is State.CLOSED and pos.remaining == 0


def test_2차매수에서_3선_이탈시_전량손절():
    pos = run(Position(), [10_000, 9_000])
    pos = run(pos, [8_000])
    assert pos.state is State.CLOSED and pos.remaining == 0


def test_2차_익절_상태에서_본절_이탈시_전량청산():
    pos = run(Position(), [10_000, 9_000, 9_785])
    pos = run(pos, [9_500])  # 새 평단 터치
    assert pos.state is State.CLOSED and pos.remaining == 0


def test_익절_시작_후에는_2선_이탈해도_추가매수_없음():
    pos = run(Position(), [10_000, 10_300])  # 1차 + 3% 익절
    d = decide(pos, P, 9_000)  # 2선 이탈 가격
    # 본절(10,000) 이탈이 먼저 발동 → 매수가 아니라 전량 청산
    assert d.side is Side.SELL and d.to_state is State.CLOSED


def test_1차3익절_상태에서_7퍼_갭이면_5퍼_생략_전량청산():
    pos = run(Position(), [10_000, 10_300])  # 1차 + 3% 익절, 잔량 60
    d = decide(pos, P, 10_700)  # +5%를 건너뛰고 +7% 도달
    assert d.to_state is State.CLOSED
    assert d.qty == 60  # 잔량 전부를 주문 1건으로
    assert run(pos, [10_700]).remaining == 0


def test_2차3익절_상태에서_7퍼_갭이면_5퍼_생략_전량청산():
    pos = run(Position(), [10_000, 9_000, 9_785])  # 2차 + 3% 익절, 잔량 120
    d = decide(pos, P, 10_165)  # 새 평단 9,500 대비 +7%
    assert d.to_state is State.CLOSED and d.qty == 120


def test_극소량_보유시_매도수량_0이면_주문없이_상태만_전이():
    p = Params(
        line1=10_000, line2=9_000, line3=8_000, buy1_amount=20_000, buy2_amount=18_000
    )  # 1선 체결 시 2주
    pos = run(Position(), [10_000], p)  # 2주 보유
    d = decide(pos, p, 10_300)  # floor(2×0.4) = 0주
    assert d.side is None and d.to_state is State.BUY1_TP1
    pos = run(pos, [10_300, 10_500, 10_700], p)  # 이후 1주, 1주 매도로 정리
    assert pos.state is State.CLOSED and pos.remaining == 0


def test_본절_버퍼가_1차_익절률_이상이면_설정_에러():
    with pytest.raises(ValueError):
        Params(
            line1=10_000,
            line2=9_000,
            line3=8_000,
            buy1_amount=1_000_000,
            buy2_amount=900_000,
            breakeven_buffer=0.03,
        )


# ── 갭 처리: 중간 단계 생략 ─────────────────────────────────────


def test_대기에서_2선_이하_갭이면_1차2차_동시매수():
    pos = run(Position(), [8_500])
    assert pos.state is State.BUY2
    assert (
        pos.total_bought == pos.remaining == 223
    )  # (100만+90만) ÷ 8,500 을 한 주문으로
    assert pos.avg_price == 8_500  # 평단 = 실제 갭 체결가


def test_동시매수_후_익절은_체결_평단_기준():
    pos = run(Position(), [8_500])
    assert decide(pos, P, 8_754) is None  # 8500×1.03 = 8755 미만
    pos = run(pos, [8_755])
    assert pos.state is State.BUY2_TP1 and pos.remaining == 134  # 223 - floor(223×0.4)


def test_대기에서_3선_이하면_주문없이_당일_종료():
    d = decide(Position(), P, 8_000)  # 손절선 이하 갭 시가
    assert d.side is None and d.qty == 0 and d.to_state is State.CLOSED
    pos = run(Position(), [7_900, 8_500, 10_300])  # 이후 반등해도 당일 재진입 없음
    assert pos.state is State.CLOSED
    assert pos.total_bought == 0  # 실제 매매는 일어나지 않았음


def test_주문없는_전이와_체결_전이의_확정_함수는_교차사용_불가():
    no_order = decide(Position(), P, 7_900)
    with pytest.raises(ValueError):
        apply_fill(Position(), no_order, 7_900, 0)
    order = decide(Position(), P, 9_950)
    with pytest.raises(ValueError):
        apply_transition(Position(), order)


def test_갭상승_7퍼_이상이면_중간단계_생략_전량청산():
    pos = run(Position(), [10_000])
    d = decide(pos, P, 10_800)  # +8% 갭
    assert d.to_state is State.CLOSED
    assert d.qty == 100  # 부분 매도 없이 한 번에 전량


def test_갭상승_5퍼면_누적_90퍼_매도후_5퍼_익절_상태():
    pos = run(Position(), [10_000])
    pos = run(pos, [10_550])  # +3%를 건너뛰고 +5% 구간 진입
    assert pos.state is State.BUY1_TP2
    assert pos.remaining == 10  # 40%+50% = 90% 를 한 주문으로 매도


def test_갭하락_1차매수에서_3선_아래면_2차매수_생략_손절():
    pos = run(Position(), [10_000])
    d = decide(pos, P, 7_900)  # 2선·3선 동시 갭 이탈
    assert d.side is Side.SELL and d.to_state is State.CLOSED


# ── 체결 대기(pending) / 종료 / 관리자 개입 ─────────────────────


def test_체결_대기중에는_판정_중단():
    pos = mark_pending(run(Position(), [10_000]))
    assert decide(pos, P, 10_300) is None


def test_종료_상태에서는_아무_가격에도_반응_없음():
    pos = run(Position(), [10_000, 10_300, 10_000])  # 본절 종료
    assert decide(pos, P, 9_000) is None
    assert decide(pos, P, 12_000) is None


def test_관리자_리셋은_종료에서만_가능():
    pos = run(Position(), [10_000, 10_300, 10_000])
    # 종료 → 대기 초기화하되, 당일 실현손익(+3% 익절 40주 = 12,000)은 이어서 집계
    assert reset(pos) == Position(realized_pnl=12_000)
    holding = Position(
        state=State.BUY1, avg_price=10_000, total_bought=100, remaining=100
    )
    with pytest.raises(ValueError):
        reset(holding)


def test_실현손익은_매도_체결마다_누적():
    pos = run(Position(), [10_000])  # 100주 @ 10,000
    pos = run(pos, [10_300])  # 40주 매도 → (10300-10000)×40 = +12,000
    assert pos.realized_pnl == 12_000
    pos = run(pos, [10_500])  # 50주 매도 → +25,000 누적
    assert pos.realized_pnl == 37_000
    pos = run(pos, [10_700])  # 잔량 10주 → +7,000
    assert pos.realized_pnl == 44_000 and pos.state is State.CLOSED


def test_손절시_실현손익은_음수():
    pos = run(Position(), [10_000, 9_000])  # 200주, 평단 9,500
    pos = run(pos, [8_000])  # 전량 손절 → (8000-9500)×200 = -300,000
    assert pos.realized_pnl == -300_000


# ── 오버나이트 / 수동 초기 상태 ─────────────────────────────────


def test_오버나이트_상태로_시작해_다음날_이어서_진행():
    # 전날 '1차 매수 + 3% 익절' (평단 10,000, 100주 중 60주 잔량) 로 마감
    pos = Position(
        state=State.BUY1_TP1, avg_price=10_000, total_bought=100, remaining=60
    )
    pos = run(pos, [10_500])  # 다음날 +5% 도달
    assert pos.state is State.BUY1_TP2
    assert pos.remaining == 10  # 누적 90% 기준으로 이어서 매도


def test_모순된_수동_입력은_생성_시점에_실패():
    with pytest.raises(ValueError):  # 익절 상태인데 잔량 0
        Position(state=State.BUY1_TP1, avg_price=10_000, total_bought=100, remaining=0)
    with pytest.raises(ValueError):  # 보유 상태인데 평단 없음
        Position(state=State.BUY2, avg_price=0, total_bought=100, remaining=100)
    with pytest.raises(ValueError):  # 대기인데 보유 정보 존재
        Position(state=State.WAITING, avg_price=10_000, total_bought=100, remaining=100)
    with pytest.raises(ValueError):  # 종료인데 잔량 존재
        Position(state=State.CLOSED, avg_price=10_000, total_bought=100, remaining=10)
    with pytest.raises(ValueError):  # 잔량이 누적 매수량 초과
        Position(state=State.BUY1, avg_price=10_000, total_bought=100, remaining=150)


# ── 수량 계산 / 유효성 ──────────────────────────────────────────


def test_애매한_수량도_익절_합계가_전량과_일치():
    p = Params(
        line1=10_000, line2=9_000, line3=8_000, buy1_amount=70_000, buy2_amount=63_000
    )  # 1선 체결 시 7주
    pos = run(Position(), [10_000, 10_300, 10_500, 10_700], p)
    assert pos.state is State.CLOSED
    assert pos.remaining == 0  # 7주: 2 + 4 + 1 로 남김없이 청산


def test_매도_체결량이_잔량을_넘으면_에러():
    pos = run(Position(), [10_000])
    d = Decision(State.CLOSED, Side.SELL, 999, "테스트")
    with pytest.raises(ValueError):
        apply_fill(pos, d, 10_000, 999)


def test_잘못된_설정은_생성_시점에_실패():
    with pytest.raises(ValueError):  # 선 순서 위반
        Params(
            line1=9_000,
            line2=10_000,
            line3=8_000,
            buy1_amount=1_000_000,
            buy2_amount=900_000,
        )
    with pytest.raises(ValueError):  # 비중 합 100% 위반
        Params(
            line1=10_000,
            line2=9_000,
            line3=8_000,
            buy1_amount=1_000_000,
            buy2_amount=900_000,
            tp_ratios=(0.4, 0.5, 0.2),
        )
    with pytest.raises(ValueError):  # 매수 금액으로 1주도 못 삼
        Params(
            line1=10_000,
            line2=9_000,
            line3=8_000,
            buy1_amount=9_999,
            buy2_amount=900_000,
        )


def test_본절_버퍼_적용시_버퍼_가격에서_청산():
    p = Params(
        line1=10_000,
        line2=9_000,
        line3=8_000,
        buy1_amount=1_000_000,
        buy2_amount=900_000,
        breakeven_buffer=0.004,
    )
    pos = run(Position(), [10_000, 10_300], p)
    assert decide(pos, p, 10_041) is None  # 버퍼(10,040) 위 → 유지
    d = decide(pos, p, 10_040)  # 평단 +0.4% 이하 → 청산
    assert d.to_state is State.CLOSED
