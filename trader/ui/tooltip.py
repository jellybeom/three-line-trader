"""마우스를 올리면 뜨는 짧은 설명.

툴바의 연결 상태처럼 **화면에는 점 하나로 줄여 놨지만 자세한 사정이 있는** 자리에 쓴다.
`● 키움` 만 보고는 미연결 사유를 알 수 없는데, 그렇다고 툴바에 전부 적으면 한 줄이
넘친다. 마우스를 올릴 때만 보여 주면 둘 다 된다.

Tk 에는 툴팁이 없어 `Toplevel` 로 직접 만든다. 테두리 없는 창을 마우스 아래에 띄우고,
마우스가 벗어나면 지운다.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

DELAY_MS = 450  # 스치듯 지나갈 때는 뜨지 않게 한다


class Tooltip:
    """위젯 하나에 붙는 툴팁. `text` 를 바꾸면 다음에 뜰 때 반영된다."""

    def __init__(self, widget: tk.Misc, text: str = ""):
        self._widget = widget
        self.text = text
        self._tip: tk.Toplevel | None = None
        self._after: str | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        if self.text:
            self._after = self._widget.after(DELAY_MS, self._show)

    def _cancel(self) -> None:
        if self._after is not None:
            try:
                self._widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _show(self) -> None:
        self._after = None
        if self._tip is not None or not self.text:
            return
        try:
            x = self._widget.winfo_rootx() + 12
            y = self._widget.winfo_rooty() + self._widget.winfo_height() + 6
            tip = tk.Toplevel(self._widget)
            tip.wm_overrideredirect(True)  # 제목 표시줄 없는 창
            tip.wm_geometry(f"+{x}+{y}")
            ttk.Label(
                tip, text=self.text, padding=(8, 4), relief="solid", borderwidth=1
            ).pack()
            self._tip = tip
        except tk.TclError:
            self._tip = None  # 창을 못 만드는 환경 — 툴팁이 없다고 막힐 일은 아니다

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None
