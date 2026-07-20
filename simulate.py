"""UI 개발용 시뮬레이터 — 키움 API 없이 가짜 틱으로 전체 흐름을 돌려본다.

    uv run simulate.py

랜덤워크 가격을 만들어 실제 상태 머신·저장소·UI 를 그대로 구동한다.
주문은 지시 가격에 즉시 전량 체결된다고 가정한다.
이 파일의 SimCore.loop 는 향후 실제 코어(watcher+broker 연동)의
처리 순서를 보여주는 참조 구현이기도 하다.

DB 는 실전과 분리된 data/simulator.db 를 사용한다.
"""

from __future__ import annotations

import random
import threading
import time
from datetime import date, datetime

from dataclasses import replace

from trader.state_machine import (
    Side,
    State,
    apply_fill,
    apply_transition,
    decide,
    mark_pending,
)
from trader.notifier import DiscordNotifier, format_message, load_webhook, should_notify
from trader.store import Store
from trader.ui import bus

_TICK_INTERVAL = 0.2  # 초
_VOLATILITY = 0.003  # 틱당 표준편차 0.3%


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class SimCore:
    """가짜 시세 + 실제 상태 머신/저장소. 백그라운드 스레드에서 loop() 실행."""

    def __init__(self, b: bus.Bus, store: Store):
        self._bus = b
        self._store = store
        self._running = False
        self._date = date.today().isoformat()  # 활성 매매일
        self._notifier: DiscordNotifier | None = None
        self._notify_level = store.get_setting("notify_level", "전체")
        # symbol -> {"name", "params", "pos", "price"}
        self._entries: dict[str, dict] = {}
        self._load_date(self._date)

    def _load_date(self, trade_date: str) -> None:
        """해당 매매일의 관심종목 리스트를 로드한다."""
        self._date = trade_date
        self._entries = {}
        for symbol, (name, params, pos) in self._store.load_all(trade_date).items():
            price = pos.avg_price if pos.avg_price else params.line1 * 1.03
            self._entries[symbol] = {
                "name": name,
                "params": params,
                "pos": pos,
                "price": price,
            }

    def loop(self) -> None:
        # 복원된 종목을 UI 에 초기 표시
        self._emit_date_loaded()
        self._bus.events.put(bus.WatchStatus(self._running))
        self._emit_funds()
        self._bus.events.put(
            bus.Mode(self._store.get_setting("mode", "모의") == "실전")
        )
        self._bus.events.put(
            bus.NotifyLevel(self._store.get_setting("notify_level", "전체"))
        )

        while True:
            self._handle_commands()
            if self._running:
                for symbol in list(self._entries):
                    self._step(symbol)
            time.sleep(_TICK_INTERVAL)

    # ── 명령 처리 (UI → 코어) ───────────────────────────────────

    def _emit_date_loaded(self) -> None:
        """매매일 확정 통지 후 해당 날짜의 전 종목을 UI 로 발행."""
        self._bus.events.put(bus.TradeDate(self._date))
        for symbol in self._entries:
            self._emit_position(symbol)

    def _handle_commands(self) -> None:
        while not self._bus.commands.empty():
            cmd = self._bus.commands.get_nowait()
            match cmd:
                case bus.ConnectKiwoom():
                    self._bus.events.put(bus.KiwoomStatus(True, "시뮬레이션"))
                    self._bus.events.put(bus.Account(10_000_000))
                    self._log(
                        "시스템", "연결", "키움 연결 (시뮬레이션 — 실제 접속 없음)"
                    )
                case bus.RefreshAccount():
                    self._bus.events.put(bus.Account(10_000_000))
                case bus.LookupSymbol(symbol=s):
                    self._bus.events.put(bus.SymbolInfo(s, f"시뮬종목{s[-2:]}"))
                case bus.ConnectDiscord():
                    try:
                        notifier = DiscordNotifier(load_webhook())
                        notifier.send(
                            "🔔 three-line-trader 연결되었습니다 (시뮬레이터)"
                        )
                    except Exception as e:  # noqa: BLE001
                        self._bus.events.put(bus.DiscordStatus(False, "연결 실패"))
                        self._log(
                            "시스템", "에러", f"Discord 연결 실패: {e}", notify=False
                        )
                    else:
                        self._notifier = notifier
                        self._bus.events.put(bus.DiscordStatus(True, ""))
                        self._log(
                            "시스템",
                            "연결",
                            f"Discord 연결됨 (알림 수준: {self._notify_level})",
                        )
                case bus.SetNotifyLevel(level=lv):
                    self._notify_level = lv
                    self._store.set_setting("notify_level", lv)
                    self._bus.events.put(bus.NotifyLevel(lv))
                    self._log("시스템", "설정", f"Discord 알림 수준: {lv}")
                case bus.SetTradeDate(date=d):
                    if self._running:
                        self._log(
                            "시스템", "에러", "감시 중에는 매매일을 전환할 수 없습니다"
                        )
                        continue
                    self._load_date(d)
                    self._emit_date_loaded()
                    self._log(
                        "시스템",
                        "설정",
                        f"매매일 {d} 리스트 로드 ({len(self._entries)}종목)",
                    )
                case bus.Register(symbol=s, name=n, params=p, position=pos):
                    if self._running:
                        self._log(
                            s,
                            "에러",
                            "감시 중에는 등록/편집할 수 없습니다 — 먼저 중지하세요",
                        )
                        continue
                    if pos is None:  # 편집: 현재 포지션 유지, 설정만 교체
                        pos = self._entries[s]["pos"] if s in self._entries else None
                    if pos is None:
                        self._log(s, "에러", "편집 대상 종목이 없습니다")
                        continue
                    self._store.register_symbol(self._date, s, n, p, pos)
                    price = (
                        self._entries[s]["price"]
                        if s in self._entries
                        else (pos.avg_price if pos.avg_price else p.line1 * 1.03)
                    )
                    self._entries[s] = {
                        "name": n,
                        "params": p,
                        "pos": pos,
                        "price": price,
                    }
                    self._emit_position(s)
                    self._log(s, "등록", f"{n} (상태: {pos.state.value})")
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
                        continue
                    for key, val in (
                        ("funds_total", t),
                        ("funds_max", m),
                        ("funds_buy1", b1),
                        ("funds_buy2", b2),
                        ("funds_rates", ",".join(map(str, rates))),
                        ("funds_ratios", ",".join(map(str, ratios))),
                    ):
                        self._store.set_setting(key, str(val))
                    self._emit_funds()
                    for s, e in self._entries.items():  # 대기 종목에 즉시 반영
                        if e["pos"].state is State.WAITING:
                            e["params"] = replace(
                                e["params"],
                                buy1_amount=b1,
                                buy2_amount=b2,
                                tp_rates=rates,
                                tp_ratios=ratios,
                            )
                            self._store.register_symbol(
                                self._date, s, e["name"], e["params"], e["pos"]
                            )
                            self._emit_position(s)
                    self._log(
                        "시스템",
                        "설정",
                        f"전역 설정 적용: 총 {t:,.0f} / {m}종목 / 1차 {b1:,.0f} / 2차 {b2:,.0f}",
                    )
                case bus.SetMode(real=real):
                    self._store.set_setting("mode", "실전" if real else "모의")
                    self._bus.events.put(bus.Mode(real))
                    self._log(
                        "시스템",
                        "설정",
                        f"{'실전' if real else '모의'}투자 모드로 전환",
                    )
                case bus.Delete(symbol=s):
                    if self._running:
                        self._log(
                            s,
                            "에러",
                            "감시 중에는 삭제할 수 없습니다 — 먼저 중지하세요",
                        )
                        continue
                    self._store.delete_symbol(self._date, s)
                    self._entries.pop(s, None)
                    self._bus.events.put(bus.SymbolRemoved(s))
                    self._log(s, "삭제", "관심종목 제외")
                case bus.Reset(symbol=s) if s in self._entries:
                    try:
                        new_pos = self._store.admin_reset(
                            self._date, s, self._entries[s]["pos"]
                        )
                    except ValueError as err:
                        self._log(s, "에러", str(err))
                    else:
                        self._entries[s]["pos"] = new_pos
                        self._emit_position(s)
                        self._log(s, "리셋", "관리자 수동 초기화 (종료 → 대기)")
                case bus.SetRunning(running=r):
                    self._running = r
                    self._bus.events.put(bus.WatchStatus(r))

    # ── 틱 1회 처리: 실제 코어와 동일한 순서 ────────────────────

    def _step(self, symbol: str) -> None:
        e = self._entries[symbol]
        # 대기 상태에서는 약한 하락 편향을 줘서 진입 전이를 빨리 구경할 수 있게 한다.
        # 보유 중에는 중립 랜덤워크 — 익절/손절 어느 쪽으로든 자연스럽게 흘러간다.
        drift = -0.0008 if e["pos"].state is State.WAITING else 0.0
        e["price"] = max(1, round(e["price"] * (1 + random.gauss(drift, _VOLATILITY))))
        price, pos, params = e["price"], e["pos"], e["params"]
        self._bus.events.put(bus.Tick(symbol, price))

        d = decide(pos, params, price)
        if d is None:
            return
        from_state = pos.state
        if d.side is None:  # 주문 없는 즉시 전이
            pos = apply_transition(pos, d)
        else:  # 주문 → (즉시 체결 가정) → 확정
            pos = apply_fill(mark_pending(pos), d, fill_price=price, fill_qty=d.qty)
        e["pos"] = pos
        self._store.save_transition(self._date, symbol, from_state, pos, d, price)
        self._emit_position(symbol)
        text = d.reason
        if pos.state.value == "종료":
            text += f" (실현손익 {pos.realized_pnl:+,.0f})"
        self._log(symbol, "전이", text)

    # ── 이벤트 발행 (코어 → UI) ─────────────────────────────────

    def _emit_position(self, symbol: str) -> None:
        e = self._entries[symbol]
        self._bus.events.put(
            bus.PositionUpdate(symbol, e["name"], e["pos"], e["params"])
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

    def _log(self, symbol: str, kind: str, text: str) -> None:
        self._bus.events.put(bus.LogLine(_now(), symbol, kind, text))


def main() -> None:
    b = bus.Bus()

    def run_core() -> None:
        # Store(sqlite 연결)는 반드시 그것을 사용할 코어 스레드 안에서 생성한다.
        # sqlite3 연결은 만든 스레드에서만 쓸 수 있다 (설계 원칙과도 일치:
        # "Store 는 매매 코어 스레드가 단독으로 소유한다").
        store = Store("data/simulator.db")
        SimCore(b, store).loop()

    threading.Thread(target=run_core, daemon=True).start()

    from trader.ui.app import App

    App(b).mainloop()


if __name__ == "__main__":
    main()
