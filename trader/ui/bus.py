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
    memo: str = ""
    tags: str = ""  # 종목 선정 근거 (편집 창 프리필용)
    base_date: str = ""  # 기준봉 날짜


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
class Blocked:
    """진입 보류 상태 — 종목 행에 표시한다 (active=False 면 해제)."""

    symbol: str
    active: bool
    reason: str = ""


@dataclass(frozen=True)
class ChartReady:
    """복기 차트 PNG 생성 완료 — UI 가 창으로 표시한다."""

    symbol: str
    name: str
    daily_path: str
    minute_path: str


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
    """계좌 요약 (주문가능금액 + 표시용 계좌 라벨)."""

    deposit: float
    account: str = ""  # config.toml 의 표시용 계좌 문자열 (예: 5790-8081 위탁종합)


@dataclass(frozen=True)
class SymbolRemoved:
    symbol: str


# ── UI → 코어 명령 ─────────────────────────────────────────────


@dataclass(frozen=True)
class Register:
    """관심종목 등록/갱신.

    - position=None: 편집(설정만 교체, 현재 포지션 유지)
    - position + edit=True: 편집에서 상태·평단·수량까지 통째로 교체 (기존 종목 덮어쓰기 허용)
    - position + edit=False: 신규 등록 (기존 종목이면 거부 — 실수 덮어쓰기 방지)
    """

    symbol: str
    name: str
    params: Params
    position: Position | None
    edit: bool = False
    memo: str = ""
    # 종목 선정 근거 — 기준봉(급등일) 시점의 판단이라 종목에 고정된다.
    # 쉼표 구분 문자열 (예: "KOSPI상승장,테마주")
    tags: str = ""
    base_date: str = ""  # 기준봉 날짜 (YYYY-MM-DD)


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
class ManualSell:
    """수동 전량 청산 (시장가) — 소량 보유 등 사용자 판단 개입. 감시 중에도 허용."""

    symbol: str


@dataclass(frozen=True)
class CarryOver:
    """보유 종목을 다음 영업일 리스트로 이월 (상태·평단·잔량 유지)."""

    symbol: str


@dataclass(frozen=True)
class ChartRequest:
    """복기 차트 생성 요청.

    결과는 **요청한 곳으로** 돌아간다 — 📈 버튼은 UI 창(ChartReady 이벤트),
    Discord 슬래시 명령은 Discord 채널. 명령을 낸 자리에서 결과를 못 보면
    원격 조회의 의미가 없다.
    """

    symbol: str
    to_discord: bool = False  # True 면 UI 창 대신 Discord 채널로 전송


@dataclass(frozen=True)
class SendChartDiscord:
    """생성된 차트 이미지를 Discord 로 전송 (차트 창의 버튼)."""

    symbol: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class RegistrationNotice:
    """CSV 등록 결과 — 종목별 선정 근거를 담은 알림을 코어가 발송한다.

    rows: {symbol, name, tags, base_date, memo, qty} 목록.
    """

    rows: tuple
    warnings: tuple = ()
    staged: int = 0  # 3선 미입력으로 대기 목록에 들어간 종목 수
    skipped: int = 0  # 이미 등록돼 있어 건너뛴 종목 수


@dataclass(frozen=True)
class Notice:
    """UI 에서 일어난 일을 코어에 기록·발송 요청 (CSV 불러오기 결과 등).

    팝업창은 닫으면 사라져 나중에 확인할 수 없다. 사용자의 입력 실수처럼 되짚어야 하는
    내용은 로그와 Discord 에도 남긴다.
    """

    kind: str  # 등록 / 경고 / 에러 ...
    text: str
    symbol: str = "시스템"


@dataclass(frozen=True)
class RequestDailySummary:
    """오늘 매매 요약을 Discord 로 즉시 발송 (스케줄과 별개로 수동 확인용)."""


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
