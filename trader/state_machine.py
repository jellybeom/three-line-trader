"""3선 자동매매 상태 머신 — 순수 로직 (I/O 없음).

이 모듈은 외부 세계(키움 API, DB, UI)를 전혀 모른다.
가격을 받아 "무엇을 해야 하는지"(Decision)를 돌려줄 뿐,
실제 주문 전송과 저장은 코어(orchestrator)의 몫이다.

동작 원리 (2단계 전이):
    1. decide(pos, params, price)  → 조건 충족 시 Decision(주문 지시) 반환
    2. 코어가 주문 전송 후 mark_pending() 으로 체결 대기 표시
    3. 체결통보 수신 시 apply_fill() → 평단/잔량 갱신 + 상태 전이 확정

상태 전이는 "주문 접수"가 아니라 "체결 확인" 시점에 완료된다. (README 3장)
경계값: 모든 가격 조건은 터치 시 발동한다 (하락 조건 ≤, 상승 조건 ≥).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum


class State(str, Enum):
    WAITING = "대기"
    BUY1 = "1차 매수"
    BUY1_TP1 = "1차 매수 + 3% 익절"
    BUY1_TP2 = "1차 매수 + 5% 익절"
    BUY2 = "2차 매수"
    BUY2_TP1 = "2차 매수 + 3% 익절"
    BUY2_TP2 = "2차 매수 + 5% 익절"
    CLOSED = "종료"


class Side(str, Enum):
    BUY = "매수"
    SELL = "매도"


@dataclass(frozen=True)
class Params:
    """종목별 설정값. 유효하지 않으면 생성 시점에 즉시 실패한다.

    매수는 금액 기반: 수량은 트리거 시점 체결가로 floor(금액 ÷ 가격) 즉석 계산.
    매수 트리거는 항상 기준선 이하 가격에서 발동하므로,
    금액 ≥ 기준선 가격이면 수량이 최소 1주 이상임이 보장된다.
    """

    line1: float  # 1차 매수 기준선
    line2: float  # 2차 매수 기준선
    line3: float  # 손절 기준선
    buy1_amount: float  # 1차 매수 금액
    buy2_amount: float  # 2차 매수 금액
    tp_rates: tuple[float, float, float] = (0.03, 0.05, 0.07)  # 평단 대비 익절 트리거
    tp_ratios: tuple[float, float, float] = (
        0.40,
        0.50,
        0.10,
    )  # 최초 물량 대비 매도 비중

    def __post_init__(self) -> None:
        if not (self.line1 > self.line2 > self.line3 > 0):
            raise ValueError(
                f"1선 > 2선 > 3선 > 0 이어야 함: {self.line1}, {self.line2}, {self.line3}"
            )
        if not (0 < self.tp_rates[0] < self.tp_rates[1] < self.tp_rates[2]):
            raise ValueError(f"익절률은 오름차순이어야 함: {self.tp_rates}")
        if abs(sum(self.tp_ratios) - 1.0) > 1e-9:
            raise ValueError(f"익절 비중 합은 100% 여야 함: {self.tp_ratios}")
        if self.buy1_amount < self.line1 or self.buy2_amount < self.line2:
            raise ValueError(
                "매수 금액으로 최소 1주를 살 수 있어야 함: "
                f"1차 {self.buy1_amount} (≥ 1선 {self.line1}), 2차 {self.buy2_amount} (≥ 2선 {self.line2})"
            )


_HOLDING_STATES = frozenset(
    {
        State.BUY1,
        State.BUY1_TP1,
        State.BUY1_TP2,
        State.BUY2,
        State.BUY2_TP1,
        State.BUY2_TP2,
    }
)


@dataclass(frozen=True)
class Position:
    """종목 하나의 현재 스냅샷. 불변(frozen) — 변경은 항상 새 객체를 반환한다.

    시작 상태는 '대기'가 기본이지만, 오버나이트 보유분을 이어가기 위해
    사용자가 임의 상태로 직접 생성할 수도 있다. 이때 상태와 보유 정보가
    모순되면(예: 익절 상태인데 잔량 0) 생성 시점에 즉시 실패한다.
    """

    state: State = State.WAITING
    avg_price: float = 0.0
    total_bought: int = 0  # 누적 매수 총량 = 익절 비중의 기준 "최초 보유 물량"
    remaining: int = 0  # 현재 잔량
    pending: bool = False  # 주문 접수 후 체결 대기 중이면 True (중복 주문 방지)
    realized_pnl: float = 0.0  # 당일 누적 실현손익 (세전). 매도 체결마다 누적

    def __post_init__(self) -> None:
        if self.state in _HOLDING_STATES:
            if self.avg_price <= 0 or not (0 < self.remaining <= self.total_bought):
                raise ValueError(
                    f"보유 상태({self.state.value})는 평단 > 0, 0 < 잔량 ≤ 누적매수량 이어야 함: "
                    f"평단 {self.avg_price}, 잔량 {self.remaining}/{self.total_bought}"
                )
        elif self.state is State.WAITING:
            if self.avg_price != 0 or self.total_bought != 0 or self.remaining != 0:
                raise ValueError("대기 상태는 보유 정보가 없어야 함")
        elif self.remaining != 0:  # CLOSED
            raise ValueError(f"종료 상태는 잔량이 0 이어야 함: {self.remaining}")


@dataclass(frozen=True)
class Decision:
    """상태 머신의 출력: 어떤 주문을 내고, 체결되면 어느 상태로 갈 것인가.

    side 가 None 이면 주문 없는 즉시 전이 — 체결을 기다리지 않고
    apply_transition() 으로 바로 확정한다. (예: 3선 이하 갭 시가 → 진입 금지 종료)
    """

    to_state: State
    side: Side | None
    qty: int
    reason: str


# ── 내부 헬퍼 ──────────────────────────────────────────────────────


def _tp_price(pos: Position, params: Params, level: int) -> float:
    """level차 익절 트리거 가격 (level: 1~3). 항상 현재 평단가 기준."""
    return pos.avg_price * (1 + params.tp_rates[level - 1])


def _breakeven(pos: Position, params: Params) -> float:
    """본절 청산 기준가 = 평단가."""
    return pos.avg_price


def _tp_sell_qty(pos: Position, params: Params, upto_level: int) -> int:
    """upto_level차 익절까지 도달했을 때 이번에 매도할 수량.

    비중은 최초 물량(total_bought) 기준 누적으로 계산하므로,
    갭 상승으로 단계를 건너뛰어도 매도 총량이 어긋나지 않는다.
    예) 100주, 대기 익절 상태에서 +5% 도달 → 누적 90% - 기매도 0 = 90주 매도
    """
    cum_ratio = sum(params.tp_ratios[:upto_level])
    already_sold = pos.total_bought - pos.remaining
    qty = math.floor(pos.total_bought * cum_ratio) - already_sold
    return min(max(qty, 0), pos.remaining)


def _buy_qty(amount: float, price: float) -> int:
    """매수 수량 즉석 계산: floor(금액 ÷ 트리거 시점 체결가)."""
    return int(amount // price)


def _sell_all(pos: Position, reason: str) -> Decision:
    return Decision(State.CLOSED, Side.SELL, pos.remaining, reason)


def _decide_tp_chain(
    pos: Position,
    params: Params,
    price: float,
    base: str,
    tp1_state: State,
    tp2_state: State,
) -> Decision | None:
    """익절 사다리 공통 판정. base 는 로그용 접두어("1차"/"2차").

    높은 단계부터 검사한다 — 한 틱이 여러 단계를 통과하면(갭)
    가장 높은 단계 하나로 수렴시키기 위함이다.
    +7% 이상이면 중간 단계를 생략하고 즉시 전량 청산한다. (README 운영규칙)
    """
    if price >= _tp_price(pos, params, 3):
        return _sell_all(pos, f"{base} 평단 +{params.tp_rates[2]:.0%} 도달 → 전량 청산")
    if price >= _tp_price(pos, params, 2) and pos.state != tp2_state:
        return _tp_decision(
            pos,
            params,
            2,
            tp2_state,
            f"{base} 평단 +{params.tp_rates[1]:.0%} 도달 → 2차 익절",
        )
    if price >= _tp_price(pos, params, 1) and pos.state not in (tp1_state, tp2_state):
        return _tp_decision(
            pos,
            params,
            1,
            tp1_state,
            f"{base} 평단 +{params.tp_rates[0]:.0%} 도달 → 1차 익절",
        )
    return None


def _tp_decision(
    pos: Position, params: Params, level: int, to_state: State, reason: str
) -> Decision:
    """부분 익절 Decision 생성. 계산된 매도 수량이 0이면(극소량 보유)
    거부될 0주 주문 대신 주문 없는 상태 전이로 처리한다."""
    qty = _tp_sell_qty(pos, params, level)
    if qty == 0:
        return Decision(to_state, None, 0, reason + " (매도 수량 0 → 상태만 전이)")
    return Decision(to_state, Side.SELL, qty, reason)


# ── 공개 API ──────────────────────────────────────────────────────


def decide(pos: Position, params: Params, price: float) -> Decision | None:
    """체결가 틱 하나에 대한 판정. 할 일이 없으면 None.

    pending(체결 대기) 중이거나 종료 상태면 아무것도 하지 않는다.
    """
    if pos.pending or pos.state is State.CLOSED:
        return None

    s = pos.state

    if s is State.WAITING:
        if price <= params.line3:  # 손절선 아래 갭 시가: 진입하지 않고 당일 매매 종료
            return Decision(
                State.CLOSED, None, 0, "3선 이하 갭 시가 → 진입 금지, 당일 종료"
            )
        if (
            price <= params.line2
        ):  # 갭 하락 진입: 1·2차 금액을 합쳐 한 주문으로 동시 매수
            qty = _buy_qty(params.buy1_amount + params.buy2_amount, price)
            return Decision(State.BUY2, Side.BUY, qty, "2선 이하 갭 → 1·2차 동시 매수")
        if price <= params.line1:
            return Decision(
                State.BUY1,
                Side.BUY,
                _buy_qty(params.buy1_amount, price),
                "1선 이탈 → 1차 매수",
            )
        return None

    # ── 1차 매수 라인 ──
    if s is State.BUY1:
        if d := _decide_tp_chain(
            pos, params, price, "1차", State.BUY1_TP1, State.BUY1_TP2
        ):
            return d
        if price <= params.line3:  # 갭 하락: 2차 매수를 생략하고 즉시 손절
            return _sell_all(pos, "3선 이탈(갭) → 2차 매수 생략, 전량 손절")
        if price <= params.line2:
            return Decision(
                State.BUY2,
                Side.BUY,
                _buy_qty(params.buy2_amount, price),
                "2선 이탈 → 2차 매수",
            )
        return None

    if s in (State.BUY1_TP1, State.BUY1_TP2):
        if d := _decide_tp_chain(
            pos, params, price, "1차", State.BUY1_TP1, State.BUY1_TP2
        ):
            return d
        if price <= _breakeven(pos, params):
            return _sell_all(pos, "본절 이탈 → 잔량 전량 청산")
        return None

    # ── 2차 매수 라인 ──
    if s is State.BUY2:
        if d := _decide_tp_chain(
            pos, params, price, "2차", State.BUY2_TP1, State.BUY2_TP2
        ):
            return d
        if price <= params.line3:
            return _sell_all(pos, "3선 이탈 → 전량 손절")
        return None

    if s in (State.BUY2_TP1, State.BUY2_TP2):
        if d := _decide_tp_chain(
            pos, params, price, "2차", State.BUY2_TP1, State.BUY2_TP2
        ):
            return d
        if price <= _breakeven(pos, params):
            return _sell_all(pos, "본절 이탈 → 잔량 전량 청산")
        return None

    raise AssertionError(f"처리되지 않은 상태: {s}")


def mark_pending(pos: Position) -> Position:
    """주문 전송 직후 호출 — 체결 확인 전까지 decide() 를 잠근다."""
    return replace(pos, pending=True)


def apply_transition(pos: Position, decision: Decision) -> Position:
    """주문 없는 전이(side=None)를 즉시 확정한다. 체결을 기다리지 않는다."""
    if decision.side is not None:
        raise ValueError("주문이 있는 Decision 은 apply_fill() 로 확정해야 함")
    return replace(pos, state=decision.to_state, pending=False)


def apply_fill(
    pos: Position, decision: Decision, fill_price: float, fill_qty: int
) -> Position:
    """체결통보 수신 시 호출. 평단/잔량을 갱신하고 상태 전이를 확정한다.

    - 매수: 평단가 재계산 (물타기 반영), 누적 매수량 증가
    - 매도: 잔량 차감. 평단가는 바뀌지 않는다 (README 설계 원칙)
    """
    if decision.side is None:
        raise ValueError("주문 없는 Decision 은 apply_transition() 으로 확정해야 함")
    if decision.side is Side.BUY:
        new_remaining = pos.remaining + fill_qty
        new_avg = (
            pos.avg_price * pos.remaining + fill_price * fill_qty
        ) / new_remaining
        return replace(
            pos,
            state=decision.to_state,
            avg_price=new_avg,
            total_bought=pos.total_bought + fill_qty,
            remaining=new_remaining,
            pending=False,
        )

    new_remaining = pos.remaining - fill_qty
    if new_remaining < 0:
        raise ValueError(
            f"매도 체결량이 잔량 초과: 잔량 {pos.remaining}, 체결 {fill_qty}"
        )
    realized = pos.realized_pnl + (fill_price - pos.avg_price) * fill_qty
    return replace(
        pos,
        state=decision.to_state,
        remaining=new_remaining,
        realized_pnl=realized,
        pending=False,
    )


def reset(pos: Position) -> Position:
    """관리자 수동 개입: 종료 → 대기. 새 사이클을 위해 포지션을 초기화한다."""
    if pos.state is not State.CLOSED:
        raise ValueError(f"종료 상태에서만 초기화 가능: 현재 {pos.state.value}")
    return Position(realized_pnl=pos.realized_pnl)  # 당일 실현손익은 이어서 집계
