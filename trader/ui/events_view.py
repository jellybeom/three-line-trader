"""이벤트 로그 뷰 — 시간순 표시(YYYY-MM-DD HH:MM:SS), 최근 500줄 유지.

'지우기'는 화면 표시만 비운다 — DB 의 events 이력은 그대로 보존된다.
'내보내기'는 현재 표시 중인 로그를 CSV 파일로 저장한다.
"""

from __future__ import annotations

import csv
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

_MAX_ROWS = 500
_COLUMNS = ("ts", "symbol", "kind", "text")


class EventsView(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(4, 4))  # 위아래 여백 동일하게
        ttk.Label(bar, text="로그").pack(side="left", padx=(2, 0))
        ttk.Button(bar, text="내보내기", command=self._export).pack(
            side="right", padx=(4, 2)
        )
        ttk.Button(bar, text="지우기", command=self._clear).pack(
            side="right", padx=(4, 2)
        )

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(body, columns=_COLUMNS, show="headings", height=8)
        for col, head, width in (
            ("ts", "시각", 150),
            ("symbol", "종목", 80),
            ("kind", "종류", 60),
            ("text", "내용", 520),
        ):
            self.tree.heading(col, text=head)
            self.tree.column(col, width=width, stretch=(col == "text"))

        scroll = ttk.Scrollbar(body, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def append(self, ts: str, symbol: str, kind: str, text: str) -> None:
        self.tree.insert("", "end", values=(ts, symbol, kind, text))
        children = self.tree.get_children()
        if len(children) > _MAX_ROWS:
            self.tree.delete(children[0])
        self.tree.see(children[-1])  # 자동 스크롤

    def _clear(self) -> None:
        self.tree.delete(*self.tree.get_children())

    def _export(self) -> None:
        rows = [self.tree.item(i, "values") for i in self.tree.get_children()]
        if not rows:
            messagebox.showinfo("안내", "내보낼 로그가 없습니다.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            initialfile=f"trader-log-{datetime.now():%Y%m%d-%H%M%S}.csv",
            filetypes=[("CSV", "*.csv"), ("모든 파일", "*.*")],
        )
        if not path:
            return
        with open(
            path, "w", newline="", encoding="utf-8-sig"
        ) as f:  # 엑셀 한글 호환 BOM
            writer = csv.writer(f)
            writer.writerow(["시각", "종목", "종류", "내용"])
            writer.writerows(rows)
        messagebox.showinfo("완료", f"로그 {len(rows)}건을 저장했습니다.\n{path}")
