"""실전 매매 코어 — watcher(시세) · state_machine(판단) · broker(주문) · store(기록) 조립.

simulate.SimCore 의 실전판이다. "즉시 체결 가정" 대신 실제 2단계 흐름을 구현한다:

    틱 수신 → decide → (예수금 방어) → REST 주문 전송 → pending 표시
    → 체결통보(00) 수신 → apply_fill 로 상태 확정 → 저장·UI 발행

코어 레벨 정책 (README 운영 규칙):
- 예수금 부족: 1차 매수 시점 → 주문 없이 '종료' 전환. 2차 매수 시점 → 1차 물량
  유지, 해당 종목 추가 매수만 차단(1회 알림). 손절·익절 경로는 계속 동작한다.
- 시작·연결 시 계좌 실보유와 저장된 포지션을 대조(reconcile)해 불일치를 경고한다.
- WebSocket 재연결 직후 REST 현재가로 공백 구간을 1회 보정한다.
- 체결통보가 일정 시간 오지 않는 pending 주문은 경고한다 (수동 확인 필요).

전체가 코어 스레드의 단일 asyncio 루프에서 돌며(store 는 이 스레드 소유),
blocking REST 호출만 asyncio.to_thread 로 내보낸다.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

from trader.broker import Broker, BrokerError, extract_fill
from trader.kiwoom import load_auth
from trader.notifier import (
    build_alert_embed,
    build_batch_embed,
    build_daily_summary_embed,
    build_proximity_embed,
    build_trade_embed,
    format_message,
    format_trade,
    should_notify,
)
from dataclasses import replace

from trader.state_machine import (
    Decision,
    Params,
    Side,
    State,
    apply_fill,
    apply_transition,
    decide,
    mark_pending,
)
from trader.store import Store
from trader.ui import bus
from trader.watcher import Tick, Watcher

_PENDING_WARN_SEC = 90  # 체결통보 미도착 경고 기준 (아직 정상 범위일 수 있음)
# 개장 직후에는 시장가 주문도 수 분 뒤에 체결될 수 있다(2026-07-30 실측: 09:00:04 접수 →
# 09:02:16 체결). 이 시간을 짧게 잡으면 살아 있는 주문을 죽은 것으로 오판해 중복 주문이 된다.
_PENDING_RECOVER_SEC = 420  # 7분
_ORDER_FAIL_COOLDOWN_SEC = 30  # 주문 실패 후 같은 종목 재시도 금지 시간
_ORDER_FAIL_BLOCK_COUNT = 3  # 연속 실패 이 횟수면 당일 해당 종목 주문 차단
_DEPOSIT_TTL_SEC = 5.0  # 예수금 조회 캐시 수명 (틱마다 REST 호출 방지)
# 조회한 주문가능금액의 이 비율까지만 쓴다. 직전 체결이 증권사 여력에 반영되기까지
# 시차가 있어, 꽉 채워 주문하면 "매수증거금이 부족합니다" 로 거부된다(2026-07-29 실측).
_DEPOSIT_SAFETY = 0.98
_BOT_REFRESH_SEC = (
    10.0  # Discord 대시보드 편집 주기 (새 메시지가 아니라 편집이라 가볍다)
)
_DAY_LOW_FLUSH_SEC = 60.0  # 당일 최저가 저장 주기 (전이가 없는 종목도 남기기 위해)
_ACCOUNT_TTL_SEC = 30.0  # 계좌 요약 조회 주기 (평가손익은 초 단위로 볼 필요가 없다)
_AUTO_CONNECT_RETRY_SEC = 300.0  # 자동 연결 재시도 간격 (서버 점검 등 일시 장애 대비)
_BLOCK_LOG_COOLDOWN_SEC = 600  # 같은 사유·구간이 이어질 때의 재기록 주기 (최후 방어)
_MIN_ENTRY_QTY = 3  # 1차 매수 수량이 이 미만이면 분할 익절이 어려워 경고
_BATCH_QUIET_SEC = 2.0  # 이 시간 동안 새 알림이 없으면 모아둔 것을 한 장으로 보낸다
_BATCH_MAX = 50  # 버퍼가 이만큼 차면 기다리지 않고 즉시 발송
# 묶음 대상: 한 번에 여러 건이 쏟아지는 정보성 알림. 체결 알림은 즉시성이 중요해 제외한다.
_BATCH_KINDS = (
    "등록",
    "편집",
    "삭제",
    "보류",
    "경고",
    "에러",
    "설정",
    "리셋",
    "이월",
    "연결",
    "감시",
    "시작",
    "요약",
)
_LOOP_SEC = 0.1


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _mode_file(db_dir: Path) -> Path:
    """현재 투자 모드를 담는 작은 파일.

    모드별로 DB 파일을 나누면 '어느 DB 를 열지' 를 정하기 위해 모드를 먼저 알아야 하는데,
    모드가 DB 안(settings)에 있으면 순환이 된다. 그래서 모드만 DB 밖으로 뺀다.
    """
    return db_dir / "mode.txt"


def read_mode(db_dir: str | Path = "data") -> bool:
    """저장된 모드를 읽는다 (True = 실전). 파일이 없으면 안전하게 모의로 시작."""
    path = _mode_file(Path(db_dir))
    if not path.exists():
        return False
    return path.read_text(encoding="utf-8").strip() == "실전"


def write_mode(real: bool, db_dir: str | Path = "data") -> None:
    directory = Path(db_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _mode_file(directory).write_text("실전" if real else "모의", encoding="utf-8")


def db_path_for(real: bool, db_dir: str | Path = "data") -> str:
    """모드별 DB 파일 경로. 모의와 실전 기록이 절대 섞이지 않게 파일 자체를 분리한다."""
    return str(Path(db_dir) / ("trader-real.db" if real else "trader-mock.db"))


def _load_auto_connect(config_path: str) -> bool:
    """[startup] auto_connect — 프로그램 실행 시 키움·Discord 를 스스로 연결할지.

    연결은 조회·알림 경로만 여는 것이라 그 자체로는 주문이 나가지 않는다.
    실제 매매는 '감시 시작' 이후에만 일어나므로, 자동 연결은 안전하다.
    """
    import tomllib
    from pathlib import Path

    path = Path(config_path)
    if not path.exists():
        return False
    return bool(
        tomllib.loads(path.read_text(encoding="utf-8"))
        .get("startup", {})
        .get("auto_connect", False)
    )


def _load_schedule(config_path: str) -> dict:
    """config.toml 의 [schedule] — 감시 자동 시작/중지와 일일 요약 발송 시각.

    휴장일 목록은 두지 않는다. 휴장일에는 체결 틱 자체가 없어 감시가 켜져도
    아무 일도 일어나지 않으므로, 요일만 확인하면 충분하다.
    """
    import tomllib
    from pathlib import Path

    default = {
        "enabled": False,
        "start": dtime(8, 55),
        "stop": dtime(15, 30),
        "summary": dtime(15, 35),
        "summary2": dtime(20, 5),
    }
    path = Path(config_path)
    if not path.exists():
        return default
    section = tomllib.loads(path.read_text(encoding="utf-8")).get("schedule", {})
    result = dict(default)
    result["enabled"] = bool(section.get("enabled", False))
    for key in ("start", "stop", "summary", "summary2"):
        raw = section.get(key)
        if not raw:
            continue
        try:
            hour, minute = (int(x) for x in str(raw).split(":"))
            result[key] = dtime(hour, minute)
        except (ValueError, TypeError):
            pass  # 형식이 잘못되면 기본값 유지
    return result


def _load_chart_font(config_path: str) -> str | None:
    import tomllib

    path = Path(config_path)
    if not path.exists():
        return None
    return (
        tomllib.loads(path.read_text(encoding="utf-8")).get("chart", {}).get("font")
        or None
    )


def _load_fee_rates(config_path: str) -> tuple[float, float]:
    """(수수료율, 거래세율). config.toml 의 [fees] 에서 읽고, 없으면 보수적 기본값.

    수수료율은 증권사·계좌마다 다르고 거래세율은 제도 변경이 잦으므로 설정으로 뺀다.
    거래세는 매도에만 부과된다.
    """
    import tomllib
    from pathlib import Path

    default = (0.00015, 0.0015)
    path = Path(config_path)
    if not path.exists():
        return default
    fees = tomllib.loads(path.read_text(encoding="utf-8")).get("fees", {})
    try:
        return (
            float(fees.get("commission_rate", default[0])),
            float(fees.get("tax_rate", default[1])),
        )
    except (TypeError, ValueError):
        return default


def _load_account_label(config_path: str, real: bool) -> str:
    """config.toml 의 표시용 계좌 문자열 (선택 항목 account). 없으면 빈 문자열."""
    import tomllib
    from pathlib import Path

    path = Path(config_path)
    if not path.exists():
        return ""
    kiwoom = tomllib.loads(path.read_text(encoding="utf-8")).get("kiwoom", {})
    section = kiwoom.get("real" if real else "mock") or kiwoom
    return str(section.get("account", ""))


class Core:
    def __init__(
        self, b: bus.Bus, db_dir: str = "data", config_path: str = "config.toml"
    ):
        self._bus = b
        self._db_dir = db_dir
        self._config_path = config_path
        self._store: Store | None = None
        self._broker: Broker | None = None
        # 알림 발송은 Discord 봇 하나로만 나간다 (_bot). 웹훅은 사용하지 않는다.
        self._account_label = ""
        self._notify_level = "전체"
        self._max_symbols = 10
        self._watcher: Watcher | None = None
        self._watcher_task: asyncio.Task | None = None
        self._running = False
        self._mode_real = False
        self._date = date.today().isoformat()
        self._entries: dict[str, dict] = {}  # symbol -> {name, params, pos, price}
        # order_no -> {symbol, from_state, decision, order_id, ts, warned}
        self._pending: dict[str, dict] = {}
        self._recovered: dict[str, dict] = {}  # 강제 복구한 주문 — 늦은 체결통보 대비
        self._bot = None  # Discord 봇 (선택) — 없어도 프로그램은 정상 동작한다
        self._bot_task = None
        self._bot_refresh_at = 0.0
        self._auto_connect = False
        # None = 아직 한 번도 시도 안 함. 0.0 을 쓰면 time.monotonic() 이 부팅 후 경과
        # 시간이라, PC 를 켠 직후 실행했을 때 '방금 시도했다' 고 오판해 첫 연결을 건너뛴다
        # (2026-08-04 실측 버그).
        self._auto_connect_at: float | None = None
        self._auto_connect_warned = False  # 실패 알림은 처음 한 번만
        self._deposit_display: float | None = (
            None  # 화면·대시보드 표시용 최근 주문가능금액
        )
        self._account: dict[str, float] = {}  # 계좌 요약 (매입·평가·손익·추정자산)
        self._account_at = 0.0
        self._day_low_at = 0.0
        self._notice_batch: list[tuple[str, str, str]] = []  # (종류, 표시, 내용)
        self._notice_at = 0.0  # 마지막 적재 시각 (조용해지면 발송)
        self._deposit_cache: tuple[float, float] | None = None  # (조회시각, 값)
        self._block_logged: dict[str, tuple[str, float]] = (
            {}
        )  # 종목 → (마지막 사유·구간, 시각)
        self._block_notified: set[str] = set()  # 보류 알림은 종목당 1회
        self._order_fail: dict[str, dict] = {}  # 종목별 주문 실패 누적 {count, until}
        self._order_blocked: set[str] = set()  # 연속 실패로 당일 주문 차단된 종목
        self._commission_rate = 0.0
        self._tax_rate = 0.0
        self._schedule = {"enabled": False}
        # 항목별 마지막 실행 날짜. 메모리에만 두면 재시작 때 '오늘 아직 안 했다' 고 판단해
        # 이미 보낸 요약을 다시 보낸다(2026-07-31 실측: 15:35 발송 후 15:41 재시작 시 재발송).
        # 실제 판단은 DB(settings)를 기준으로 하고, 이 dict 는 조회를 줄이는 캐시다.
        self._sched_done: dict[str, str] = {}
        self._chart_busy: set[str] = set()  # 종목별 차트 생성 중복 방지

    # ── 메인 루프 ───────────────────────────────────────────────

    async def run(self) -> None:
        self._mode_real = read_mode(self._db_dir)
        self._commission_rate, self._tax_rate = _load_fee_rates(self._config_path)
        self._schedule = _load_schedule(self._config_path)
        self._auto_connect = _load_auto_connect(self._config_path)
        self._open_store()
        if self._schedule["enabled"]:
            self._log(
                "시스템",
                "설정",
                f"자동 스케줄 사용: {self._schedule['start']:%H:%M} 감시 시작 · "
                f"{self._schedule['stop']:%H:%M} 중지 · "
                f"{self._schedule['summary']:%H:%M} 요약 발송",
                notify=False,
            )
        self._log(
            "시스템",
            "시작",
            f"코어 시작 · {'실전' if self._mode_real else '모의'}투자 DB · "
            f"매매일 {self._date} ({len(self._entries)}종목 복원) · "
            f"수수료 {self._commission_rate:.4%} / 거래세 {self._tax_rate:.4%}",
        )
        await self._start_bot()  # 설정이 없으면 조용히 건너뛴다
        if self._auto_connect:
            self._log(
                "시스템",
                "설정",
                "자동 연결 사용 — 키움·Discord 연결을 시도합니다",
                notify=False,
            )

        while True:
            await self._drain_commands()
            await self._check_pending()
            await self._check_schedule()
            if (
                self._notice_batch
                and time.monotonic() - self._notice_at > _BATCH_QUIET_SEC
            ):
                await self._flush_notices()
            await self._tick_bot()
            await self._tick_auto_connect()
            self._flush_day_lows()
            await asyncio.sleep(_LOOP_SEC)

    def _open_store(self) -> None:
        """현재 모드의 DB 를 열고 화면 상태를 전부 다시 발행한다 (시작·모드 전환 공용)."""
        if self._store is not None:
            self._store.close()
        self._store = Store(db_path_for(self._mode_real, self._db_dir))
        self._notify_level = self._store.get_setting("notify_level", "전체")
        self._max_symbols = int(self._store.get_setting("funds_max", "10"))
        self._block_logged.clear()
        self._block_notified.clear()
        self._sched_done.clear()
        self._deposit_cache = None
        self._order_fail.clear()
        self._order_blocked.clear()
        self._pending.clear()
        self._load_date(self._date)
        self._emit_date_loaded()  # TradeDate 이벤트가 UI 목록을 비우고 다시 채운다
        self._replay_logs()
        self._emit_funds()
        self._bus.events.put(bus.Mode(self._mode_real))
        self._bus.events.put(bus.NotifyLevel(self._notify_level))
        self._bus.events.put(bus.WatchStatus(False))
        self._warn_restored_pending()

    # ── 명령 처리 (UI → 코어) ───────────────────────────────────

    async def _drain_commands(self) -> None:
        while not self._bus.commands.empty():
            cmd = self._bus.commands.get_nowait()
            try:
                await self._handle_command(cmd)
            except (
                Exception
            ) as e:  # noqa: BLE001 — 명령 하나의 실패가 코어를 죽이면 안 됨
                self._log("시스템", "에러", f"{type(cmd).__name__} 처리 실패: {e}")

    async def _handle_command(self, cmd) -> None:
        match cmd:
            case bus.ManualSell(symbol=s):
                await self._manual_sell(s)
            case bus.CarryOver(symbol=s):
                if self._running:
                    self._log(
                        s, "에러", "감시 중에는 이월할 수 없습니다 — 먼저 중지하세요"
                    )
                    return
                e = self._entries.get(s)
                if e is None:
                    return
                if e["pos"].pending:
                    self._log(s, "에러", "체결 대기 중인 종목은 이월할 수 없습니다")
                    return
                target = date.fromisoformat(self._date) + timedelta(days=1)
                while target.weekday() >= 5:  # 주말 건너뛰고 다음 영업일
                    target += timedelta(days=1)
                self._store.register_symbol(
                    target.isoformat(),
                    s,
                    e["name"],
                    e["params"],
                    e["pos"],
                    memo=e.get("memo", ""),
                )
                self._log(
                    s,
                    "이월",
                    f"{target.isoformat()} 리스트로 이월 (상태: {e['pos'].state.value}, "
                    f"잔량 {e['pos'].remaining}주)",
                )
            case bus.ConnectKiwoom():
                await self._connect()
            case bus.RefreshAccount():
                await self._refresh_account()
            case bus.LookupSymbol(symbol=s):
                if self._broker is None:
                    self._log(s, "에러", "종목명 조회는 키움 연결 후 가능합니다")
                    return
                name, _ = await asyncio.to_thread(self._broker.stock_info, s)
                self._bus.events.put(bus.SymbolInfo(s, name))
            case bus.Notice(kind=kind, text=text, symbol=sym):
                self._log(sym, kind, text)
            case bus.RequestDailySummary():
                await self.send_daily_summary()
            case bus.ChartRequest(symbol=s, to_discord=to_discord):
                if self._broker is None:
                    self._log(s, "에러", "차트는 키움 연결 후 생성할 수 있습니다")
                    return
                if s not in self._entries:
                    self._log(s, "에러", "관심종목에 없는 종목입니다")
                    return
                if s in self._chart_busy:
                    self._log(s, "차트", "이미 생성 중입니다", notify=False)
                    return
                self._log(s, "차트", "복기 차트 생성 중... (수 초 소요)", notify=False)
                asyncio.create_task(
                    self._chart_task(s, to_ui=not to_discord, to_discord=to_discord)
                )
            case bus.SendChartDiscord(symbol=s, paths=paths):
                if self._bot is None:
                    self._log(s, "에러", "Discord 연결 후 전송할 수 있습니다")
                    return
                asyncio.create_task(self._send_chart_images(s, paths))
            case bus.SetNotifyLevel(level=lv):
                self._notify_level = lv
                self._store.set_setting("notify_level", lv)
                self._bus.events.put(bus.NotifyLevel(lv))
                self._log("시스템", "설정", f"Discord 알림 수준: {lv}")
            case bus.Register(
                symbol=s, name=n, params=p, position=pos, edit=edit, memo=memo
            ):
                if self._running:
                    self._log(
                        s,
                        "에러",
                        "감시 중에는 등록/편집할 수 없습니다 — 먼저 중지하세요",
                    )
                    return
                if pos is not None and not edit and s in self._entries:
                    self._log(
                        s,
                        "에러",
                        "이미 등록된 종목입니다 — 수정하려면 편집(✎)을 사용하세요",
                    )
                    return
                if pos is None:  # 편집(설정만): 현재 포지션 유지
                    if s not in self._entries:
                        self._log(s, "에러", "편집 대상 종목이 없습니다")
                        return
                    pos = self._entries[s]["pos"]
                self._store.register_symbol(self._date, s, n, p, pos, memo=memo)
                price = (
                    self._entries[s]["price"] if s in self._entries else pos.avg_price
                )
                self._entries[s] = {
                    "name": n,
                    "params": p,
                    "pos": pos,
                    "price": price,
                    "memo": memo,
                    "high": pos.high_price,
                    "low": pos.low_price,
                    "day_low": pos.day_low,
                }
                self._clear_block(s)
                self._emit_position(s)
                self._log(
                    s,
                    "등록" if not edit else "편집",
                    f"{n} (상태: {pos.state.value}, 잔량 {pos.remaining}주)",
                )
                self._warn_small_qty(s, n, p)
                await self._sync_watcher_symbols()
            case bus.Delete(symbol=s):
                if self._running:
                    self._log(
                        s, "에러", "감시 중에는 삭제할 수 없습니다 — 먼저 중지하세요"
                    )
                    return
                self._store.delete_symbol(self._date, s)
                self._entries.pop(s, None)
                self._clear_block(s)
                self._bus.events.put(bus.SymbolRemoved(s))
                self._log(s, "삭제", "관심종목 제외")
                await self._sync_watcher_symbols()
            case bus.Reset(symbol=s) if s in self._entries:
                try:
                    new_pos = self._store.admin_reset(
                        self._date, s, self._entries[s]["pos"]
                    )
                except ValueError as e:
                    self._log(s, "에러", str(e))
                else:
                    self._entries[s]["pos"] = new_pos
                    self._clear_block(s)
                    self._emit_position(s)
                    self._log(s, "리셋", "관리자 수동 초기화 (종료 → 대기)")
            case bus.SetRunning(running=r):
                if r and self._broker is None:
                    self._log(
                        "시스템", "에러", "키움 연결 후 감시를 시작할 수 있습니다"
                    )
                    return
                self._running = r
                self._bus.events.put(bus.WatchStatus(r))
                self._log("시스템", "감시", "감시 시작" if r else "감시 중지")
            case bus.SetFunds(
                total=t,
                max_symbols=m,
                buy1_amount=b1,
                buy2_amount=b2,
                tp_rates=rates,
                tp_ratios=ratios,
            ):
                if self._running:
                    self._log(
                        "시스템",
                        "에러",
                        "감시 중에는 설정을 변경할 수 없습니다 — 먼저 중지하세요",
                    )
                    return
                for key, val in (
                    ("funds_total", t),
                    ("funds_max", m),
                    ("funds_buy1", b1),
                    ("funds_buy2", b2),
                    ("funds_rates", ",".join(map(str, rates))),
                    ("funds_ratios", ",".join(map(str, ratios))),
                ):
                    self._store.set_setting(key, str(val))
                self._max_symbols = m
                self._emit_funds()
                self._apply_globals_to_waiting(b1, b2, rates, ratios)
                self._log(
                    "시스템",
                    "설정",
                    f"전역 설정 적용: 총 {t:,.0f} / {m}종목 / 1차 {b1:,.0f} / 2차 {b2:,.0f} "
                    f"/ 익절 {'/'.join(f'{r:.0%}' for r in rates)}",
                )
            case bus.SetMode(real=real):
                if self._running:
                    self._log("시스템", "에러", "감시 중에는 모드를 전환할 수 없습니다")
                    return
                await self._disconnect("모드 전환 — 다시 연결하세요")
                self._mode_real = real
                write_mode(real, self._db_dir)
                self._open_store()  # 모드별 DB 로 교체 후 전체 재로드
                self._log(
                    "시스템",
                    "설정",
                    f"{'실전' if real else '모의'}투자 모드로 전환 "
                    f"(DB: {db_path_for(real, self._db_dir)})",
                )
            case bus.SetTradeDate(date=d):
                if self._running:
                    self._log(
                        "시스템", "에러", "감시 중에는 매매일을 전환할 수 없습니다"
                    )
                    return
                self._load_date(d)
                self._emit_date_loaded()
                self._replay_logs()
                self._warn_restored_pending()
                await self._sync_watcher_symbols()

    # ── 키움 연결 ───────────────────────────────────────────────

    async def _manual_sell(self, symbol: str) -> None:
        """사용자 판단 수동 전량 청산 (시장가). 감시 중에도 허용 — 주문 행위이지 설정 변경이 아니다."""
        e = self._entries.get(symbol)
        if e is None:
            return
        pos = e["pos"]
        if self._broker is None:
            self._log(symbol, "에러", "수동 청산은 키움 연결 후 가능합니다")
            return
        if pos.pending:
            self._log(symbol, "에러", "체결 대기 중에는 수동 청산할 수 없습니다")
            return
        if pos.remaining <= 0:
            self._log(symbol, "에러", "청산할 잔량이 없습니다")
            return
        d = Decision(
            State.CLOSED, Side.SELL, pos.remaining, "사용자 판단 → 수동 전량 청산"
        )
        await self._execute(symbol, d, e["price"] or pos.avg_price)

    async def _connect(self, quiet: bool = False) -> None:
        """키움 접속. quiet 면 실패를 화면 로그로만 남긴다(자동 재시도용)."""
        try:
            auth = load_auth(self._config_path, real=self._mode_real)
            await asyncio.to_thread(
                auth.token
            )  # 잘못된 키/네트워크 오류는 여기서 드러남 (10초 타임아웃)
        except Exception as e:  # noqa: BLE001
            self._bus.events.put(bus.KiwoomStatus(False, "연결 실패"))
            self._log("시스템", "에러", f"키움 연결 실패: {e}", notify=not quiet)
            return
        self._broker = Broker(auth)
        self._account_label = _load_account_label(self._config_path, self._mode_real)
        self._bus.events.put(
            bus.KiwoomStatus(True, f"만료 {auth._expires_at:%m-%d %H:%M}")
        )
        self._log(
            "시스템", "연결", f"키움 {'실전' if self._mode_real else '모의'}투자 연결됨"
        )
        await self._refresh_account()
        await self._reconcile()

        if self._watcher_task:
            self._watcher_task.cancel()
        self._watcher = Watcher(
            auth.ws_url,
            auth.token,
            on_tick=self._on_tick,
            on_status=self._on_ws_status,
            on_reconnect=self._on_ws_reconnect,
            on_fill=self._on_fill_values,
        )
        await self._sync_watcher_symbols()
        self._watcher_task = asyncio.create_task(self._watcher.run())

    async def _disconnect(self, reason: str) -> None:
        self._running = False
        self._bus.events.put(bus.WatchStatus(False))
        if self._watcher:
            await self._watcher.stop()
        if self._watcher_task:
            self._watcher_task.cancel()
        self._watcher = self._watcher_task = self._broker = None
        self._bus.events.put(bus.KiwoomStatus(False, reason))

    def _fee(self, side: Side, amount: float) -> float:
        """체결 1건의 거래비용. 매도에는 거래세가 더해진다 (원 미만 절사)."""
        rate = self._commission_rate + (self._tax_rate if side is Side.SELL else 0.0)
        return float(int(amount * rate))

    def _notify_trade(self, symbol: str, reason: str, qty: int, price: float) -> None:
        """체결 확정·종료 전이 시 사용자 친화 요약을 Discord 로 발송한다."""
        if not (self._bot and should_notify(self._notify_level, symbol, "체결")):
            return
        e = self._entries[symbol]
        pos = e["pos"]
        if (
            pos.state is State.CLOSED and pos.total_bought
        ):  # 하루 몇 번뿐인 결산 → embed
            embed = build_trade_embed(
                e["name"], symbol, reason, qty, price, pos.realized_pnl, pos.fees
            )
            asyncio.create_task(self._send_embed(embed))
            return
        asyncio.create_task(
            self._send_discord(
                format_trade(e["name"], symbol, reason, qty, price, None)
            )
        )

    async def _send_embed(self, embed: dict) -> None:
        if self._bot is None:
            return
        try:
            await self._bot.send_embed(embed)
        except Exception as e:  # noqa: BLE001 — 발송 실패가 매매를 막지 않게
            self._bus.events.put(
                bus.LogLine(_now(), "시스템", "경고", f"알림 발송 실패: {e}")
            )

    async def _send_discord(self, text: str) -> None:
        try:
            await self._bot.send_text(text)
        except (
            Exception
        ) as e:  # noqa: BLE001 — 발송 실패가 재귀 알림이 되지 않게 notify=False
            self._log("시스템", "경고", f"Discord 발송 실패: {e}", notify=False)

    async def _refresh_account(self) -> None:
        if self._broker is None:
            self._log("시스템", "에러", "키움 연결 후 조회할 수 있습니다")
            return
        deposit = await asyncio.to_thread(self._broker.deposit)
        self._bus.events.put(bus.Account(deposit, self._account_label))

    async def _reconcile(self) -> None:
        """저장된 포지션 잔량과 계좌 실보유를 대조. 불일치는 경고만 (수동 확인)."""
        holdings = await asyncio.to_thread(self._broker.holdings)
        for symbol, e in self._entries.items():
            expected = e["pos"].remaining
            actual = holdings.get(symbol, 0)
            if expected != actual:
                self._log(
                    symbol,
                    "경고",
                    f"잔고 불일치: 프로그램 {expected}주 vs 계좌 {actual}주 — 수동 확인 필요",
                )

    async def _sync_watcher_symbols(self) -> None:
        if self._watcher:
            await self._watcher.update_symbols(list(self._entries))

    # ── 시세 → 판단 → 주문 ──────────────────────────────────────

    async def _on_tick(self, tick: Tick) -> None:
        e = self._entries.get(tick.symbol)
        if e is None:
            return
        e["price"] = tick.price
        pos = e["pos"]
        # 당일 최저가는 진입 전에도 기록한다 — "1선에 얼마나 근접했나" 를 알아야
        # 진입이 없는 날이 '설정이 보수적' 인지 '시장이 안 맞는' 것인지 구분된다.
        e["day_low"] = min(e.get("day_low") or tick.price, tick.price)
        if pos.total_bought and pos.state is not State.CLOSED:  # 보유 구간만 추적
            e["high"] = max(e.get("high") or 0.0, tick.price)
            e["low"] = min(e.get("low") or tick.price, tick.price)
        self._bus.events.put(bus.Tick(tick.symbol, tick.price))
        if not self._running:
            return  # 감시 중지 상태: 시세 표시만
        d = decide(e["pos"], e["params"], tick.price, self._commission_rate)
        if d is None:
            return
        await self._execute(tick.symbol, d, tick.price)

    async def _execute(self, symbol: str, d: Decision, price: float) -> None:
        e = self._entries[symbol]
        pos, from_state = e["pos"], e["pos"].state

        if d.side is None:  # 주문 없는 즉시 전이 (진입 금지 종료, 수량 0 익절 등)
            e["pos"] = replace(
                apply_transition(pos, d),
                high_price=e.get("high") or 0.0,
                low_price=e.get("low") or 0.0,
                day_low=e.get("day_low") or 0.0,
            )
            self._store.save_transition(
                self._date, symbol, from_state, e["pos"], d, price
            )
            self._emit_position(symbol)
            self._log(symbol, "전이", d.reason, notify=False)
            if (
                e["pos"].state is State.CLOSED
            ):  # 진입 금지 등 종료만 알림 (수량 0 익절 전이는 제외)
                self._notify_trade(symbol, d.reason, 0, price)
            return

        if (
            d.side is Side.BUY and pos.state is State.WAITING
        ):  # 1차(또는 갭 동시) 진입 시점
            active = sum(
                1
                for x in self._entries.values()
                if x["pos"].state not in (State.WAITING, State.CLOSED)
            )
            if active >= self._max_symbols:
                # 슬롯이 비면 재진입할 수 있도록 '대기' 상태를 유지한다.
                self._log_block(
                    symbol,
                    f"최대 종목 수({self._max_symbols}) 도달 — "
                    "자리가 나면 재시도합니다",
                    price,
                )
                return

        if d.side is Side.BUY and not await self._can_buy(symbol, d, price):
            return
        if not self._order_allowed(symbol):
            return

        try:
            order_fn = self._broker.buy if d.side is Side.BUY else self._broker.sell
            order_no = await asyncio.to_thread(order_fn, symbol, d.qty)
        except BrokerError as err:
            self._on_order_failed(symbol, err)
            return
        self._order_fail.pop(symbol, None)  # 성공하면 실패 누적 초기화
        self._clear_block(symbol)  # 진입에 성공했으니 보류 표시 해제
        order_id = self._store.record_order(symbol, d.side.value, d.qty)
        e["pos"] = mark_pending(pos)
        self._store.save_position(self._date, symbol, e["pos"])
        self._pending[order_no] = {
            "symbol": symbol,
            "from_state": from_state,
            "decision": d,
            "order_id": order_id,
            "trigger_price": price,  # 슬리피지 분석용 기록
            "ts": time.monotonic(),
            "warned": False,
        }
        self._emit_position(symbol)
        self._log(
            symbol,
            "주문",
            f"{d.side.value} {d.qty}주 시장가 접수 (주문번호 {order_no}) — {d.reason}",
            notify=False,
        )

    async def _get_deposit(self) -> float:
        """주문가능금액 (짧은 캐시). 보류 종목이 틱마다 REST 를 때리는 것을 막는다.

        매도 체결로 자금이 늘면 캐시를 즉시 버려(_invalidate_deposit) 회수분을
        바로 인식한다.
        """
        now = time.monotonic()
        if self._deposit_cache and now - self._deposit_cache[0] < _DEPOSIT_TTL_SEC:
            return self._deposit_cache[1]
        value = await asyncio.to_thread(self._broker.deposit)
        self._deposit_cache = (time.monotonic(), value)
        self._deposit_display = value
        return value

    def _invalidate_deposit(self) -> None:
        self._deposit_cache = None

    @staticmethod
    def _entry_qty(params: Params) -> int:
        """1선 가격 기준 1차 매수 예상 수량 (경고 판단용)."""
        return int(params.buy1_amount // params.line1) if params.line1 else 0

    def _warn_small_qty(self, symbol: str, name: str, params: Params) -> None:
        """1차 수량이 너무 적으면 분할 익절이 사실상 불가능하므로 알려준다.

        진입을 막지는 않는다 — 판단은 사용자 몫이고, 소량 종목은 수동 전량 청산으로
        정리하면 된다.
        """
        qty = self._entry_qty(params)
        if qty >= _MIN_ENTRY_QTY:
            return
        detail = (
            f"1차 매수 예상 {qty}주 (1선 {params.line1:,.0f} · "
            f"금액 {params.buy1_amount:,.0f})"
        )
        if qty == 0:
            self._log(
                symbol,
                "경고",
                f"{detail} — 매수 금액이 1선보다 적어 진입할 수 없습니다",
            )
        else:
            self._log(
                symbol,
                "경고",
                f"{detail} — {_MIN_ENTRY_QTY}주 미만이라 단계 익절이 어렵습니다 "
                "(수동 전량 청산 권장)",
            )

    def _clear_block(self, symbol: str) -> None:
        if self._block_logged.pop(symbol, None) is not None:
            self._bus.events.put(bus.Blocked(symbol, False))
            if self._bot is not None:
                self._bot.set_blocked(symbol, False)
        self._block_notified.discard(symbol)

    def _price_zone(self, symbol: str, price: float) -> str:
        """현재가가 어느 구간인지 — 보류 로그를 구간이 바뀔 때만 남기기 위한 기준."""
        p = self._entries[symbol]["params"]
        if price > p.line1:
            return "1선 위"
        if price > p.line2:
            return "1선~2선"
        if price > p.line3:
            return "2선~3선"
        return "3선 아래"

    def _log_block(self, symbol: str, text: str, price: float) -> None:
        """진입 보류 — 종목 행에 '보류' 를 띄우고, 로그는 사유·구간이 바뀔 때만 남긴다.

        틱마다 조건이 성립하므로 그대로 두면 로그가 수백 건이 된다(2026-07-29 실측:
        4종목 107건). 상태 표시는 화면이 계속 보여주고, 로그는 변화만 기록한다.
        """
        self._bus.events.put(bus.Blocked(symbol, True, text))
        if self._bot is not None:
            self._bot.set_blocked(symbol, True, text.split("—")[0].strip())
        key = f"{text.split('—')[0].strip()}|{self._price_zone(symbol, price)}"
        last_key, last_at = self._block_logged.get(symbol, ("", 0.0))
        now = time.monotonic()
        if key == last_key and now - last_at < _BLOCK_LOG_COOLDOWN_SEC:
            return
        self._block_logged[symbol] = (key, now)
        first = symbol not in self._block_notified
        self._block_notified.add(symbol)
        # 알림은 종목당 1회만 (이후 로그는 화면에만) — 묶음 경로라 종목명이 함께 나온다
        self._log(symbol, "보류", text, notify=first)

    async def _can_buy(self, symbol: str, d: Decision, price: float) -> bool:
        """예수금 방어. False 면 이번 틱만 주문을 거른다(종목은 대기 상태로 남는다).

        예전에는 1차 시점에 자금이 모자라면 당일 '종료' 시켰지만, 매도로 자금이
        회수돼도 되살아나지 못해 기회를 통째로 잃었다(2026-07-28 실측: 5종목).
        지금은 대기 상태를 유지해, 자금이 생기면 다음 틱에 같은 규칙으로 재판정된다
        (1선~2선이면 1차, 2선~3선이면 1·2차 동시, 3선 아래면 그때 종료).
        """
        deposit = await self._get_deposit()
        need = (
            d.qty * price * (1.0 + self._commission_rate)
        )  # 수수료까지 있어야 주문이 통과된다
        if deposit * _DEPOSIT_SAFETY >= need:
            return True

        e = self._entries[symbol]
        if e["pos"].state is State.WAITING:
            self._log_block(
                symbol,
                f"예수금 부족({deposit:,.0f} < {need:,.0f}) — "
                "자금이 생기면 재시도합니다",
                price,
            )
        else:  # 2차 매수 보류 — 1차 물량은 그대로 두고 손절·익절은 계속 동작
            self._log_block(
                symbol,
                f"예수금 부족({deposit:,.0f} < {need:,.0f}) — "
                "2차 매수 보류 (1차 물량 유지)",
                price,
            )
        return False

    # ── 체결통보 → 상태 확정 ────────────────────────────────────

    async def _on_fill_values(self, values: dict) -> None:
        fill = extract_fill(values)
        if fill is None:
            self._log(
                "시스템", "경고", f"체결통보 해석 실패 (필드 확인 필요): {values}"
            )
            return
        info = self._pending.get(fill.order_no)
        if info is None:
            late = self._recovered.pop(fill.order_no, None)
            if late is not None and fill.filled_qty and fill.unfilled_qty == 0:
                # 대기 해제 후에야 도착한 체결통보 — 상태는 이미 계좌 기준으로 맞췄으므로
                # 자동 반영하지 않고, 실현손익 보정이 필요함을 알린다.
                self._log(
                    late["symbol"],
                    "경고",
                    f"주문 {fill.order_no} 체결통보가 뒤늦게 도착 "
                    f"({fill.filled_qty}주 @ {fill.fill_price:,.0f}) — 대기 해제 후라 "
                    "자동 반영하지 않았습니다. 실현손익을 편집으로 보정하세요",
                )
            return  # 이 프로그램이 낸 주문이 아님 (수동 주문 등)
        if fill.filled_qty == 0 or fill.unfilled_qty > 0:
            return  # 접수/부분 체결 통보 — 완전 체결까지 대기

        self._pending.pop(fill.order_no)
        symbol, d = info["symbol"], info["decision"]
        e = self._entries[symbol]
        if fill.filled_qty != d.qty:
            self._log(
                symbol,
                "경고",
                f"체결 수량 상이: 주문 {d.qty}주 vs 체결 {fill.filled_qty}주",
            )
        self._invalidate_deposit()  # 체결로 자금이 변했으니 다음 판정은 최신값으로
        fee = self._fee(d.side, fill.fill_price * fill.filled_qty)
        filled = apply_fill(e["pos"], d, fill.fill_price, fill.filled_qty)
        if d.side is Side.BUY and not e.get(
            "high"
        ):  # 첫 체결 시점부터 MFE/MAE 추적 시작
            e["high"] = e["low"] = fill.fill_price
        e["pos"] = replace(
            filled,
            fees=filled.fees + fee,
            high_price=e.get("high") or 0.0,
            low_price=e.get("low") or 0.0,
            day_low=e.get("day_low") or 0.0,
        )
        self._store.save_transition(
            self._date,
            symbol,
            info["from_state"],
            e["pos"],
            d,
            fill.fill_price,
            info.get("trigger_price"),
        )
        self._store.update_order(
            info["order_id"],
            "체결",
            fill_price=fill.fill_price,
            fill_qty=fill.filled_qty,
            broker_order_no=fill.order_no,
        )
        self._emit_position(symbol)
        text = f"{d.reason} → 체결 {fill.filled_qty}주 @ {fill.fill_price:,.0f}"
        trigger = info.get("trigger_price")
        if trigger and fill.fill_price:  # 시장가 주문의 체결 오차
            gap = (fill.fill_price - trigger) / trigger
            if abs(gap) >= 0.001:  # 0.1% 미만은 잡음이라 표시하지 않는다
                text += f" (판정 {trigger:,.0f} 대비 {gap:+.2%})"
        if e["pos"].state is State.CLOSED:
            net = e["pos"].realized_pnl - e["pos"].fees
            text += (
                f" (실현손익 {e['pos'].realized_pnl:+,.0f} · 비용 {e['pos'].fees:,.0f} "
            )
            text += f"→ 세후 {net:+,.0f})"
        self._log(symbol, "체결", text, notify=False)
        self._notify_trade(symbol, d.reason, fill.filled_qty, fill.fill_price)
        if (
            e["pos"].state is State.CLOSED
            and e["pos"].total_bought
            and self._bot is not None
        ):
            asyncio.create_task(self._chart_task(symbol, to_discord=True))

    def _order_allowed(self, symbol: str) -> bool:
        """주문 실패 직후 같은 종목이 매 틱 재주문하는 것을 막는다.

        틱은 초당 수십 건씩 들어오므로 실패를 그대로 두면 주문·로그·알림이 폭주하고,
        '응답만 유실되고 주문은 접수된' 경우에는 중복 주문까지 될 수 있다.
        """
        if symbol in self._order_blocked:
            return False
        fail = self._order_fail.get(symbol)
        return not (fail and time.monotonic() < fail["until"])

    def _on_order_failed(self, symbol: str, err: Exception) -> None:
        fail = self._order_fail.setdefault(symbol, {"count": 0, "until": 0.0})
        fail["count"] += 1
        fail["until"] = time.monotonic() + _ORDER_FAIL_COOLDOWN_SEC
        # 재시도해도 절대 성공하지 않는 오류 — 상태가 계좌와 어긋난 것이므로 사람이 봐야 한다
        if "매도가능수량" in str(err):
            self._order_blocked.add(symbol)
            self._log(
                symbol,
                "에러",
                f"주문 실패: {err} — 프로그램이 아는 잔량과 계좌가 다릅니다. "
                "당일 이 종목 주문을 중단합니다 (편집으로 잔량을 맞춰주세요)",
            )
            return
        self._log(
            symbol,
            "에러",
            f"주문 실패({fail['count']}회): {err} — {_ORDER_FAIL_COOLDOWN_SEC}초간 재시도 보류",
        )
        if fail["count"] >= _ORDER_FAIL_BLOCK_COUNT:
            self._order_blocked.add(symbol)
            self._log(
                symbol,
                "경고",
                f"주문 연속 실패 {fail['count']}회 → 당일 이 종목 주문을 중단합니다. "
                "계좌 상태를 직접 확인하세요",
            )

    async def _check_pending(self) -> None:
        """체결통보가 오지 않는 주문을 경고하고, 더 지연되면 강제로 대기를 해제한다."""
        now = time.monotonic()
        for order_no, info in list(self._pending.items()):
            age = now - info["ts"]
            if not info["warned"] and age > _PENDING_WARN_SEC:
                info["warned"] = True
                self._log(
                    info["symbol"],
                    "경고",
                    f"주문 {order_no} 체결통보 {_PENDING_WARN_SEC}초 미도착 — "
                    f"{_PENDING_RECOVER_SEC - _PENDING_WARN_SEC:.0f}초 더 기다린 뒤 "
                    "계좌 기준으로 정리합니다 (개장 직후엔 지연될 수 있음)",
                )
            if age > _PENDING_RECOVER_SEC and not info.get("recovering"):
                info["recovering"] = True
                await self._recover_pending(order_no, info)

    async def _recover_pending(self, order_no: str, info: dict) -> None:
        """체결 대기 잠김 해제 — 계좌 실보유를 근거로 잔량을 맞추고 판정을 되살린다.

        체결통보가 유실되거나 부분 체결로 미완결이면 그 종목은 pending 에 묶여
        손절·익절 판정이 멈춘다. 하루 종일 방치되는 것이 가장 위험하므로, 일정 시간이
        지나면 **추측이 아니라 계좌 잔고를 근거로** 상태를 맞추고 대기를 푼다.
        평단·실현손익은 정확히 복원할 수 없으므로 경고를 남겨 수동 확인을 요청한다.
        """
        symbol = info["symbol"]
        e = self._entries.get(symbol)
        if e is None:
            self._pending.pop(order_no, None)
            return
        try:
            detail = await asyncio.to_thread(self._broker.holdings_detail)
        except BrokerError as err:
            info["recovering"] = False  # 다음 주기에 다시 시도
            self._log(symbol, "경고", f"체결 대기 복구용 잔고 조회 실패: {err}")
            return

        self._pending.pop(order_no, None)
        self._recovered[order_no] = info  # 늦게 체결통보가 오면 알려주기 위해 보관
        pos, d = e["pos"], info["decision"]
        held, sellable = detail.get(symbol, (0, 0))
        # 매도 주문이었다면 '매도가능수량' 이 실질 잔량이다. 보유수량은 매도 접수·체결 직후에도
        # 그대로 보여, 그 값을 믿으면 같은 물량을 다시 팔려 해 주문이 계속 거부된다.
        actual = sellable if d.side is Side.SELL else held
        try:
            if actual == 0:
                new_pos = replace(pos, state=State.CLOSED, remaining=0, pending=False)
            else:
                state = pos.state
                if state in (State.WAITING, State.CLOSED):  # 첫 매수가 걸린 경우
                    state = d.to_state
                avg = pos.avg_price or e["price"] or 0.0
                new_pos = replace(
                    pos,
                    state=state,
                    pending=False,
                    remaining=actual,
                    total_bought=max(pos.total_bought, actual),
                    avg_price=avg,
                )
        except ValueError as err:  # 계좌와 상태가 끝내 모순이면 그대로 두고 경고만
            self._log(
                symbol,
                "에러",
                f"체결 대기 복구 실패 ({err}) — 편집으로 상태를 직접 맞춰주세요",
            )
            return

        e["pos"] = new_pos
        self._store.save_position(self._date, symbol, new_pos)
        self._store.update_order(info["order_id"], "미확인")
        self._emit_position(symbol)
        basis = "매도가능수량" if d.side is Side.SELL else "보유수량"
        self._log(
            symbol,
            "경고",
            f"주문 {order_no} 체결통보 미도착 {_PENDING_RECOVER_SEC}초 경과 → "
            f"계좌 {basis}({actual}주) 기준으로 대기 해제"
            + (f" · 보유 {held}주" if d.side is Side.SELL and held != actual else "")
            + ". 평단·실현손익이 실제와 다를 수 있으니 계좌 체결 내역과 대조 후 "
            "편집으로 보정하세요",
        )

    # ── WebSocket 상태 ──────────────────────────────────────────

    async def _on_ws_status(self, msg: str) -> None:
        self._log("시스템", "연결", msg)

    async def _on_ws_reconnect(self) -> None:
        """재연결 후 공백 구간 보정: 보유·대기 종목 현재가를 REST 로 1회 조회해 재판정.

        장 운영시간 밖(새벽 서버 세션 정리 등)에는 시세 조회가 오류를 내므로 생략하고,
        실패는 종목별 알림 대신 요약 1건으로만 남긴다 (Discord 제한 방지).

        조회는 Broker 가 최소 간격을 두고 재시도까지 처리하므로 여기서는 순서대로 부르기만
        한다 (종목이 많으면 수 초가 걸리지만 그동안에도 실시간 틱 처리는 계속된다).
        """
        now = datetime.now()
        if now.weekday() >= 5 or not (dtime(8, 30) <= now.time() <= dtime(15, 40)):
            self._log("시스템", "연결", "장외 재연결 — 가격 보정 생략", notify=False)
            return
        failed: list[str] = []
        first_error = ""
        # 보유 중(손절·익절 판정이 걸린) 종목을 먼저 보정한다 — 종목이 많으면 뒤로 갈수록
        # 시간이 걸리므로, 위험을 지고 있는 포지션의 공백을 먼저 메우는 것이 안전하다.
        targets = sorted(
            (s for s, e in self._entries.items() if e["pos"].state is not State.CLOSED),
            key=lambda s: self._entries[s]["pos"].state is State.WAITING,
        )
        for symbol in targets:
            try:
                _, price = await asyncio.to_thread(self._broker.stock_info, symbol)
            except BrokerError as err:
                failed.append(symbol)
                first_error = first_error or str(err)
                continue
            if price > 0:
                await self._on_tick(Tick(symbol, price, ""))
        if failed:
            self._log(
                "시스템",
                "경고",
                f"재연결 가격 보정 실패 {len(failed)}/{len(targets)}종목 "
                f"({', '.join(failed[:5])}{' 외' if len(failed) > 5 else ''}) — {first_error}",
            )
        else:
            self._log(
                "시스템",
                "연결",
                f"재연결 가격 보정 완료 ({len(targets)}종목)",
                notify=False,
            )

    # ── Discord 봇 연동 ────────────────────────────────────────
    # 봇은 아래 속성으로 상태를 읽고, 변경은 request_* 로 명령 큐에 넣는다.
    # UI 와 완전히 같은 경로라 경쟁 상태가 생기지 않는다.

    @property
    def entries(self) -> dict:
        return self._entries

    @property
    def running(self) -> bool:
        return self._running

    @property
    def trade_date(self) -> str:
        return self._date

    @property
    def mode_real(self) -> bool:
        return self._mode_real

    @property
    def deposit_display(self) -> float | None:
        return self._deposit_display

    @property
    def notify_level(self) -> str:
        return self._notify_level

    @property
    def kiwoom_connected(self) -> bool:
        return self._broker is not None

    @property
    def account(self) -> dict[str, float]:
        """계좌 요약 — 총매입/총평가/평가손익/수익률/추정자산 (조회 실패 시 빈 dict)."""
        return self._account

    async def refresh_display(self) -> None:
        """조회 명령(/상태)용 최신화 — 주문가능금액과 계좌 요약을 함께 갱신한다."""
        if self._broker is None:
            return
        try:
            await self._get_deposit()
        except BrokerError:
            pass
        await self.refresh_account()

    async def refresh_account(self) -> dict[str, float]:
        """계좌 요약 갱신 (짧은 캐시). 대시보드·요약에서 공용으로 쓴다."""
        if self._broker is None:
            return self._account
        now = time.monotonic()
        if self._account and now - self._account_at < _ACCOUNT_TTL_SEC:
            return self._account
        try:
            self._account = await asyncio.to_thread(self._broker.account_summary)
            self._account_at = now
        except BrokerError:
            pass  # 조회 실패 시 직전 값을 유지한다 (표시 전용이라 치명적이지 않다)
        return self._account

    def find_symbol(self, text: str) -> str | None:
        """종목코드 또는 종목명 일부로 관심종목을 찾는다 (봇 명령용)."""
        text = text.strip()
        if text in self._entries:
            return text
        matches = [s for s, e in self._entries.items() if text and text in e["name"]]
        return matches[0] if len(matches) == 1 else None

    def request_running(self, on: bool) -> None:
        self._bus.commands.put(bus.SetRunning(on))

    def request_notify_level(self, level: str) -> None:
        self._bus.commands.put(bus.SetNotifyLevel(level))

    def request_daily_summary(self) -> None:
        self._bus.commands.put(bus.RequestDailySummary())

    def proximity_embed(self, trade_date: str = "") -> dict:
        """1선 근접도 조회 (봇 전용). 장중에는 메모리 값을 우선 반영한다."""
        date = trade_date or self._date
        if date == self._date:
            self._flush_day_lows(force=True)
        symbols, _ = self._store.daily_report(date)
        return build_proximity_embed(date, symbols)

    async def summary_embed(self, trade_date: str = "") -> dict:
        """지정한 매매일의 요약 embed. 과거 날짜도 조회할 수 있다."""
        date = trade_date or self._date
        symbols, fills = self._store.daily_report(date)
        if not symbols:
            return {
                "title": f"📊 {date} 매매 요약",
                "description": "그 날짜에는 등록된 관심종목이 없습니다.",
                "color": 0x616161,
            }
        if date == self._date:  # 오늘이면 계좌·주문가능금액을 최신으로
            self._flush_day_lows(force=True)
            deposit = None
            if self._broker is not None:
                try:
                    self._invalidate_deposit()
                    deposit = await self._get_deposit()
                except BrokerError:
                    deposit = None
            return build_daily_summary_embed(
                date, symbols, fills, deposit, await self.refresh_account()
            )
        return build_daily_summary_embed(date, symbols, fills)  # 과거는 기록만

    def trade_dates(self, limit: int = 10) -> list[str]:
        """최근 매매일 목록 (요약 조회 자동완성용)."""
        return self._store.recent_trade_dates(limit)

    def request_chart(self, symbol: str, to_discord: bool = False) -> None:
        self._bus.commands.put(bus.ChartRequest(symbol, to_discord=to_discord))

    async def fetch_deposit(self) -> float:
        if self._broker is None:
            raise BrokerError("키움 연결이 필요합니다")
        value = await self._get_deposit()
        self._deposit_display = value
        return value

    def on_bot_warning(self, text: str) -> None:
        """봇에서 발생한 비치명적 문제 — 화면 로그로만 남긴다."""
        self._log("시스템", "경고", text, notify=False)

    def on_bot_ready(self) -> None:
        """봇 연결 완료 — 이 시점부터 알림·명령·대시보드가 모두 동작한다."""
        self._bus.events.put(bus.DiscordStatus(True, ""))
        self._log("시스템", "연결", "Discord 연결됨 (알림·명령·대시보드)")

    async def _start_bot(self) -> None:
        """설정이 있으면 봇을 띄운다. 실패해도 매매에는 영향이 없다."""
        from trader.discord_bot import BotConfigError, TraderBot, load_bot_config

        try:
            config = load_bot_config(self._config_path)
        except BotConfigError as e:
            self._bus.events.put(bus.DiscordStatus(False, "미설정"))
            self._log(
                "시스템",
                "설정",
                f"Discord 알림을 사용하지 않습니다 ({e})",
                notify=False,
            )
            return
        bot = TraderBot(self, config)

        async def runner() -> None:
            try:
                await bot.run()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._bus.events.put(bus.DiscordStatus(False, "연결 실패"))
                self._log("시스템", "에러", f"Discord 연결 오류: {e}", notify=False)

        self._bot = bot
        self._bot_task = asyncio.create_task(runner())

    def _flush_day_lows(self, force: bool = False) -> None:
        """당일 최저가를 주기적으로 저장한다.

        진입하지 않은 종목은 상태 전이가 없어 저장될 기회가 없다. 재시작해도 근접도
        기록이 남도록 주기적으로 반영한다 (값이 바뀐 종목만 쓰므로 부담이 없다).
        force 는 감시 중지 후(요약 직전)에도 확정 저장하기 위한 것이다.
        """
        if not (force or self._running):
            return
        now = time.monotonic()
        if not force and now - self._day_low_at < _DAY_LOW_FLUSH_SEC:
            return
        self._day_low_at = now
        for symbol, e in self._entries.items():
            low = e.get("day_low") or 0.0
            pos = e["pos"]
            if low and pos.day_low != low:
                e["pos"] = replace(pos, day_low=low)
                self._store.save_position(self._date, symbol, e["pos"])

    async def _tick_auto_connect(self) -> None:
        """자동 연결 — 실패해도 주기적으로 다시 시도한다.

        증권사 서버 점검처럼 일시적인 사유로 실패할 수 있으므로 한 번 실패하고 포기하지
        않는다. 다만 알림은 처음 한 번만 보내 실패가 반복될 때 채널이 시끄럽지 않게 한다.
        """
        if not self._auto_connect:
            return
        if self._broker is not None:
            return
        now = time.monotonic()
        first = self._auto_connect_at is None
        if not first and now - self._auto_connect_at < _AUTO_CONNECT_RETRY_SEC:
            return
        self._auto_connect_at = now
        if self._broker is None:
            await self._connect(quiet=not first and self._auto_connect_warned)
            if self._broker is None:
                self._auto_connect_warned = True

    async def _tick_bot(self) -> None:
        """대시보드 갱신 — 감시 중에는 주기적으로, 중지되면 걷어낸다."""
        if self._bot is None:
            return
        now = time.monotonic()
        if now - self._bot_refresh_at < _BOT_REFRESH_SEC:
            return
        self._bot_refresh_at = now
        try:
            if self._running:
                await self.refresh_account()
                await self._bot.refresh_dashboard()
            else:
                await self._bot.clear_dashboard()
        except Exception as e:  # noqa: BLE001 — 대시보드 실패가 매매를 막지 않게
            self._log("시스템", "경고", f"대시보드 갱신 실패: {e}", notify=False)

    # ── 복기 차트 ───────────────────────────────────────────────

    async def _chart_task(
        self, symbol: str, to_ui: bool = False, to_discord: bool = False
    ) -> None:
        """차트 생성은 느리므로(REST 2~3회 + 렌더링 1~3초) 전부 스레드에 위임한다.
        매매 루프·틱 판정은 이 작업과 무관하게 계속 돈다."""
        if symbol in self._chart_busy:
            return
        self._chart_busy.add(symbol)
        # SQLite 는 스레드 전용이므로 DB 접근(체결 내역)은 여기 코어 스레드에서 끝내고,
        # 워커 스레드에는 순수 데이터만 넘긴다.
        _, fills_all = self._store.daily_report(self._date)
        fills = [
            (f["ts"], f["side"], f["price"]) for f in fills_all if f["symbol"] == symbol
        ]
        try:
            daily_path, minute_path, kospi_error = await asyncio.to_thread(
                self._build_charts, symbol, fills
            )
        except Exception as e:  # noqa: BLE001 — 차트 실패가 매매에 영향 주지 않게
            self._log(symbol, "에러", f"차트 생성 실패: {e}")
            return
        finally:
            self._chart_busy.discard(symbol)
        if kospi_error:
            self._log(
                "시스템",
                "경고",
                f"KOSPI 지수 조회 실패 (패널 생략): {kospi_error}",
                notify=False,
            )
        if to_ui:
            self._bus.events.put(
                bus.ChartReady(
                    symbol, self._entries[symbol]["name"], daily_path, minute_path
                )
            )
        if to_discord and self._bot is not None:
            await self._send_chart_images(symbol, (daily_path, minute_path))

    def _build_charts(
        self, symbol: str, fills_raw: list[tuple[str, str, float]]
    ) -> tuple[str, str, str]:
        """(워커 스레드에서 실행) REST 조회 → 일봉·3분봉 PNG 생성.

        이 함수는 store 나 이벤트 로그를 절대 건드리지 않는다 — SQLite 는 스레드 전용이라
        DB 접근은 호출부(코어 스레드)가 끝내고 순수 데이터만 넘긴다.
        반환: (일봉 경로, 분봉 경로, KOSPI 실패 사유 또는 "").
        """
        from trader.chart import Bar, Fill, render_daily, render_minute

        e = self._entries[symbol]
        params, pos = e["params"], e["pos"]
        broker = self._broker
        if broker is None:
            raise RuntimeError("키움 연결이 끊겼습니다")

        daily = [Bar(*row) for row in broker.daily_chart(symbol)]
        minute_all = [Bar(*row) for row in broker.minute_chart(symbol)]
        kospi_error = ""
        try:
            kospi = [Bar(*row) for row in broker.index_daily()]
        except BrokerError as err:  # 지수 TR 미확인 대비 — KOSPI 패널만 생략
            kospi, kospi_error = None, str(err)
        if not daily or not minute_all:
            raise RuntimeError(
                "차트 데이터가 비어 있습니다 (장 시작 전이거나 조회 실패)"
            )

        fills = [Fill(ts, side, price) for ts, side, price in fills_raw]

        # 3분봉 범위: 당일(약 130봉) + 직전 여분 8봉이 기본. 봉이 많으면 3:4 화면에서
        # 캔들이 뭉개지므로, 진입 후 며칠 보유한 이월 종목만 진입일부터 전부 보여준다.
        entry_day = (
            fills[0].ts[:10].replace("-", "") if fills else self._date.replace("-", "")
        )
        # 최소 2거래일을 보여준다 — 당일만 그리면 짧게 보유한 종목은 봉이 10여 개뿐이라
        # 캔들이 지나치게 커진다(2026-07-28 피드백). 이월 종목은 진입일부터.
        days = sorted({b.key[:8] for b in minute_all})
        base_day = days[-2] if len(days) >= 2 else days[0]
        start_day = min(entry_day, base_day)
        start_idx = next(
            (i for i, b in enumerate(minute_all) if b.key[:8] >= start_day), 0
        )
        minute = minute_all[max(0, start_idx - 8) :]

        lines = (params.line1, params.line2, params.line3)
        net = pos.realized_pnl - pos.fees
        title = f"{e['name']}({symbol}) 일봉"
        if pos.total_bought:
            title += f" · {pos.state.value} · 세후 {net:+,.0f}원"
            if pos.avg_price and pos.high_price:
                title += (
                    f" · 최고 {(pos.high_price - pos.avg_price) / pos.avg_price:+.1%}"
                    f"/최저 {(pos.low_price - pos.avg_price) / pos.avg_price:+.1%}"
                )

        out_dir = Path(self._db_dir) / "charts" / self._date
        font = _load_chart_font(self._config_path)
        daily_path = render_daily(
            out_dir / f"{symbol}-daily.png",
            title,
            daily,
            lines,
            fills,
            kospi=kospi,
            font=font,
        )
        minute_path = render_minute(
            out_dir / f"{symbol}-minute.png",
            f"{e['name']}({symbol}) 3분봉",
            minute,
            lines,
            fills,
            font=font,
        )
        return daily_path, minute_path, kospi_error

    async def _send_chart_images(self, symbol: str, paths: tuple[str, ...]) -> None:
        name = self._entries[symbol]["name"] if symbol in self._entries else symbol
        try:  # 일봉·3분봉을 한 메시지로 (요청 1회, 사진 2장 나란히)
            await self._bot.send_images(list(paths), f"📈 {name}({symbol}) 차트")
        except Exception as e:  # noqa: BLE001
            self._log(symbol, "경고", f"차트 전송 실패: {e}", notify=False)

    # ── 자동 스케줄 ─────────────────────────────────────────────

    def _sched_last(self, key: str) -> str:
        """해당 스케줄 항목을 마지막으로 실행한 날짜 (DB 영속)."""
        if key not in self._sched_done:
            self._sched_done[key] = self._store.get_setting(f"sched_{key}", "")
        return self._sched_done[key]

    def _sched_mark(self, key: str, today: str) -> None:
        self._sched_done[key] = today
        self._store.set_setting(f"sched_{key}", today)

    async def _check_schedule(self) -> None:
        if not self._schedule.get("enabled"):
            return
        now = datetime.now()
        if now.weekday() >= 5:  # 주말
            return
        today, t = now.date().isoformat(), now.time()

        if (
            self._sched_last("start") != today
            and self._schedule["start"] <= t < self._schedule["stop"]
        ):
            self._sched_mark("start", today)
            await self._auto_start(today)

        if (
            self._sched_last("stop") != today
            and t >= self._schedule["stop"]
            and self._running
        ):
            self._sched_mark("stop", today)
            self._running = False
            self._bus.events.put(bus.WatchStatus(False))
            self._log("시스템", "감시", "자동 스케줄 — 감시 중지")

        if self._sched_last("summary") != today and t >= self._schedule["summary"]:
            self._sched_mark("summary", today)
            await self.send_daily_summary()

        if (
            self._sched_last("summary2") != today
            and t >= self._schedule["summary2"]
            and self._date == today
        ):
            self._sched_mark("summary2", today)
            await self._review_after_hours()

    async def _auto_start(self, today: str) -> None:
        """무인 운용: 필요한 연결까지 스스로 하고 감시를 시작한다."""
        if self._running:
            return
        if self._date != today:  # 지난 날짜 리스트로 매매하는 사고 방지
            self._log(
                "시스템",
                "경고",
                f"자동 시작 취소 — 화면의 매매일({self._date})이 오늘이 아닙니다",
            )
            return
        if self._broker is None:
            await self._connect()
        if self._broker is None:
            self._log("시스템", "에러", "자동 시작 실패 — 키움 연결에 실패했습니다")
            return
        self._running = True
        self._bus.events.put(bus.WatchStatus(True))
        self._log(
            "시스템", "감시", f"자동 스케줄 — 감시 시작 ({len(self._entries)}종목)"
        )

    async def _review_after_hours(self) -> None:
        """20:05 시간외 리뷰 — 대체거래소(NXT) 거래가 실제로 있었던 종목만 3분봉 재전송.

        NXT 상장 목록을 관리하는 대신, 분봉을 다시 조회해 15:30 이후 봉이 존재하는지로
        판별한다 (데이터가 직접 알려주는 방식). 감시(WebSocket)와 무관한 REST 조회라
        장 마감 후에도 동작한다.
        """
        if self._broker is None or self._bot is None:
            self._log(
                "시스템",
                "설정",
                "시간외 리뷰 생략 — 키움/Discord 연결 없음",
                notify=False,
            )
            return
        symbols, _ = self._store.daily_report(self._date)
        traded = [s["symbol"] for s in symbols if s["total_bought"] > 0]
        today = self._date.replace("-", "")
        reviewed = 0
        for symbol in traded:
            if symbol not in self._entries:
                continue
            try:
                rows = await asyncio.to_thread(self._broker.minute_chart, symbol)
            except BrokerError as err:
                self._log(symbol, "경고", f"시간외 확인 실패: {err}", notify=False)
                continue
            has_after_hours = any(
                r[0][:8] == today and r[0][8:12] > "1530" for r in rows
            )
            if not has_after_hours:
                continue
            _, fills_all = self._store.daily_report(self._date)
            fills = [
                (f["ts"], f["side"], f["price"])
                for f in fills_all
                if f["symbol"] == symbol
            ]
            try:
                _, minute_path, _ = await asyncio.to_thread(
                    self._build_charts, symbol, fills
                )
            except Exception as e:  # noqa: BLE001
                self._log(symbol, "에러", f"시간외 차트 생성 실패: {e}")
                continue
            name = self._entries[symbol]["name"]
            try:
                await self._bot.send_images(
                    [minute_path], f"🌙 {name}({symbol}) 시간외(NXT) 포함 3분봉"
                )
                reviewed += 1
            except Exception as e:  # noqa: BLE001
                self._log(symbol, "경고", f"시간외 차트 전송 실패: {e}", notify=False)
        self._log(
            "시스템",
            "요약",
            f"시간외 리뷰 완료 — 재전송 {reviewed}종목 / 확인 {len(traded)}종목",
            notify=False,
        )

    async def send_daily_summary(self) -> None:
        """하루 매매 요약을 Discord 로 발송한다 (알림 수준과 무관하게 항상 발송)."""
        symbols, fills = self._store.daily_report(self._date)
        deposit = None
        if self._broker is not None:
            try:
                # _get_deposit 을 쓰면 표시값(_deposit_display)이 함께 갱신되어
                # 요약과 /상태·대시보드가 같은 숫자를 보여준다.
                self._invalidate_deposit()  # 요약은 마감 시점 최신값이어야 한다
                deposit = await self._get_deposit()
            except BrokerError:
                deposit = None
        self._flush_day_lows(force=True)  # 요약 직전에 최신 근접도를 확정 저장
        account = await self.refresh_account()
        embed = build_daily_summary_embed(self._date, symbols, fills, deposit, account)
        holding = [s for s in symbols if s["total_bought"] > 0 and s["state"] != "종료"]
        self._log(
            "시스템",
            "요약",
            f"일일 요약 — 체결 {len(fills)}건"
            + (f", 이월 필요 {len(holding)}종목" if holding else ""),
            notify=False,
        )
        if self._bot is not None:  # 요약이 나갔으니 장중 대시보드는 걷어낸다
            await self._bot.clear_dashboard()
        await self._send_embed(embed)

    # ── 상태 로드 / 발행 ────────────────────────────────────────

    def _load_date(self, trade_date: str) -> None:
        self._date = trade_date
        self._entries = {}
        self._block_logged.clear()
        self._block_notified.clear()
        for symbol, (name, params, pos, memo) in self._store.load_all(
            trade_date
        ).items():
            self._entries[symbol] = {
                "name": name,
                "params": params,
                "pos": pos,
                "price": pos.avg_price,
                "memo": memo,
                "high": pos.high_price,
                "low": pos.low_price,
                "day_low": pos.day_low,
            }

    def _warn_restored_pending(self) -> None:
        """체결 확인 전 크래시로 pending 인 채 복원된 종목 경고."""
        for symbol, e in self._entries.items():
            if e["pos"].pending:
                self._log(
                    symbol,
                    "경고",
                    "체결 대기 중 종료된 포지션 복원 — 계좌 체결 내역과 대조 후 "
                    "필요 시 편집으로 상태를 바로잡으세요",
                )

    def _replay_logs(self) -> None:
        """해당 매매일의 저장된 로그를 화면에 복원한다 (재시작·날짜 전환 대비)."""
        for ts, symbol, kind, text in self._store.recent_events(self._date):
            self._bus.events.put(bus.LogLine(ts, symbol, kind, text))

    def _emit_date_loaded(self) -> None:
        self._bus.events.put(bus.TradeDate(self._date))
        for symbol in self._entries:
            self._emit_position(symbol)

    def _emit_position(self, symbol: str) -> None:
        e = self._entries[symbol]
        self._bus.events.put(
            bus.PositionUpdate(
                symbol, e["name"], e["pos"], e["params"], e.get("memo", "")
            )
        )

    def _apply_globals_to_waiting(self, b1, b2, rates, ratios) -> None:
        """진입 전('대기') 종목에 새 전역 설정을 즉시 반영한다. 보유 중 종목은 진입 시점 값 유지."""
        updated = 0
        for symbol, e in self._entries.items():
            if e["pos"].state is not State.WAITING:
                continue
            try:
                e["params"] = replace(
                    e["params"],
                    buy1_amount=b1,
                    buy2_amount=b2,
                    tp_rates=rates,
                    tp_ratios=ratios,
                )
            except ValueError as err:  # 예: 금액 < 기준선
                self._log(symbol, "에러", f"새 전역 설정 적용 불가: {err}")
                continue
            self._store.register_symbol(
                self._date,
                symbol,
                e["name"],
                e["params"],
                e["pos"],
                memo=e.get("memo", ""),
            )
            self._emit_position(symbol)
            updated += 1
        if updated:
            self._log(
                "시스템", "설정", f"대기 종목 {updated}개에 새 매수 금액·익절 설정 반영"
            )
        small = [
            e["name"]
            for e in self._entries.values()
            if e["pos"].state is State.WAITING
            and self._entry_qty(e["params"]) < _MIN_ENTRY_QTY
        ]
        if small:
            self._log(
                "시스템",
                "경고",
                f"1차 수량이 {_MIN_ENTRY_QTY}주 미만인 종목 {len(small)}개: "
                f"{', '.join(small[:5])}{' 외' if len(small) > 5 else ''} "
                "— 단계 익절이 어렵습니다",
            )

    def _emit_funds(self) -> None:
        g = self._store.get_setting
        total = float(g("funds_total", "10000000"))
        max_n = int(g("funds_max", "10"))
        per_half = total / max_n / 2
        rates = tuple(float(x) for x in g("funds_rates", "0.03,0.05,0.07").split(","))
        ratios = tuple(float(x) for x in g("funds_ratios", "0.4,0.5,0.1").split(","))
        self._bus.events.put(
            bus.Funds(
                total,
                max_n,
                float(g("funds_buy1", str(per_half))),
                float(g("funds_buy2", str(per_half))),
                rates,
                ratios,
            )
        )

    def _log(self, symbol: str, kind: str, text: str, notify: bool = True) -> None:
        self._store.log(self._date, symbol, kind, text)
        self._bus.events.put(bus.LogLine(_now(), symbol, kind, text))
        if not (
            notify and self._bot and should_notify(self._notify_level, symbol, kind)
        ):
            return
        if kind in _BATCH_KINDS:  # 여러 건이 몰리는 정보성 알림 → 모아서 한 장으로
            name = self._entries.get(symbol, {}).get("name", "")
            label = f"{name}({symbol})" if name and symbol != "시스템" else symbol
            self._notice_batch.append((kind, label, text))
            self._notice_at = time.monotonic()
            if len(self._notice_batch) >= _BATCH_MAX:
                asyncio.create_task(self._flush_notices())
            return
        asyncio.create_task(self._send_discord(format_message(symbol, kind, text)))

    async def _flush_notices(self) -> None:
        """모아둔 정보성 알림을 발송. 1건이면 줄글, 2건 이상이면 embed 한 장."""
        items, self._notice_batch = self._notice_batch, []
        if not items or self._bot is None:
            return
        if len(items) == 1:  # 단건도 색 띠가 붙는 embed 로 — 줄글은 흐름에 묻힌다
            kind, label, text = items[0]
            await self._send_embed(build_alert_embed(kind, label, text))
            return
        await self._send_embed(build_batch_embed(items))

    def _notify(self, symbol: str, text: str) -> None:
        """중요 이벤트 — '알림' 종류로 기록되며 알림 수준 필터를 거쳐 Discord 로 발송된다."""
        self._log(symbol, "알림", text)
