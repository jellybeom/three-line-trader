"""이벤트 로그 뷰 — 상태 전이·주문·에러를 시간순으로 표시. 최근 500줄 유지."""

from __future__ import annotations

from tkinter import ttk

_MAX_ROWS = 500


class EventsView(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.tree = ttk.Treeview(
            self, columns=("ts", "symbol", "kind", "text"), show="headings", height=8
        )
        for col, head, width in (
            ("ts", "시각", 140),
            ("symbol", "종목", 80),
            ("kind", "종류", 60),
            ("text", "내용", 480),
        ):
            self.tree.heading(col, text=head)
            self.tree.column(col, width=width, stretch=(col == "text"))

        scroll = ttk.Scrollbar(self, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def append(self, ts: str, symbol: str, kind: str, text: str) -> None:
        self.tree.insert("", "end", values=(ts, symbol, kind, text))
        children = self.tree.get_children()
        if len(children) > _MAX_ROWS:
            self.tree.delete(children[0])
        self.tree.see(children[-1])  # 자동 스크롤
