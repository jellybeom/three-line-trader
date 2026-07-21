"""코어 ↔ UI 통신 버스 — 스레드 안전 큐 2개와 메시지 타입 정의.

UI 는 이벤트 큐를 200ms 주기로 폴링해 화면만 갱신하고,
모든 조작(등록·삭제·리셋·시작/정지)은 명령 큐로 코어에 위임한다.
UI 에는 비즈니스 로직이 없다 — 메시지를 만들고 그리는 것이 전부다.

메시지는 전부 불변 dataclass 라서, 타입 목록 자체가 코어와 UI 사이의
계약(프로토콜) 문서 역할을 한다.
"""

from __future__ import annotations

import queue
from dataclasses import dataclass, field

from trader.state_machine import Params, Position

# ── 코어 → UI 이벤트 ──────────────────────────────────────────


@dataclass(frozen=True)
class PositionUpdate:
    """포지션 스냅샷 변경 (등록·전이·리셋 직후). params 는 편집 창 프리필과 3선 컬럼 표시용."""

    symbol: str
    name: str
    position: Position
    params: Params


@dataclass(frozen=True)
class Funds:
    """전역 자금·전략 설정 (시작 시 복원값 또는 변경 확정값)."""

    total: float
    max_symbols: int
    buy1_amount: float
    buy2_amount: float
    tp_rates: tuple[float, float, float] = (0.03, 0.05, 0.07)
    tp_ratios: tuple[float, float, float] = (0.40, 0.50, 0.10)


@dataclass(frozen=True)
class Mode:
    """투자 모드. real=True 는 실전투자."""

    real: bool


@dataclass(frozen=True)
class SymbolInfo:
    """종목코드 조회 결과."""

    symbol: str
    name: str


@dataclass(frozen=True)
class DiscordStatus:
    """Discord 연결 상태."""

    connected: bool
    detail: str


@dataclass(frozen=True)
class NotifyLevel:
    """Discord 알림 수준 (시작 시 복원값 또는 변경 확정값)."""

    level: str


@dataclass(frozen=True)
class TradeDate:
    """현재 활성 매매일. UI 는 수신 시 테이블을 비우고 이어지는 PositionUpdate 로 다시 채운다."""

    date: str  # YYYY-MM-DD


@dataclass(frozen=True)
class Tick:
    """현재가 갱신 (표시용)."""

    symbol: str
    price: float


@dataclass(frozen=True)
class LogLine:
    """이벤트 로그 한 줄."""

    ts: str
    symbol: str
    kind: str
    text: str


@dataclass(frozen=True)
class WatchStatus:
    """감시 실행 여부 (시작/일시정지 버튼 상태 동기화)."""

    running: bool


@dataclass(frozen=True)
class KiwoomStatus:
    """키움 연결 상태 (연결/실패/끊김)."""

    connected: bool
    detail: str  # 만료일 또는 실패 사유


@dataclass(frozen=True)
class Account:
    """계좌 요약 (주문가능금액)."""

    deposit: float


@dataclass(frozen=True)
class SymbolRemoved:
    symbol: str


# ── UI → 코어 명령 ─────────────────────────────────────────────


@dataclass(frozen=True)
class Register:
    """관심종목 등록/갱신. position=None 이면 편집 모드 — 현재 포지션을 유지한 채
    설정(params)만 교체한다 (편집 창이 열려 있는 동안 상태가 바뀌어도 안전)."""

    symbol: str
    name: str
    params: Params
    position: Position | None


@dataclass(frozen=True)
class SetFunds:
    """전역 자금·전략 설정 변경. '대기' 상태 종목에는 즉시 반영된다."""

    total: float
    max_symbols: int
    buy1_amount: float
    buy2_amount: float
    tp_rates: tuple[float, float, float]
    tp_ratios: tuple[float, float, float]


@dataclass(frozen=True)
class SetMode:
    """투자 모드 전환."""

    real: bool


@dataclass(frozen=True)
class ConnectKiwoom:
    """config.toml 의 현재 모드 키로 키움 연결 (토큰 발급 + 계좌 조회 + WS 시작)."""


@dataclass(frozen=True)
class RefreshAccount:
    """예수금(주문가능금액) 새로고침."""


@dataclass(frozen=True)
class LookupSymbol:
    """종목코드 → 종목명 조회 요청 (등록 창의 '조회' 버튼)."""

    symbol: str


@dataclass(frozen=True)
class CarryOver:
    """보유 종목을 다음 영업일 리스트로 이월 (상태·평단·잔량 유지)."""

    symbol: str


@dataclass(frozen=True)
class ConnectDiscord:
    """config.toml 의 webhook 으로 Discord 연결 (테스트 발송 포함)."""


@dataclass(frozen=True)
class SetNotifyLevel:
    """Discord 알림 수준 변경 (전체 / 매매만 / 에러만 / 끔)."""

    level: str


@dataclass(frozen=True)
class SetTradeDate:
    """매매일 전환 — 해당 날짜의 관심종목 리스트를 로드한다. 감시 중에는 거부된다."""

    date: str  # YYYY-MM-DD


@dataclass(frozen=True)
class Delete:
    symbol: str


@dataclass(frozen=True)
class Reset:
    """관리자 개입: 종료 → 대기."""

    symbol: str


@dataclass(frozen=True)
class SetRunning:
    running: bool


@dataclass
class Bus:
    """코어와 UI 가 공유하는 큐 한 쌍."""

    events: queue.Queue = field(default_factory=queue.Queue)  # 코어 → UI
    commands: queue.Queue = field(default_factory=queue.Queue)  # UI → 코어
