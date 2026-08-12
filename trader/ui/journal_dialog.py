"""매매일지 창 — 매매 결과와 차트를 보면서 코멘트를 남긴다.

작성 시점을 강제하지 않는다(종료 직후·장중·마감 후·주말 어느 때나). 그래서 창은
**최근 매매 목록**을 왼쪽에 두고, 고른 매매의 지표와 차트를 오른쪽에 보여준 뒤
아래에서 코멘트를 쓰는 구조다. 미작성 건은 목록에서 바로 구분된다.

매매 데이터(손익·MFE/MAE·태그)는 이미 DB 에 있으므로 여기서는 **읽어서 보여주고
사람이 쓴 것만 저장**한다.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

from trader.journal import transition_path  # 재수출 — 창에서 경로 문자열을 쓴다
from trader.ui.chart_view import ChartView

__all__ = [
    "JournalDialog",
    "entry_label",
    "filter_entries",
    "net_pnl",
    "summarize",
    "transition_path",
]

_ICON = Path(__file__).resolve().parents[2] / "assets" / "three-line-trader.ico"

_TEXT_LINES = 4  # 잘한 점 · 아쉬운 점 입력칸 줄 수 (둘은 항상 같아야 한다)


def summarize(entry: dict) -> list[tuple[str, str]]:
    """일지 상단에 보여줄 매매 요약 (라벨, 값) 목록."""
    avg = entry.get("avg_price") or 0
    total = entry.get("total_bought") or 0
    net = (entry.get("realized_pnl") or 0) - (entry.get("fees") or 0)
    rows = [
        ("매매일", entry.get("trade_date", "")),
        ("종목", f"{entry.get('name', '')}({entry.get('symbol', '')})"),
        ("상태", entry.get("state", "")),
        ("평단 / 수량", f"{avg:,.0f} · {total}주" if avg else "-"),
        (
            "실현손익(세후)",
            f"{net:+,.0f}원"
            + (f" ({net / (avg * total):+.2%})" if avg and total else ""),
        ),
    ]
    high, low = entry.get("high_price") or 0, entry.get("low_price") or 0
    if avg and high:
        rows.append(
            ("최고 / 최저", f"{(high - avg) / avg:+.1%} / {(low - avg) / avg:+.1%}")
        )
    opened, closed = entry.get("day_open") or 0, entry.get("day_close") or 0
    if opened and closed:
        rows.append(("당일 등락", f"{(closed - opened) / opened:+.2%}"))
    if path := (entry.get("path") or ""):
        rows.append(("상태 경로", path))
    if timeline := (entry.get("timeline") or ""):
        rows.append(("시점", timeline))
    if tags := (entry.get("tags") or ""):
        rows.append(("태그", " ".join(f"#{t}" for t in tags.split(",") if t)))
    if base := (entry.get("base_date") or ""):
        rows.append(("기준봉", base))
    if memo := (entry.get("memo") or ""):
        rows.append(("메모", memo))
    return rows


def _add_placeholder(entry: ttk.Entry, var: tk.StringVar, text: str) -> None:
    """비어 있을 때만 흐린 안내 문구를 보여준다 (ttk 에는 기본 기능이 없다)."""
    label = ttk.Label(entry, text=text, foreground="#9e9e9e", background="white")

    def refresh(*_args) -> None:
        if var.get():
            label.place_forget()
        else:
            label.place(x=4, rely=0.5, anchor="w")

    var.trace_add("write", refresh)
    entry.bind("<FocusIn>", lambda _e: label.place_forget())
    entry.bind("<FocusOut>", lambda _e: refresh())
    refresh()


def net_pnl(entry: dict) -> float:
    """세후 실현손익 — 목록 표시와 익절/손절 판정이 같은 값을 쓰도록 한 곳에 둔다."""
    return (entry.get("realized_pnl") or 0) - (entry.get("fees") or 0)


def filter_entries(
    entries: list[dict], query: str = "", result: str = "전체"
) -> list[dict]:
    """검색어와 익절/손절 조건으로 목록을 걸러낸다 (순수 함수 — UI 없이 시험 가능).

    검색어는 **종목명·종목코드 어느 쪽이든 일부만** 맞으면 된다. 코드를 외우고 있지
    않아도 되고, 반대로 이름이 헷갈릴 때는 코드로 찾을 수 있다. 대소문자는 무시한다.

    익절/손절은 **세후 손익 부호**로 나눈다. 수수료·세금까지 빼고도 남았는지가
    실제로 번 것인지의 기준이라, 매도가 평단보다 높았는지보다 이쪽이 정확하다.
    본전(0원)은 익절도 손절도 아니므로 '전체' 에서만 보인다.
    """
    rows = entries
    if query := query.strip().lower():
        rows = [
            e
            for e in rows
            if query in (e.get("name") or "").lower()
            or query in (e.get("symbol") or "").lower()
        ]
    if result == "익절":
        rows = [e for e in rows if net_pnl(e) > 0]
    elif result == "손절":
        rows = [e for e in rows if net_pnl(e) < 0]
    return rows


def entry_label(entry: dict) -> str:
    """목록 한 줄 — 작성 여부가 한눈에 보이도록 앞에 표시를 둔다."""
    net = net_pnl(entry)
    icon = "💰" if net > 0 else ("🛑" if net < 0 else "⚪")
    written = "✍" if (entry.get("good") or entry.get("bad")) else "　"
    return f"{written} {icon} {entry.get('trade_date', '')} {entry.get('name', '')} {net:+,.0f}"


class JournalDialog(tk.Toplevel):
    """일지 작성 창. 저장은 on_save(trade_date, symbol, good, bad) 로 위임한다."""

    def __init__(
        self,
        master,
        entries: list[dict],
        on_save: Callable[[str, str, str, str], None],
        select: tuple[str, str] | None = None,
    ):
        super().__init__(master)
        self.title("매매일지")
        self.geometry("1180x760")
        if _ICON.exists():
            try:
                self.iconbitmap(str(_ICON))
            except tk.TclError:
                pass

        self._entries = entries
        self._visible = entries  # 검색·필터를 통과해 지금 목록에 보이는 것
        self._on_save = on_save
        self._current: dict | None = None
        self._views: dict[str, ChartView] = {}

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=8, pady=8)

        left = ttk.Frame(pane)
        ttk.Label(left, text="매매 목록 (✍ = 작성됨)").pack(anchor="w", pady=(0, 4))

        # 검색 — 종목명·코드 어느 쪽으로도 찾을 수 있다. 입력할 때마다 즉시 걸러진다
        # (목록이 수백 건이어도 문자열 비교뿐이라 체감 지연이 없다).
        self._query = tk.StringVar()
        self._query.trace_add("write", lambda *_: self._fill_list())
        search = ttk.Entry(left, textvariable=self._query)
        search.pack(fill="x", pady=(0, 4))
        _add_placeholder(search, self._query, "종목명 또는 종목코드")

        self._result = tk.StringVar(value="전체")
        buttons = ttk.Frame(left)
        buttons.pack(fill="x", pady=(0, 4))
        for text in ("전체", "익절", "손절"):
            ttk.Radiobutton(
                buttons,
                text=text,
                value=text,
                variable=self._result,
                command=self._fill_list,
                style="Toolbutton",  # 라디오점 대신 눌린 버튼 모양으로 보인다
            ).pack(side="left", expand=True, fill="x")

        self._list = tk.Listbox(left, width=34, exportselection=False)
        self._list.pack(fill="both", expand=True)
        self._list.bind("<<ListboxSelect>>", self._on_select)
        self._count = ttk.Label(left, text="", foreground="#9e9e9e")
        self._count.pack(anchor="w", pady=(2, 0))
        pane.add(left, weight=1)

        right = ttk.Frame(pane)
        pane.add(right, weight=3)

        self._summary = ttk.Frame(right)
        self._summary.pack(side="top", fill="x", pady=(0, 6))

        # **입력칸과 저장 버튼을 아래쪽에 먼저 배치한다.** pack 은 순서대로 자리를 나눠주는데,
        # 차트(expand=True)를 먼저 넣으면 창보다 내용이 커질 때 뒤에 오는 위젯이 화면 밖으로
        # 밀려난다 — 실제로 요약 줄이 늘어나자 저장 버튼이 통째로 사라졌다(2026-08-11).
        # side="bottom" 으로 먼저 잡아두면 차트는 '남는 만큼' 만 쓰므로 절대 밀리지 않는다.
        bar = ttk.Frame(right)
        bar.pack(side="bottom", fill="x", pady=(6, 0))
        self._status = ttk.Label(bar, text="", foreground="#9e9e9e")
        self._status.pack(side="left")
        ttk.Button(bar, text="저장", command=self._save).pack(side="right")

        form = ttk.Frame(right)
        form.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Label(form, text="잘한 점").grid(row=0, column=0, sticky="nw", pady=(0, 4))
        self._good = tk.Text(form, height=_TEXT_LINES, wrap="word")
        self._good.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(0, 4))
        ttk.Label(form, text="아쉬운 점").grid(
            row=1, column=0, sticky="nw", pady=(0, 4)
        )
        self._bad = tk.Text(form, height=_TEXT_LINES, wrap="word")
        self._bad.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(0, 4))
        form.columnconfigure(1, weight=1)
        # uniform 을 같게 주면 두 칸의 높이가 항상 같아진다 (한쪽만 눌리지 않는다)
        for row in (0, 1):
            form.rowconfigure(row, weight=1, uniform="comment")

        self._charts = ttk.Notebook(right)
        self._charts.pack(side="top", fill="both", expand=True)

        self._fill_list()
        self.update_idletasks()  # 차트 축소 배율을 실제 배치 크기로 계산하기 위해
        if select:
            self._select_symbol(*select)
        elif entries:
            self._list.selection_set(0)
            self._show(entries[0])

    # ── 목록 ───────────────────────────────────────────────────

    def _fill_list(self) -> None:
        """검색·필터를 적용해 목록을 다시 그린다.

        고른 항목이 필터에 걸려 사라지면 오른쪽 상세는 그대로 둔다 — 검색어를 지우는
        중에 화면이 깜빡이거나, 쓰던 코멘트가 날아가는 것을 막기 위해서다.
        """
        self._visible = filter_entries(
            self._entries, self._query.get(), self._result.get()
        )
        self._list.delete(0, "end")
        for entry in self._visible:
            self._list.insert("end", entry_label(entry))
        total = len(self._entries)
        shown = len(self._visible)
        self._count.configure(
            text=f"{shown}건" if shown == total else f"{shown} / {total}건"
        )
        if self._current in self._visible:  # 보고 있던 항목이 남아 있으면 선택 유지
            index = self._visible.index(self._current)
            self._list.selection_set(index)
            self._list.see(index)

    def _select_symbol(self, trade_date: str, symbol: str) -> None:
        for i, entry in enumerate(self._visible):
            if entry["trade_date"] == trade_date and entry["symbol"] == symbol:
                self._list.selection_clear(0, "end")
                self._list.selection_set(i)
                self._list.see(i)
                self._show(entry)
                return

    def _on_select(self, _event=None) -> None:
        sel = self._list.curselection()
        if sel and sel[0] < len(self._visible):
            self._show(self._visible[sel[0]])

    # ── 표시 ───────────────────────────────────────────────────

    def _show(self, entry: dict) -> None:
        self._current = entry
        for child in self._summary.winfo_children():
            child.destroy()
        for row, (label, value) in enumerate(summarize(entry)):
            ttk.Label(self._summary, text=label, foreground="#9e9e9e").grid(
                row=row // 2, column=(row % 2) * 2, sticky="w", padx=(0, 6)
            )
            ttk.Label(self._summary, text=value).grid(
                row=row // 2, column=(row % 2) * 2 + 1, sticky="w", padx=(0, 24)
            )

        # 탭은 창을 만들 때 한 번만 두고 그림만 갈아끼운다. 매번 새로 만들면 확대 상태가
        # 초기화되고, 위젯이 쌓여 메모리도 샌다.
        for title, key in (("일봉", "daily_path"), ("3분봉", "minute_path")):
            view = self._views.get(title)
            if view is None:
                view = ChartView(self._charts)
                self._charts.add(view, text=title)
                self._views[title] = view
            view.show(entry.get(key))

        self._good.delete("1.0", "end")
        self._good.insert("1.0", entry.get("good") or "")
        self._bad.delete("1.0", "end")
        self._bad.insert("1.0", entry.get("bad") or "")
        self._status.configure(
            text="휠: 확대·축소 · 드래그: 이동 · 더블클릭: 원본 열기"
        )

    # ── 저장 ───────────────────────────────────────────────────

    def _save(self) -> None:
        if self._current is None:
            return
        good = self._good.get("1.0", "end").strip()
        bad = self._bad.get("1.0", "end").strip()
        if not (good or bad):
            messagebox.showinfo(
                "저장", "잘한 점이나 아쉬운 점을 입력해주세요.", parent=self
            )
            return
        self._on_save(self._current["trade_date"], self._current["symbol"], good, bad)
        self._current["good"], self._current["bad"] = good, bad
        if self._current in self._visible:  # ✍ 표시를 갱신한다
            index = self._visible.index(self._current)
            self._list.delete(index)
            self._list.insert(index, entry_label(self._current))
            self._list.selection_set(index)
        self._status.configure(text="저장했습니다.", foreground="#2e7d32")
