"""포지션 모니터 — 종목별 상태·현재가·평단·잔량·수익률·1/2/3선 실시간 표시.

행 더블클릭 또는 우클릭 메뉴로 편집을, 우클릭 메뉴로 리셋·제외를 요청한다.
실제 처리는 코어가 하고, 결과는 이벤트로 되돌아온다.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from trader.state_machine import Params, Position, State

_COLUMNS = (
    "name",
    "state",
    "price",
    "avg",
    "qty",
    "pnl",
    "realized",
    "line1",
    "line2",
    "line3",
)
_HEADINGS = (
    "종목명",
    "상태",
    "현재가",
    "평단가",
    "잔량/총량",
    "수익률",
    "실현손익",
    "1선",
    "2선",
    "3선",
)
_TP_STATES = {State.BUY1_TP1, State.BUY1_TP2, State.BUY2_TP1, State.BUY2_TP2}


class PositionsView(ttk.Frame):
    def __init__(
        self,
        master,
        on_edit: Callable[[str], None],
        on_reset: Callable[[str], None],
        on_delete: Callable[[str], None],
    ):
        super().__init__(master)
        self._on_edit = on_edit
        self._on_reset = on_reset
        self._on_delete = on_delete
        self._avg: dict[str, float] = {}  # 수익률 계산용 평단 캐시
        self._closed: set[str] = set()  # 종료 종목: 수익률을 종료 시점 값으로 고정

        self.tree = ttk.Treeview(self, columns=_COLUMNS, show="tree headings")
        self.tree.heading("#0", text="코드")
        self.tree.column("#0", width=76, stretch=False)
        for col, head in zip(_COLUMNS, _HEADINGS):
            self.tree.heading(col, text=head)
            width = 150 if col == "state" else (100 if col == "name" else 92)
            anchor = "w" if col in ("name", "state") else "e"
            self.tree.column(col, width=width, anchor=anchor)

        self.tree.tag_configure("tp", foreground="#c62828")  # 익절 진행 (수익 = 빨강)
        self.tree.tag_configure("closed", foreground="#9e9e9e")

        scroll = ttk.Scrollbar(self, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self._menu = tk.Menu(self, tearoff=0)
        self._menu.add_command(label="편집", command=lambda: self._call(self._on_edit))
        self._menu.add_command(
            label="종료 → 대기 초기화", command=lambda: self._call(self._on_reset)
        )
        self._menu.add_separator()
        self._menu.add_command(
            label="관심종목 제외", command=lambda: self._call(self._on_delete)
        )
        self.tree.bind("<Button-3>", self._popup_menu)
        self.tree.bind("<Double-1>", lambda _e: self._call(self._on_edit))

    # ── 이벤트 반영 (app.py 가 호출) ────────────────────────────

    def upsert(self, symbol: str, name: str, pos: Position, params: Params) -> None:
        self._avg[symbol] = pos.avg_price
        qty = f"{pos.remaining}/{pos.total_bought}" if pos.total_bought else "-"
        avg = f"{pos.avg_price:,.0f}" if pos.avg_price else "-"
        state_text = pos.state.value + (" (체결대기)" if pos.pending else "")
        tag = (
            "closed"
            if pos.state is State.CLOSED
            else ("tp" if pos.state in _TP_STATES else "")
        )
        if pos.state is State.CLOSED:
            self._closed.add(symbol)
        else:
            self._closed.discard(symbol)  # 관리자 리셋으로 되살아나면 다시 갱신
        realized = f"{pos.realized_pnl:+,.0f}" if pos.realized_pnl else "-"
        values = (
            name,
            state_text,
            self._cell(symbol, "price"),
            avg,
            qty,
            self._cell(symbol, "pnl"),
            realized,
            f"{params.line1:,.0f}",
            f"{params.line2:,.0f}",
            f"{params.line3:,.0f}",
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
        if symbol in self._closed:  # 종료: 수익률은 종료 시점 값으로 고정
            return
        avg = self._avg.get(symbol, 0)
        pnl = f"{(price - avg) / avg:+.2%}" if avg else "-"
        self.tree.set(symbol, "pnl", pnl)

    def remove(self, symbol: str) -> None:
        if self.tree.exists(symbol):
            self.tree.delete(symbol)
        self._avg.pop(symbol, None)
        self._closed.discard(symbol)

    def clear(self) -> None:
        """매매일 전환 시 전체 비우기."""
        self.tree.delete(*self.tree.get_children())
        self._avg.clear()
        self._closed.clear()

    def selected(self) -> str | None:
        sel = self.tree.selection()
        return sel[0] if sel else None

    # ── 내부 ────────────────────────────────────────────────────

    def _cell(self, symbol: str, column: str) -> str:
        """upsert 시 현재가·수익률 칸의 기존 표시값을 유지한다."""
        return self.tree.set(symbol, column) if self.tree.exists(symbol) else "-"

    def _popup_menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self._menu.post(event.x_root, event.y_root)

    def _call(self, handler: Callable[[str], None]) -> None:
        if symbol := self.selected():
            handler(symbol)
