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

from trader.broker import Broker, BrokerError, extract_fill
from trader.kiwoom import KiwoomAuthError, load_auth
from trader.notifier import (
    DiscordNotifier,
    format_message,
    format_trade,
    load_webhook,
    should_notify,
)
from dataclasses import replace

from trader.state_machine import (
    Decision,
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

_PENDING_WARN_SEC = 60  # 체결통보 미도착 경고 기준
_LOOP_SEC = 0.1


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
        self,
        b: bus.Bus,
        db_path: str = "data/trader.db",
        config_path: str = "config.toml",
    ):
        self._bus = b
        self._db_path = db_path
        self._config_path = config_path
        self._store: Store | None = None
        self._broker: Broker | None = None
        self._notifier: DiscordNotifier | None = None
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
        self._buy2_blocked: set[str] = set()

    # ── 메인 루프 ───────────────────────────────────────────────

    async def run(self) -> None:
        self._store = Store(self._db_path)
        self._mode_real = self._store.get_setting("mode", "모의") == "실전"
        self._notify_level = self._store.get_setting("notify_level", "전체")
        self._max_symbols = int(self._store.get_setting("funds_max", "10"))
        self._load_date(self._date)
        self._emit_date_loaded()
        self._replay_logs()
        self._emit_funds()
        self._bus.events.put(bus.Mode(self._mode_real))
        self._bus.events.put(
            bus.NotifyLevel(self._store.get_setting("notify_level", "전체"))
        )
        self._bus.events.put(bus.WatchStatus(False))
        self._log(
            "시스템",
            "시작",
            f"코어 시작 · 매매일 {self._date} ({len(self._entries)}종목 복원)",
        )
        self._warn_restored_pending()

        while True:
            self._drain_commands_sync_part()
            await self._drain_commands()
            self._check_pending_timeout()
            await asyncio.sleep(_LOOP_SEC)

    def _drain_commands_sync_part(self) -> None:
        pass  # 자리 유지용 (명령 처리는 전부 async 경로)

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
            case bus.ConnectDiscord():
                await self._connect_discord()
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
                }
                self._buy2_blocked.discard(s)
                self._emit_position(s)
                self._log(
                    s,
                    "등록" if not edit else "편집",
                    f"{n} (상태: {pos.state.value}, 잔량 {pos.remaining}주)",
                )
                await self._sync_watcher_symbols()
            case bus.Delete(symbol=s):
                if self._running:
                    self._log(
                        s, "에러", "감시 중에는 삭제할 수 없습니다 — 먼저 중지하세요"
                    )
                    return
                self._store.delete_symbol(self._date, s)
                self._entries.pop(s, None)
                self._buy2_blocked.discard(s)
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
                    self._buy2_blocked.discard(s)
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
                self._store.set_setting("mode", "실전" if real else "모의")
                self._mode_real = real
                self._bus.events.put(bus.Mode(real))
                await self._disconnect("모드 전환 — 다시 연결하세요")
                self._log(
                    "시스템", "설정", f"{'실전' if real else '모의'}투자 모드로 전환"
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

    async def _connect(self) -> None:
        try:
            auth = load_auth(self._config_path, real=self._mode_real)
            await asyncio.to_thread(
                auth.token
            )  # 잘못된 키/네트워크 오류는 여기서 드러남 (10초 타임아웃)
        except Exception as e:  # noqa: BLE001
            self._bus.events.put(bus.KiwoomStatus(False, "연결 실패"))
            self._log("시스템", "에러", f"키움 연결 실패: {e}")
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

    async def _connect_discord(self) -> None:
        try:
            notifier = DiscordNotifier(load_webhook(self._config_path))
            await asyncio.to_thread(
                notifier.send, "🔔 three-line-trader 연결되었습니다"
            )
        except Exception as e:  # noqa: BLE001
            self._bus.events.put(bus.DiscordStatus(False, "연결 실패"))
            self._log("시스템", "에러", f"Discord 연결 실패: {e}", notify=False)
            return
        self._notifier = notifier
        self._bus.events.put(bus.DiscordStatus(True, ""))
        self._log("시스템", "연결", f"Discord 연결됨 (알림 수준: {self._notify_level})")

    def _notify_trade(self, symbol: str, reason: str, qty: int, price: float) -> None:
        """체결 확정·종료 전이 시 사용자 친화 요약을 Discord 로 발송한다."""
        if not (self._notifier and should_notify(self._notify_level, symbol, "체결")):
            return
        e = self._entries[symbol]
        pos = e["pos"]
        pnl = (
            pos.realized_pnl if pos.state is State.CLOSED and pos.total_bought else None
        )
        asyncio.create_task(
            self._send_discord(format_trade(e["name"], symbol, reason, qty, price, pnl))
        )

    async def _send_discord(self, text: str) -> None:
        try:
            await asyncio.to_thread(self._notifier.send, text)
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
        self._bus.events.put(bus.Tick(tick.symbol, tick.price))
        if not self._running:
            return  # 감시 중지 상태: 시세 표시만
        d = decide(e["pos"], e["params"], tick.price)
        if d is None:
            return
        await self._execute(tick.symbol, d, tick.price)

    async def _execute(self, symbol: str, d: Decision, price: float) -> None:
        e = self._entries[symbol]
        pos, from_state = e["pos"], e["pos"].state

        if d.side is None:  # 주문 없는 즉시 전이 (진입 금지 종료, 수량 0 익절 등)
            e["pos"] = apply_transition(pos, d)
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
                nd = Decision(
                    State.CLOSED,
                    None,
                    0,
                    f"최대 종목 수({self._max_symbols}) 도달 → 진입 금지, 당일 종료",
                )
                await self._execute(symbol, nd, price)
                return

        if d.side is Side.BUY and not await self._can_buy(symbol, d, price):
            return

        try:
            order_fn = self._broker.buy if d.side is Side.BUY else self._broker.sell
            order_no = await asyncio.to_thread(order_fn, symbol, d.qty)
        except BrokerError as err:
            self._log(symbol, "에러", f"주문 실패: {err}")
            return
        order_id = self._store.record_order(symbol, d.side.value, d.qty)
        e["pos"] = mark_pending(pos)
        self._store.save_position(self._date, symbol, e["pos"])
        self._pending[order_no] = {
            "symbol": symbol,
            "from_state": from_state,
            "decision": d,
            "order_id": order_id,
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

    async def _can_buy(self, symbol: str, d: Decision, price: float) -> bool:
        """예수금 방어. False 면 주문을 내지 않는다."""
        e = self._entries[symbol]
        is_first_entry = e["pos"].state is State.WAITING  # 1차 또는 갭 동시 매수
        if not is_first_entry and symbol in self._buy2_blocked:
            return False  # 이미 차단·알림된 종목 (틱마다 REST 호출 방지)

        deposit = await asyncio.to_thread(self._broker.deposit)
        need = d.qty * price
        if deposit >= need:
            return True

        if is_first_entry:  # 1차 시점 부족 → 주문 없이 당일 종료
            nd = Decision(
                State.CLOSED,
                None,
                0,
                f"예수금 부족({deposit:,.0f} < {need:,.0f}) → 진입 금지, 당일 종료",
            )
            await self._execute(symbol, nd, price)
        else:  # 2차 시점 부족 → 1차 물량 유지, 추가 매수만 차단 (1회 알림)
            self._buy2_blocked.add(symbol)
            self._log(
                symbol,
                "에러",
                f"예수금 부족({deposit:,.0f} < {need:,.0f}) → 2차 매수 차단, "
                "1차 물량 유지 (손절·익절은 계속 동작)",
            )
        self._notify(symbol, "예수금 부족 발생 — 확인 필요")
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
        e["pos"] = apply_fill(e["pos"], d, fill.fill_price, fill.filled_qty)
        self._store.save_transition(
            self._date, symbol, info["from_state"], e["pos"], d, fill.fill_price
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
        if e["pos"].state is State.CLOSED:
            text += f" (실현손익 {e['pos'].realized_pnl:+,.0f})"
        self._log(symbol, "체결", text, notify=False)
        self._notify_trade(symbol, d.reason, fill.filled_qty, fill.fill_price)

    def _check_pending_timeout(self) -> None:
        for order_no, info in self._pending.items():
            if not info["warned"] and time.monotonic() - info["ts"] > _PENDING_WARN_SEC:
                info["warned"] = True
                self._log(
                    info["symbol"],
                    "경고",
                    f"주문 {order_no} 체결통보 {_PENDING_WARN_SEC}초 미도착 — 수동 확인 필요",
                )

    # ── WebSocket 상태 ──────────────────────────────────────────

    async def _on_ws_status(self, msg: str) -> None:
        self._log("시스템", "연결", msg)

    async def _on_ws_reconnect(self) -> None:
        """재연결 후 공백 구간 보정: 보유·대기 종목 현재가를 REST 로 1회 조회해 재판정.

        장 운영시간 밖(새벽 서버 세션 정리 등)에는 시세 조회가 오류를 내므로 생략하고,
        실패는 종목별 알림 대신 요약 1건으로만 남긴다 (Discord 제한 방지).
        """
        now = datetime.now()
        if now.weekday() >= 5 or not (dtime(8, 30) <= now.time() <= dtime(15, 40)):
            self._log("시스템", "연결", "장외 재연결 — 가격 보정 생략", notify=False)
            return
        failed: list[str] = []
        for symbol, e in list(self._entries.items()):
            if e["pos"].state is State.CLOSED:
                continue
            try:
                _, price = await asyncio.to_thread(self._broker.stock_info, symbol)
            except BrokerError:
                failed.append(symbol)
                continue
            if price > 0:
                await self._on_tick(Tick(symbol, price, ""))
        if failed:
            self._log(
                "시스템",
                "경고",
                f"재연결 가격 보정 실패 {len(failed)}종목: {', '.join(failed)}",
            )

    # ── 상태 로드 / 발행 ────────────────────────────────────────

    def _load_date(self, trade_date: str) -> None:
        self._date = trade_date
        self._entries = {}
        self._buy2_blocked = set()
        for symbol, (name, params, pos, memo) in self._store.load_all(
            trade_date
        ).items():
            self._entries[symbol] = {
                "name": name,
                "params": params,
                "pos": pos,
                "price": pos.avg_price,
                "memo": memo,
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
        if (
            notify
            and self._notifier
            and should_notify(self._notify_level, symbol, kind)
        ):
            asyncio.create_task(self._send_discord(format_message(symbol, kind, text)))

    def _notify(self, symbol: str, text: str) -> None:
        """중요 이벤트 — '알림' 종류로 기록되며 알림 수준 필터를 거쳐 Discord 로 발송된다."""
        self._log(symbol, "알림", text)
