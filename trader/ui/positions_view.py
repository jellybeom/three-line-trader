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
from trader.trading_calendar import format_days
from trader.ui import theme
from trader.ui.mini_scroll import MiniScroll

_COLUMNS = (
    "code",
    "name",
    "state",
    "price",
    "change",
    "avg",
    "qty",
    "hold",
    "pnl",
    "realized",
    "line1",
    "line2",
    "line3",
    "range",
    "base",
    "memo",
    "chart",
    "edit",
    "del",
)
_HEADINGS = (
    "코드",
    "종목명",
    "상태",
    "현재가",
    "등락률",
    "평단가",
    "잔량/총량",
    "보유",
    "수익률",
    "실현손익",
    "1선",
    "2선",
    "3선",
    "1선↔3선",
    "기준봉",
    "메모",
    "",
    "",
    "",
)
_ADD_ROW = "__add__"
_CSV_ROW = "__csv__"
_SPECIAL = {_ADD_ROW, _CSV_ROW}
_BASE_HEADINGS = dict(zip(_COLUMNS, _HEADINGS))


def _holding_minutes(text: str) -> float:
    """보유기간 표기를 정렬용 분으로 환산한다.

    '2일차' 는 시간을 모르므로 하루(장 6시간 30분)를 곱해 대략의 자리에 놓는다 —
    이월 종목끼리는 일수로 갈리고, 당일 종목보다는 항상 뒤에 온다.

    진입 전 종목(빈칸)은 **가장 작은 값**으로 둔다. 첫 클릭이 내림차순이라 이렇게 해야
    '오래 들고 있는 것부터' 를 보려고 눌렀을 때 아직 사지도 않은 종목이 맨 위를
    차지하지 않는다. '0분' 과도 구분되게 음수를 쓴다.
    """
    import re

    if not text.strip() or text.strip() == "-":
        return -1.0
    if m := re.match(r"(\d+)일차", text):
        return int(m.group(1)) * 390.0
    minutes = 0.0
    if m := re.search(r"(\d+)시간", text):
        minutes += int(m.group(1)) * 60
    if m := re.search(r"(\d+)분", text):
        minutes += int(m.group(1))
    return minutes


# 가격을 나타내는 열 — 오른쪽 정렬하되 셀 끝에 바싹 붙지 않게 여백을 준다
_PRICE_COLUMNS = ("price", "avg", "realized", "line1", "line2", "line3")
_CELL_PAD = "  "  # Treeview 는 셀 안쪽 여백을 지원하지 않아 공백으로 대신한다


