"""매매일지 창 — 매매 결과와 차트를 보면서 코멘트를 남긴다.

작성 시점을 강제하지 않는다(종료 직후·장중·마감 후·주말 어느 때나). 그래서 창은
**최근 매매 목록**을 왼쪽에 두고, 고른 매매의 지표와 차트를 오른쪽에 보여준 뒤
아래에서 코멘트를 쓰는 구조다. 미작성 건은 목록에서 바로 구분된다.

매매 데이터(손익·MFE/MAE·태그)는 이미 DB 에 있으므로 여기서는 **읽어서 보여주고
사람이 쓴 것만 저장**한다.
"""

from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

from trader.journal import transition_path  # 재수출 — 창에서 경로 문자열을 쓴다

__all__ = ["JournalDialog", "entry_label", "summarize", "transition_path"]

_ICON = Path(__file__).resolve().parents[2] / "assets" / "three-line-trader.ico"


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


def entry_label(entry: dict) -> str:
    """목록 한 줄 — 작성 여부가 한눈에 보이도록 앞에 표시를 둔다."""
    net = (entry.get("realized_pnl") or 0) - (entry.get("fees") or 0)
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
        self._on_save = on_save
        self._current: dict | None = None
        self._photos: list = []  # PhotoImage 참조 유지 (없으면 즉시 사라진다)

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=8, pady=8)

        left = ttk.Frame(pane)
        ttk.Label(left, text="매매 목록 (✍ = 작성됨)").pack(anchor="w", pady=(0, 4))
        self._list = tk.Listbox(left, width=34, exportselection=False)
        self._list.pack(fill="both", expand=True)
        self._list.bind("<<ListboxSelect>>", self._on_select)
        pane.add(left, weight=1)

        right = ttk.Frame(pane)
        pane.add(right, weight=3)

        self._summary = ttk.Frame(right)
        self._summary.pack(fill="x", pady=(0, 6))

        self._charts = ttk.Notebook(right)
        self._charts.pack(fill="both", expand=True)

        form = ttk.Frame(right)
        form.pack(fill="x", pady=(8, 0))
        ttk.Label(form, text="잘한 점").grid(row=0, column=0, sticky="nw")
        self._good = tk.Text(form, height=4, wrap="word")
        self._good.grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=(0, 4))
        ttk.Label(form, text="아쉬운 점").grid(row=1, column=0, sticky="nw")
        self._bad = tk.Text(form, height=4, wrap="word")
        self._bad.grid(row=1, column=1, sticky="ew", padx=(8, 0))
        form.columnconfigure(1, weight=1)

        bar = ttk.Frame(right)
        bar.pack(fill="x", pady=(6, 0))
        self._status = ttk.Label(bar, text="", foreground="#9e9e9e")
        self._status.pack(side="left")
        ttk.Button(bar, text="저장", command=self._save).pack(side="right")

        self._fill_list()
        if select:
            self._select_symbol(*select)
        elif entries:
            self._list.selection_set(0)
            self._show(entries[0])

    # ── 목록 ───────────────────────────────────────────────────

    def _fill_list(self) -> None:
        self._list.delete(0, "end")
        for entry in self._entries:
            self._list.insert("end", entry_label(entry))

    def _select_symbol(self, trade_date: str, symbol: str) -> None:
        for i, entry in enumerate(self._entries):
            if entry["trade_date"] == trade_date and entry["symbol"] == symbol:
                self._list.selection_clear(0, "end")
                self._list.selection_set(i)
                self._list.see(i)
                self._show(entry)
                return

    def _on_select(self, _event=None) -> None:
        sel = self._list.curselection()
        if sel:
            self._show(self._entries[sel[0]])

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

        for tab in self._charts.tabs():
            self._charts.forget(tab)
        self._photos.clear()
        for title, path in (
            ("일봉", entry.get("daily_path")),
            ("3분봉", entry.get("minute_path")),
        ):
            frame = ttk.Frame(self._charts)
            self._charts.add(frame, text=title)
            if not path or not Path(path).exists():
                ttk.Label(frame, text="보관된 차트가 없습니다").pack(padx=20, pady=20)
                continue
            try:
                photo = tk.PhotoImage(file=path)
            except tk.TclError as e:
                ttk.Label(frame, text=f"이미지를 열 수 없습니다: {e}").pack(
                    padx=20, pady=20
                )
                continue
            factor = -(-photo.height() // max(1, self.winfo_screenheight() - 500))
            if factor > 1:
                photo = photo.subsample(factor, factor)
            self._photos.append(photo)
            label = ttk.Label(frame, image=photo, cursor="hand2")
            label.pack()
            label.bind(
                "<Double-Button-1>",
                lambda _e, p=path: (
                    os.startfile(p) if hasattr(os, "startfile") else None
                ),
            )

        self._good.delete("1.0", "end")
        self._good.insert("1.0", entry.get("good") or "")
        self._bad.delete("1.0", "end")
        self._bad.insert("1.0", entry.get("bad") or "")
        self._status.configure(text="더블클릭: 차트 원본 열기")

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
        index = self._entries.index(self._current)
        self._list.delete(index)
        self._list.insert(index, entry_label(self._current))
        self._list.selection_set(index)
        self._status.configure(text="저장했습니다.", foreground="#2e7d32")
