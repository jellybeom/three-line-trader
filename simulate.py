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
from datetime import datetime

from trader.state_machine import (
    Side,
    State,
    apply_fill,
    apply_transition,
    decide,
    mark_pending,
)
from trader.store import Store
from trader.ui import bus

_TICK_INTERVAL = 0.2  # 초
_VOLATILITY = 0.003  # 틱당 표준편차 0.3%


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


class SimCore:
    """가짜 시세 + 실제 상태 머신/저장소. 백그라운드 스레드에서 loop() 실행."""

    def __init__(self, b: bus.Bus, store: Store):
        self._bus = b
        self._store = store
        self._running = False
        # symbol -> {"name", "params", "pos", "price"}
        self._entries: dict[str, dict] = {}
        for symbol, (name, params, pos) in store.load_all().items():
            price = pos.avg_price if pos.avg_price else params.line1 * 1.03
            self._entries[symbol] = {
                "name": name,
                "params": params,
                "pos": pos,
                "price": price,
            }

    def loop(self) -> None:
        # 복원된 종목을 UI 에 초기 표시
        for symbol, e in self._entries.items():
            self._emit_position(symbol)
        self._bus.events.put(bus.WatchStatus(self._running))

        while True:
            self._handle_commands()
            if self._running:
                for symbol in list(self._entries):
                    self._step(symbol)
            time.sleep(_TICK_INTERVAL)

    # ── 명령 처리 (UI → 코어) ───────────────────────────────────

    def _handle_commands(self) -> None:
        while not self._bus.commands.empty():
            cmd = self._bus.commands.get_nowait()
            match cmd:
                case bus.Register(symbol=s, name=n, params=p, position=pos):
                    self._store.register_symbol(s, n, p, pos)
                    price = pos.avg_price if pos.avg_price else p.line1 * 1.03
                    self._entries[s] = {
                        "name": n,
                        "params": p,
                        "pos": pos,
                        "price": price,
                    }
                    self._emit_position(s)
                    self._log(s, "등록", f"{n} (시작 상태: {pos.state.value})")
                case bus.Delete(symbol=s):
                    self._store.delete_symbol(s)
                    self._entries.pop(s, None)
                    self._bus.events.put(bus.SymbolRemoved(s))
                    self._log(s, "삭제", "관심종목 제외")
                case bus.Reset(symbol=s) if s in self._entries:
                    try:
                        new_pos = self._store.admin_reset(s, self._entries[s]["pos"])
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
        self._store.save_transition(symbol, from_state, pos, d, price)
        self._emit_position(symbol)
        self._log(symbol, "전이", d.reason)

    # ── 이벤트 발행 (코어 → UI) ─────────────────────────────────

    def _emit_position(self, symbol: str) -> None:
        e = self._entries[symbol]
        self._bus.events.put(bus.PositionUpdate(symbol, e["name"], e["pos"]))

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