def _pad(text: str) -> str:
    """오른쪽 정렬 셀의 끝 여백."""
    return f"{text}{_CELL_PAD}" if text else text


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
        on_carry: Callable[[str], None],
        on_carry_position: Callable[[str], None],
        on_manual_sell: Callable[[str], None],
    ):
        super().__init__(master)
        self._on_add = on_add
        self._on_edit = on_edit
        self._on_reset = on_reset
        self._on_delete = on_delete
        self._on_chart = on_chart
        self._on_csv = on_csv
        self._on_carry = on_carry
        self._on_carry_position = on_carry_position
        self._on_manual_sell = on_manual_sell
        self._avg: dict[str, float] = {}  # 수익률 계산용 평단 캐시
        self._closed: set[str] = set()  # 종료 종목: 수익률을 종료 시점 값으로 고정
        self._blocked: dict[str, str] = {}  # 진입 보류 중인 종목 → 사유
        self._day_open: dict[str, float] = {}  # 종목별 당일 첫 체결가 (등락률 기준)
        self._holding: dict[str, str] = {}  # 종목별 보유기간 표기 (30초마다 갱신)
        self._sort_reverse: dict[str, bool] = {}
        # 화면에 보이는 순서 그대로의 **전체** 종목 목록. tree.get_children() 은 숨긴 행을
        # 빼고 돌려주므로, 정렬·전체 삭제에 그것만 쓰면 숨긴 종목이 누락된다.
        self._order: list[str] = []
        self._query = ""  # 필터 검색어 (빈 문자열이면 전부 보인다)

        # extended: Ctrl·Shift 로 여러 종목을 골라 한 번에 처리할 수 있다
        self.tree = ttk.Treeview(
            self, columns=_COLUMNS, show="headings", selectmode="extended"
        )
        for col, head in zip(_COLUMNS, _HEADINGS):
            self.tree.heading(col, text=head, command=lambda c=col: self._sort(c))
            if col in ("chart", "edit", "del"):
                self.tree.column(col, width=32, anchor="center", stretch=False)
            elif col == "code":
                self.tree.column(col, width=76, anchor="center", stretch=False)
            elif col in ("range", "change"):
                self.tree.column(col, width=68, anchor="center")
            elif col == "base":  # 기준봉 D+n — 짧은 값이라 좁게
                self.tree.column(col, width=52, anchor="center")
            elif col == "hold":  # 보유기간 — '3시간 12분' 이 들어갈 만큼만
                self.tree.column(col, width=78, anchor="center")
            elif col == "memo":
                self.tree.column(col, width=110, anchor="center")
            else:
                width = 150 if col == "state" else (100 if col == "name" else 90)
                # 가격 열만 오른쪽 정렬(자릿수 비교가 쉬움), 나머지는 가운데 정렬
                anchor = "e" if col in _PRICE_COLUMNS else "center"
                self.tree.column(col, width=width, anchor=anchor)

        c = theme.palette()
        self.tree.tag_configure(
            "profit", foreground=c.profit
        )  # 수익 = 빨강 (국내 관례)
        self.tree.tag_configure("loss", foreground=c.loss)  # 손실 = 파랑
        self.tree.tag_configure("closed", foreground=c.muted)
        self.tree.tag_configure("addrow", foreground=c.link)
        self.tree.tag_configure("staged", foreground=c.warn)  # 3선 미입력 대기

        self.tree.pack(side="left", fill="both", expand=True)
        # 스크롤바는 자리를 차지하지 않는 오버레이로 (종목명·상태 열이 그만큼 넓어진다)
        self._scroll = MiniScroll(self.tree)

        self._menu_targets: list[str] = []
        self._menu = tk.Menu(self, tearoff=0, **theme.classic(self, "menu"))
        # 여러 종목을 한 번에 처리할 수 있는 항목을 위에, 한 종목씩 다뤄야 하는 항목은 아래에.
        self._menu.add_command(
            label="다음 매매일로 이월 (상태·평단·수량)",
            command=lambda: self._call(self._on_carry_position),
        )
        self._menu.add_command(
            label="다음 매매일로 이월 (전체)",
            command=lambda: self._call(self._on_carry),
        )
        self._menu.add_command(
            label="수동 전량 청산 (시장가)",
            command=lambda: self._call(self._on_manual_sell, single=True),
        )
        self._menu.add_command(
            label="종료 → 대기 초기화",
            command=lambda: self._call(self._on_reset, single=True),
        )
        self._menu.add_separator()
        self._menu.add_command(
            label="차트 보기", command=lambda: self._call(self._on_chart)
        )
        self._menu.add_command(
            label="편집", command=lambda: self._call(self._on_edit, single=True)
        )
        self._menu.add_command(
            label="관심종목 제외", command=lambda: self._call(self._confirm_delete)
        )
        # 한 종목에만 의미가 있는 항목 (여러 개 선택 시 비활성화)
        self._single_only = ("수동 전량 청산 (시장가)", "종료 → 대기 초기화", "편집")
        self.tree.bind("<Button-3>", self._popup_menu)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-1>", self._on_click)

        self._ensure_add_row()

    # ── 이벤트 반영 (app.py 가 호출) ────────────────────────────

    def upsert(
        self,
        symbol: str,
        name: str,
        pos: Position,
        params: Params,
        memo: str = "",
        base_days: int | None = None,
        holding: str = "",
    ) -> None:
        self._avg[symbol] = pos.avg_price
        # 빈 값은 다른 열과 같게 '-' 로 채운다. 한 칸만 비어 있으면 값이 없는 것인지
        # 화면이 덜 그려진 것인지 구분되지 않는다.
        holding = holding or "-"
        self._holding[symbol] = holding
        qty = f"{pos.remaining}/{pos.total_bought}" if pos.total_bought else "-"
        avg = f"{pos.avg_price:,.0f}" if pos.avg_price else "-"
        state_text = pos.state.value + (" (체결대기)" if pos.pending else "")
        if symbol in self._blocked and not pos.pending:
            state_text += " (보류)"
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
        line_range = (
            params.line1 - params.line3
        ) / params.line1  # 1선 대비 3선까지 낙폭
        values = (
            name,
            state_text,
            self._cell(symbol, "price"),
            self._cell(symbol, "change"),  # 등락률은 틱이 올 때 갱신된다
            _pad(avg),
            qty,
            holding,
            pnl_cell,
            _pad(realized),
            _pad(f"{params.line1:,.0f}"),
            _pad(f"{params.line2:,.0f}"),
            _pad(f"{params.line3:,.0f}"),
            f"{line_range:.1%}",
            format_days(base_days),
            memo,
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
            self._order.append(symbol)
        self._ensure_add_row()
        # 필터를 켠 채로 틱이 오면 insert 로 되살아나거나 _ensure_add_row 의 move 로
        # 순서가 흔들린다. 갱신 끝에 조건을 다시 걸어 '안 맞으면 안 보인다' 를 지킨다.
        self._apply_filter(symbol)

    def set_holding(self, symbol: str, text: str) -> None:
        """보유기간 칸만 갱신한다 (30초 주기).

        upsert 는 18칸을 통째로 다시 쓰고 필터·순서까지 손대므로, 분 단위로만 바뀌는
        값 하나 때문에 부를 일이 아니다. 값이 그대로면 아무것도 하지 않는다.
        """
        text = text or "-"
        if self._holding.get(symbol) == text or not self.tree.exists(symbol):
            return
        self._holding[symbol] = text
        self.tree.set(symbol, "hold", text)

    def holding_symbols(self) -> list[str]:
        """보유기간 시계가 도는 종목 — 30초 갱신 대상."""
        return [s for s, text in self._holding.items() if text and text != "-"]

    def tick(self, symbol: str, price: float) -> None:
        if not self.tree.exists(symbol) or symbol in _SPECIAL:
            return
        self.tree.set(symbol, "price", _pad(f"{price:,.0f}"))
        # 등락률: 감시 시작 후 첫 체결가 대비. 코어가 이미 기록하는 값이라 추가 조회가 없다.
        opened = self._day_open.setdefault(symbol, price)
        if opened:
            self.tree.set(symbol, "change", f"{(price - opened) / opened:+.2%}")
        if symbol in self._closed:  # 종료: 수익률·색상 고정
            return
        avg = self._avg.get(symbol, 0)
        if not avg:
            return
        pnl = (price - avg) / avg
        self.tree.set(symbol, "pnl", f"{pnl:+.2%}")
        tag = "profit" if pnl > 0 else ("loss" if pnl < 0 else "")
        self.tree.item(symbol, tags=(tag,) if tag else ())

    def set_blocked(self, symbol: str, active: bool, reason: str = "") -> None:
        """진입 보류 표시 — 상태 칸에 '(보류)' 를 붙이고 사유를 도구 설명처럼 남긴다.

        보류는 틱마다 성립하므로 로그로 남기지 않고 화면에만 계속 보여준다
        (코어의 _log_block 참고). 다음 upsert 때 상태 칸이 다시 그려지므로,
        여기서는 기록만 고치고 지금 화면도 즉시 맞춰준다.
        """
        if active:
            self._blocked[symbol] = reason
        else:
            self._blocked.pop(symbol, None)
        if not self.tree.exists(symbol):
            return  # 아직 행이 없다 — 다음 upsert 가 반영한다
        text = self.tree.set(symbol, "state")
        marked = text.endswith(" (보류)")
        if active and not marked and "(체결대기)" not in text:
            self.tree.set(symbol, "state", text + " (보류)")
        elif not active and marked:
            self.tree.set(symbol, "state", text[: -len(" (보류)")])

    def set_day_open(self, symbol: str, price: float) -> None:
        """복원 시 코어가 알려주는 당일 첫 체결가 (재시작해도 등락률이 이어지도록)."""
        if price:
            self._day_open[symbol] = price

    def remove(self, symbol: str) -> None:
        if symbol in self._order:
            self._order.remove(symbol)
        if self.tree.exists(symbol):
            self.tree.delete(symbol)
        self._avg.pop(symbol, None)
        self._holding.pop(symbol, None)
        self._closed.discard(symbol)
        self._day_open.pop(symbol, None)
        self._blocked.pop(symbol, None)

    def clear(self) -> None:
        """매매일 전환 시 전체 비우기.

        get_children() 은 **숨긴 행을 빼고** 돌려주므로 그것만 지우면 필터에 걸린
        종목이 유령처럼 남는다. 전체 목록을 따로 들고 있는 이유다.
        """
        # **필터를 먼저 푼다.** get_children() 은 숨긴 행을 빼고 주므로, 필터가 걸린 채로
        # 지우면 그 종목들이 유령처럼 남는다. 겸사겸사 다른 날의 종목 구성에 어제 검색어가
        # 남는 것도 막는다.
        self.set_filter("")
        self.tree.delete(*self.tree.get_children())
        self._order.clear()
        self._avg.clear()
        self._holding.clear()
        self._closed.clear()
        self._day_open.clear()  # 매매일이 바뀌면 등락률 기준도 새로 잡는다
        self._blocked.clear()
        self._ensure_add_row()

    def selected(self) -> str | None:
        sel = self.tree.selection()
        return sel[0] if sel and sel[0] not in _SPECIAL else None

    # ── 행 내 조작 ──────────────────────────────────────────────

    # ── 검색 필터 ───────────────────────────────────────────────

    def set_filter(self, query: str) -> int:
        """종목명·코드로 목록을 좁힌다. 보이는 종목 수를 돌려준다.

        **살아 있는 화면을 가리는 기능**이라 규칙을 좁게 잡았다.
        - 검색 대상은 종목명과 코드뿐이다 (메모·태그까지 넣으면 왜 걸렸는지 알기 어렵다)
        - 숨긴 행도 값 갱신은 계속된다 — 되돌리면 그동안의 변화가 그대로 보인다
        - 숨긴 종목은 선택할 수 없다(Tk 가 막는다). 보이지 않는 종목에 주문이 나가지 않는다
        - ＋추가 행은 언제나 맨 아래 남는다 (0건일 때 바로 등록으로 이어진다)
        """
        self._query = query.strip().lower()
        self._relayout()
        return sum(1 for iid in self._order if self._matches(iid))

    def count(self) -> int:
        """등록된 전체 종목 수 (숨긴 것 포함)."""
        return len(self._order)

    def _matches(self, symbol: str) -> bool:
        if not self._query:
            return True
        if not self.tree.exists(symbol):
            return False
        name = str(self.tree.set(symbol, "name")).lower()
        return self._query in symbol.lower() or self._query in name

    def _relayout(self) -> None:
        """_order 순서대로 화면을 다시 세운다 (조건에 맞는 것만 붙인다)."""
        visible = 0
        for symbol in self._order:
            if not self.tree.exists(symbol):
                continue  # 삭제와 겹치면 TclError 가 난다 — 반드시 먼저 확인한다
            if self._matches(symbol):
                self.tree.move(symbol, "", visible)
                visible += 1
            else:
                self.tree.detach(symbol)
        self._ensure_add_row()
        # selection 은 detach 시 Tk 가 알아서 비우지만 focus 는 남는다.
        # 남겨두면 나중에 focus 기반 조작이 숨긴 종목을 가리킬 수 있다.
        if (current := self.tree.focus()) and not self.tree.exists(current):
            self.tree.focus("")
        elif current and current not in _SPECIAL and not self._matches(current):
            self.tree.focus("")

    def _apply_filter(self, symbol: str) -> None:
        """한 종목만 조건에 맞춰 붙이거나 뗀다 (틱마다 전체를 다시 세우지 않도록)."""
        if not self._query or not self.tree.exists(symbol):
            return
        if self._matches(symbol):
            self._relayout()  # 순서를 지키려면 전체 배치가 필요하다
        else:
            self.tree.detach(symbol)

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

    def upsert_staged(self, code: str, name: str, memo: str = "") -> None:
        """CSV 로 불러온 3선 미입력 종목 — ✎ 로 가격을 입력하면 정식 등록된다."""
        vals = {c: "" for c in _COLUMNS}
        vals.update(
            code=code,
            name=name,
            state="3선 미입력",
            memo=memo,
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
            self._order.append(code)
        self._ensure_add_row()
        self._apply_filter(code)

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
        """열 기준 정렬 — **숨긴 종목까지 포함해** 순서를 정한다.

        보이는 것만 정렬하면 필터를 풀었을 때 숨어 있던 종목이 제자리를 못 찾아
        순서가 뒤죽박죽이 된다. 전체 순서를 먼저 확정하고 화면은 그 뒤에 맞춘다.
        """
        if col in ("chart", "edit", "del"):
            return  # 조작 열은 정렬 대상 아님
        rows = [iid for iid in self._order if self.tree.exists(iid)]
        keyed = [(self.tree.set(iid, col), iid) for iid in rows]
        reverse = self._sort_reverse[col] = not self._sort_reverse.get(col, False)

        def key(pair):
            if col == "hold":  # '47분' < '3시간 12분' < '2일차' 로 세운다
                return (0, _holding_minutes(pair[0]))
            raw = pair[0].replace(",", "").replace("%", "").replace("+", "")
            try:
                return (0, float(raw))
            except ValueError:
                return (1, pair[0])

        self._order = [iid for _, iid in sorted(keyed, key=key, reverse=reverse)]
        self._relayout()
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
        """우클릭 메뉴. 이미 선택된 여러 행 위에서 누르면 그 선택을 유지한다."""
        row = self.tree.identify_row(event.y)
        if not row or row in _SPECIAL:
            return
        selection = [s for s in self.tree.selection() if s not in _SPECIAL]
        if row not in selection:  # 선택 밖을 눌렀으면 그 행만 대상으로
            self.tree.selection_set(row)
            selection = [row]
        self._menu_targets = selection

        multi = len(selection) > 1
        for label in self._single_only:
            self._menu.entryconfigure(
                label, state="disabled" if multi else "normal", label=label
            )
        self._menu.post(event.x_root, event.y_root)

    def _call(self, handler: Callable[[str], None], single: bool = False) -> None:
        """선택된 종목마다 handler 실행. single 항목은 한 종목일 때만 동작한다."""
        targets = [s for s in self._menu_targets if self.tree.exists(s)]
        if not targets or (single and len(targets) > 1):
            return
        for symbol in targets:
            handler(symbol)
