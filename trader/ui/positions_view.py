"""포지션 모니터 — 상태·현재가·평단·잔량·수익률·실현손익·1/2/3선 표시.

행 내 조작 (직관 UX):
- 각 행 끝의 📈(차트, 추후 구현) / ✎(편집) / ✕(제외) 셀 클릭
- 맨 아래 "＋ 종목 추가하기" 행 클릭 → 등록 창
- 더블클릭 편집, 우클릭 메뉴(편집/리셋/제외)도 유지

열 제목 클릭 시 해당 열 기준 정렬 (재클릭 시 역순).
행 색: 수익 빨강 / 손실 파랑 / 종료 회색 (ttk 표는 셀 단위 색 불가 → 행 단위).
종료 종목의 수익률은 청산 시점 값으로 고정된다.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from trader.state_machine import Params, Position, State

_COLUMNS = (
    "code",
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
    "chart",
    "edit",
    "del",
)
_HEADINGS = (
    "코드",
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
    "",
    "",
    "",
)
_ADD_ROW = "__add__"
_CSV_ROW = "__csv__"
_SPECIAL = {_ADD_ROW, _CSV_ROW}
_BASE_HEADINGS = dict(zip(_COLUMNS, _HEADINGS))


class PositionsView(ttk.Frame):
    def __init__(
        self,
        master,
        on_add: Callable[[], None],
        on_edit: Callable[[str], None],
        on_reset: Callable[[str], None],
        on_delete: Callable[[str], None],
        on_chart: Callable[[str], None],
        on_csv: Callable[[], None],
    ):
        super().__init__(master)
        self._on_add = on_add
        self._on_edit = on_edit
        self._on_reset = on_reset
        self._on_delete = on_delete
        self._on_chart = on_chart
        self._on_csv = on_csv
        self._avg: dict[str, float] = {}  # 수익률 계산용 평단 캐시
        self._closed: set[str] = set()  # 종료 종목: 수익률을 종료 시점 값으로 고정
        self._sort_reverse: dict[str, bool] = {}

        self.tree = ttk.Treeview(self, columns=_COLUMNS, show="headings")
        for col, head in zip(_COLUMNS, _HEADINGS):
            self.tree.heading(col, text=head, command=lambda c=col: self._sort(c))
            if col in ("chart", "edit", "del"):
                self.tree.column(col, width=32, anchor="center", stretch=False)
            elif col == "code":
                self.tree.column(col, width=76, anchor="center", stretch=False)
            else:
                width = 150 if col == "state" else (100 if col == "name" else 90)
                anchor = "center" if col in ("name", "state") else "e"
                self.tree.column(col, width=width, anchor=anchor)

        self.tree.tag_configure(
            "profit", foreground="#c62828"
        )  # 수익 = 빨강 (국내 관례)
        self.tree.tag_configure("loss", foreground="#1565c0")  # 손실 = 파랑
        self.tree.tag_configure("closed", foreground="#9e9e9e")
        self.tree.tag_configure("addrow", foreground="#1565c0")
        self.tree.tag_configure("staged", foreground="#f9a825")  # 3선 미입력 대기

        scroll = ttk.Scrollbar(self, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self._menu_target: str | None = None
        self._menu = tk.Menu(self, tearoff=0)
        self._menu.add_command(label="편집", command=lambda: self._call(self._on_edit))
        self._menu.add_command(
            label="종료 → 대기 초기화", command=lambda: self._call(self._on_reset)
        )
        self._menu.add_separator()
        self._menu.add_command(
            label="관심종목 제외", command=lambda: self._call(self._confirm_delete)
        )
        self.tree.bind("<Button-3>", self._popup_menu)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-1>", self._on_click)

        self._ensure_add_row()

    # ── 이벤트 반영 (app.py 가 호출) ────────────────────────────

    def upsert(self, symbol: str, name: str, pos: Position, params: Params) -> None:
        self._avg[symbol] = pos.avg_price
        qty = f"{pos.remaining}/{pos.total_bought}" if pos.total_bought else "-"
        avg = f"{pos.avg_price:,.0f}" if pos.avg_price else "-"
        state_text = pos.state.value + (" (체결대기)" if pos.pending else "")
        if pos.state is State.CLOSED:
            self._closed.add(symbol)
            tag = "closed"
            # 과거 조회 등 틱이 없어도 최종 수익률을 저장값으로 복원 표시
            if pos.total_bought and pos.avg_price:
                pnl_cell = (
                    f"{pos.realized_pnl / (pos.avg_price * pos.total_bought):+.2%}"
                )
            else:
                pnl_cell = "-"
        else:
            self._closed.discard(symbol)  # 관리자 리셋으로 되살아나면 다시 갱신
            tag = self.tree.item(symbol, "tags") if self.tree.exists(symbol) else ""
            tag = tag[0] if tag and tag[0] in ("profit", "loss") else ""
            pnl_cell = self._cell(symbol, "pnl")
        realized = f"{pos.realized_pnl:+,.0f}" if pos.realized_pnl else "-"
        values = (
            name,
            state_text,
            self._cell(symbol, "price"),
            avg,
            qty,
            pnl_cell,
            realized,
            f"{params.line1:,.0f}",
            f"{params.line2:,.0f}",
            f"{params.line3:,.0f}",
            "📈",
            "✎",
            "✕",
        )
        values = (symbol, *values)
        if self.tree.exists(symbol):
            self.tree.item(symbol, values=values, tags=(tag,) if tag else ())
        else:
            self.tree.insert(
                "", "end", iid=symbol, values=values, tags=(tag,) if tag else ()
            )
        self._ensure_add_row()

    def tick(self, symbol: str, price: float) -> None:
        if not self.tree.exists(symbol) or symbol in _SPECIAL:
            return
        self.tree.set(symbol, "price", f"{price:,.0f}")
        if symbol in self._closed:  # 종료: 수익률·색상 고정
            return
        avg = self._avg.get(symbol, 0)
        if not avg:
            return
        pnl = (price - avg) / avg
        self.tree.set(symbol, "pnl", f"{pnl:+.2%}")
        tag = "profit" if pnl > 0 else ("loss" if pnl < 0 else "")
        self.tree.item(symbol, tags=(tag,) if tag else ())

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
        self._ensure_add_row()

    def selected(self) -> str | None:
        sel = self.tree.selection()
        return sel[0] if sel and sel[0] not in _SPECIAL else None

    # ── 행 내 조작 ──────────────────────────────────────────────

    def _ensure_add_row(self) -> None:
        for iid, label in (
            (_ADD_ROW, "＋ 종목 추가하기"),
            (_CSV_ROW, "＋ 종목 CSV 불러오기"),
        ):
            if not self.tree.exists(iid):
                values = [""] * len(_COLUMNS)
                values[1] = label  # 종목명 열 아래에 표시
                self.tree.insert("", "end", iid=iid, values=values, tags=("addrow",))
            self.tree.move(iid, "", "end")  # 항상 맨 아래 유지 (추가 → CSV 순)

    def upsert_staged(self, code: str, name: str) -> None:
        """CSV 로 불러온 3선 미입력 종목 — ✎ 로 가격을 입력하면 정식 등록된다."""
        vals = {c: "" for c in _COLUMNS}
        vals.update(
            code=code,
            name=name,
            state="3선 미입력",
            line1="-",
            line2="-",
            line3="-",
            edit="✎",
        )
        vals["del"] = "✕"
        values = [vals[c] for c in _COLUMNS]
        if self.tree.exists(code):
            self.tree.item(code, values=values, tags=("staged",))
        else:
            self.tree.insert("", "end", iid=code, values=values, tags=("staged",))
        self._ensure_add_row()

    def _on_click(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if row == _ADD_ROW:
            self._on_add()
            return
        if row == _CSV_ROW:
            self._on_csv()
            return
        col_id = self.tree.identify_column(event.x)  # '#N' (1부터)
        index = int(col_id.lstrip("#"))
        if index < 1:
            return
        col = _COLUMNS[index - 1]
        if col == "chart":
            self._on_chart(row)
        elif col == "edit":
            self._on_edit(row)
        elif col == "del":
            self._confirm_delete(row)

    def _on_double_click(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row and row not in _SPECIAL:
            self._on_edit(row)

    def _confirm_delete(self, symbol: str) -> None:
        """제외 확인은 여기 한 곳에서만 — 종목명을 함께 표시한다."""
        name = self.tree.set(symbol, "name") if self.tree.exists(symbol) else ""
        if messagebox.askyesno("확인", f"{symbol}({name})를 관심종목에서 제외할까요?"):
            self._on_delete(symbol)

    def deselect(self) -> None:
        if sel := self.tree.selection():
            self.tree.selection_remove(*sel)

    # ── 정렬 ────────────────────────────────────────────────────

    def _sort(self, col: str) -> None:
        if col in ("chart", "edit", "del"):
            return  # 조작 열은 정렬 대상 아님
        rows = [iid for iid in self.tree.get_children() if iid not in _SPECIAL]
        keyed = [(self.tree.set(iid, col), iid) for iid in rows]
        reverse = self._sort_reverse[col] = not self._sort_reverse.get(col, False)

        def key(pair):
            raw = pair[0].replace(",", "").replace("%", "").replace("+", "")
            try:
                return (0, float(raw))
            except ValueError:
                return (1, pair[0])

        for i, (_, iid) in enumerate(sorted(keyed, key=key, reverse=reverse)):
            self.tree.move(iid, "", i)
        self._ensure_add_row()
        for c, base in _BASE_HEADINGS.items():  # 정렬 기준 열에 방향 표시
            if c in ("chart", "edit", "del"):
                continue
            arrow = (" ▼" if reverse else " ▲") if c == col else ""
            self.tree.heading(c, text=base + arrow)

    # ── 내부 ────────────────────────────────────────────────────

    def _cell(self, symbol: str, column: str) -> str:
        """upsert 시 현재가·수익률 칸의 기존 표시값을 유지한다."""
        return self.tree.set(symbol, column) if self.tree.exists(symbol) else "-"

    def _popup_menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row and row not in _SPECIAL:
            self._menu_target = row
            self.tree.selection_set(row)
            self._menu.post(event.x_root, event.y_root)

    def _call(self, handler: Callable[[str], None]) -> None:
        if self._menu_target and self.tree.exists(self._menu_target):
            handler(self._menu_target)
