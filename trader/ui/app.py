"""메인 윈도우 — 툴바(시작/일시정지·등록) + 모니터(상단) + 로그(하단).

역할은 세 가지뿐이다: 화면 조립, 200ms 주기 이벤트 큐 폴링,
사용자 조작을 명령 큐로 전달. 매매 판단·저장은 전부 코어의 일이다.
"""

from __future__ import annotations

import queue
import tkinter as tk
from tkinter import ttk

from trader.ui import bus
from trader.ui.events_view import EventsView
from trader.ui.positions_view import PositionsView
from trader.ui.register_dialog import RegisterDialog

_POLL_MS = 200


class App(tk.Tk):
    def __init__(self, b: bus.Bus):
        super().__init__()
        self._bus = b
        self._running = False
        self.title("three-line-trader")
        self.geometry("900x560")

        # ── 툴바 ──
        toolbar = ttk.Frame(self, padding=(8, 6))
        toolbar.pack(fill="x")
        self._toggle_btn = ttk.Button(toolbar, text="감시 시작", command=self._toggle)
        self._toggle_btn.pack(side="left")
        ttk.Button(toolbar, text="종목 등록", command=self._open_register).pack(
            side="left", padx=(6, 0)
        )
        self._status = ttk.Label(toolbar, text="정지됨", foreground="#9e9e9e")
        self._status.pack(side="right")

        # ── 모니터(상단) / 로그(하단) 분할 ──
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.positions = PositionsView(
            paned, on_reset=self._send_reset, on_delete=self._send_delete
        )
        self.events = EventsView(paned)
        paned.add(self.positions, weight=3)
        paned.add(self.events, weight=1)

        self.after(_POLL_MS, self._poll)

    # ── 사용자 조작 → 명령 큐 ───────────────────────────────────

    def _toggle(self) -> None:
        self._bus.commands.put(bus.SetRunning(not self._running))

    def _open_register(self) -> None:
        RegisterDialog(self, on_submit=self._bus.commands.put)

    def _send_reset(self, symbol: str) -> None:
        self._bus.commands.put(bus.Reset(symbol))

    def _send_delete(self, symbol: str) -> None:
        self._bus.commands.put(bus.Delete(symbol))

    # ── 이벤트 큐 → 화면 갱신 ───────────────────────────────────

    def _poll(self) -> None:
        try:
            while True:
                self._dispatch(self._bus.events.get_nowait())
        except queue.Empty:
            pass
        self.after(_POLL_MS, self._poll)

    def _dispatch(self, ev) -> None:
        match ev:
            case bus.PositionUpdate(symbol=s, name=n, position=p):
                self.positions.upsert(s, n, p)
            case bus.Tick(symbol=s, price=p):
                self.positions.tick(s, p)
            case bus.LogLine(ts=ts, symbol=s, kind=k, text=t):
                self.events.append(ts, s, k, t)
            case bus.SymbolRemoved(symbol=s):
                self.positions.remove(s)
            case bus.WatchStatus(running=r):
                self._running = r
                self._toggle_btn.configure(text="일시정지" if r else "감시 시작")
                self._status.configure(
                    text="감시 중" if r else "정지됨",
                    foreground="#2e7d32" if r else "#9e9e9e",
                )
