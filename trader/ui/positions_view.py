"""포지션 모니터 — 종목별 상태·평단·잔량·수익률 실시간 표시.

행 우클릭 컨텍스트 메뉴로 '종료 → 대기 초기화'와 '관심종목 제외'를
명령 큐에 넣는다. 실제 처리는 코어가 하고, 결과는 이벤트로 되돌아온다.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from trader.state_machine import Position, State

_COLUMNS = ("name", "state", "price", "avg", "qty", "pnl")
_HEADINGS = ("종목명", "상태", "현재가", "평단가", "잔량/총량", "수익률")
_TP_STATES = {State.BUY1_TP1, State.BUY1_TP2, State.BUY2_TP1, State.BUY2_TP2}


class PositionsView(ttk.Frame):
    def __init__(
        self, master, on_reset: Callable[[str], None], on_delete: Callable[[str], None]
    ):
        super().__init__(master)
        self._on_reset = on_reset
        self._on_delete = on_delete
        self._avg: dict[str, float] = {}  # 수익률 계산용 평단 캐시

        self.tree = ttk.Treeview(
            self, columns=_COLUMNS, show="tree headings", height=12
        )
        self.tree.heading("#0", text="코드")
        self.tree.column("#0", width=80, stretch=False)
        for col, head in zip(_COLUMNS, _HEADINGS):
            self.tree.heading(col, text=head)
            self.tree.column(
                col,
                width=110,
                anchor="e" if col in ("price", "avg", "qty", "pnl") else "w",
            )

        self.tree.tag_configure(
            "tp", foreground="#c62828"
        )  # 익절 진행 (국내 관례: 수익 = 빨강)
        self.tree.tag_configure("closed", foreground="#9e9e9e")

        scroll = ttk.Scrollbar(self, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self._menu = tk.Menu(self, tearoff=0)
        self._menu.add_command(label="종료 → 대기 초기화", command=self._reset_selected)
        self._menu.add_command(label="관심종목 제외", command=self._delete_selected)
        self.tree.bind("<Button-3>", self._popup_menu)

    # ── 이벤트 반영 (app.py 가 호출) ────────────────────────────

    def upsert(self, symbol: str, name: str, pos: Position) -> None:
        self._avg[symbol] = pos.avg_price
        qty = f"{pos.remaining}/{pos.total_bought}" if pos.total_bought else "-"
        avg = f"{pos.avg_price:,.0f}" if pos.avg_price else "-"
        state_text = pos.state.value + (" (체결대기)" if pos.pending else "")
        tag = (
            "closed"
            if pos.state is State.CLOSED
            else ("tp" if pos.state in _TP_STATES else "")
        )
        values = (
            name,
            state_text,
            self._cell(symbol, "price"),
            avg,
            qty,
            self._cell(symbol, "pnl"),
        )
        if self.tree.exists(symbol):
            self.tree.item(symbol, values=values, tags=(tag,))
        else:
            self.tree.insert(
                "", "end", iid=symbol, text=symbol, values=values, tags=(tag,)
            )

    def tick(self, symbol: str, price: float) -> None:
        if not self.tree.exists(symbol):
            return
        self.tree.set(symbol, "price", f"{price:,.0f}")
        avg = self._avg.get(symbol, 0)
        pnl = f"{(price - avg) / avg:+.2%}" if avg else "-"
        self.tree.set(symbol, "pnl", pnl)

    def remove(self, symbol: str) -> None:
        if self.tree.exists(symbol):
            self.tree.delete(symbol)
        self._avg.pop(symbol, None)

    # ── 내부 ────────────────────────────────────────────────────

    def _cell(self, symbol: str, column: str) -> str:
        """upsert 시 현재가·수익률 칸의 기존 표시값을 유지한다."""
        return self.tree.set(symbol, column) if self.tree.exists(symbol) else "-"

    def _popup_menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self._menu.post(event.x_root, event.y_root)

    def _selected(self) -> str | None:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _reset_selected(self) -> None:
        if symbol := self._selected():
            self._on_reset(symbol)

    def _delete_selected(self) -> None:
        symbol = self._selected()
        if symbol and messagebox.askyesno(
            "확인", f"{symbol} 을 관심종목에서 제외할까요?"
        ):
            self._on_delete(symbol)
