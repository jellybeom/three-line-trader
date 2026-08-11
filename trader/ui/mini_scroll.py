"""오버레이 방식 미니 스크롤바 — 평소엔 숨어 있다가 움직일 때만 잠깐 나타난다.

ttk.Scrollbar 는 `pack` 으로 자리를 차지해 본문 폭을 15~17px 잡아먹는다. 미니PC
화면에서는 종목명 두 글자에 해당하는 폭이라 아깝다(2026-08-11 피드백). 그렇다고 아예
없애면 "전체 중 지금 어디쯤인가" 를 알 수 없다.

그래서 **`place` 로 위젯 위에 겹쳐 띄운다** — place 는 레이아웃 폭을 차지하지 않으므로
본문은 100% 폭을 그대로 쓰고, 막대는 스크롤이 일어나는 순간에만 보였다가 사라진다
(macOS·모바일 방식). 겹치는 건 오른쪽 4px 뿐이고 그마저도 평소에는 투명하다.

ttk.Scrollbar 를 얇게 만드는 대신 Canvas 로 직접 그린다 — 테마에 따라 화살표 버튼과
테두리가 남아 폭을 못 줄이는 경우가 있고, 색·굵기를 마음대로 정할 수도 있다.

사용법::

    scroll = MiniScroll(tree)      # tree 는 yview/yscrollcommand 를 가진 위젯
    # 끝. 휠·드래그·키보드 어느 쪽으로 움직여도 알아서 뜬다.
"""

from __future__ import annotations

import tkinter as tk

_WIDTH = 4  # 막대 폭 (px)
# 대상 위젯의 **테두리 안쪽**에 들어가도록 띄우는 여백. 오버레이는 대상 위에 얹히므로
# 여백이 없으면 위젯의 테두리를 덮어 프레임 선이 끊겨 보인다(2026-08-11 피드백).
_MARGIN_X = 3  # 오른쪽 테두리에서 띄우는 여백
_MARGIN_Y = 3  # 위·아래 테두리에서 띄우는 여백
_HIDE_MS = 1200  # 마지막 움직임 뒤 사라지기까지
_TROUGH = "#e8e8e8"
_THUMB = "#9e9e9e"
_MIN_THUMB = 18  # 막대 최소 길이 — 목록이 아주 길어도 잡히도록


class MiniScroll(tk.Canvas):
    """세로 스크롤 위치 표시기. 대상 위젯의 yscrollcommand 를 가로챈다.

    드래그로 이동도 된다 — 표시만 하고 조작이 안 되면 오히려 답답하다.
    """

    def __init__(self, target, width: int = _WIDTH):
        super().__init__(
            target.master,
            width=width,
            highlightthickness=0,
            borderwidth=0,
            bg=_TROUGH,
        )
        self._target = target
        self._width = width
        self._first = 0.0
        self._last = 1.0
        self._hide_job: str | None = None
        self._visible = False
        self._drag_offset = 0.0

        target.configure(yscrollcommand=self._on_view)
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        # 막대 위에 마우스가 있는 동안에는 사라지지 않게 (드래그하려는 참이다)
        self.bind("<Enter>", lambda _e: self._cancel_hide())
        self.bind("<Leave>", lambda _e: self._schedule_hide())

    # ── 표시 ───────────────────────────────────────────────────

    def _on_view(self, first, last) -> None:
        """대상 위젯이 알려주는 (보이는 구간 시작, 끝) 비율."""
        self._first, self._last = float(first), float(last)
        if self._first <= 0.0 and self._last >= 1.0:
            self._hide()  # 전부 보인다 — 스크롤바가 있을 이유가 없다
            return
        self._show()
        self._redraw()
        self._schedule_hide()

    def _show(self) -> None:
        if self._visible:
            return
        # 대상 위젯의 오른쪽 끝에 겹쳐 띄운다.
        # Tk 의 place 는 relheight 와 height 를 **더한다** — relheight=1.0 에 음수 height 를
        # 주면 "대상 높이에서 그만큼 뺀" 크기가 된다. 이걸로 테두리를 비켜 간다.
        self.place(
            in_=self._target,
            relx=1.0,
            rely=0.0,
            relheight=1.0,
            height=-2 * _MARGIN_Y,
            anchor="ne",
            x=-_MARGIN_X,
            y=_MARGIN_Y,
            width=self._width,
        )
        # 주의: Canvas.lift 는 tag_raise(도형 순서 바꾸기)로 덮여 있어 인자 없이 부르면
        # TclError 가 난다. 위젯 자체를 올리려면 Misc.lift 를 직접 호출해야 한다.
        tk.Misc.lift(self)
        self._visible = True

    def _hide(self) -> None:
        self._cancel_hide()
        if self._visible:
            self.place_forget()
            self._visible = False

    def _cancel_hide(self) -> None:
        if self._hide_job is not None:
            try:
                self.after_cancel(self._hide_job)
            except (tk.TclError, ValueError):
                pass
            self._hide_job = None

    def _schedule_hide(self) -> None:
        self._cancel_hide()
        self._hide_job = self.after(_HIDE_MS, self._hide)

    def _redraw(self) -> None:
        self.delete("all")
        height = self.winfo_height()
        if height <= 1:  # 아직 배치 전 — 다음 주기에 다시 그린다
            self.after(16, self._redraw)
            return
        top = self._first * height
        thumb = max(_MIN_THUMB, (self._last - self._first) * height)
        top = min(top, height - thumb)  # 끝에서 막대가 삐져나가지 않게
        self.create_rectangle(0, top, self._width, top + thumb, fill=_THUMB, outline="")

    # ── 드래그 ─────────────────────────────────────────────────

    def _on_press(self, event) -> None:
        height = max(self.winfo_height(), 1)
        thumb = max(_MIN_THUMB, (self._last - self._first) * height)
        top = min(self._first * height, height - thumb)
        # 막대를 잡았으면 잡은 지점을 유지하고, 빈 곳을 눌렀으면 그 자리로 점프한다
        inside = top <= event.y <= top + thumb
        self._drag_offset = (event.y - top) if inside else thumb / 2
        self._on_drag(event)

    def _on_drag(self, event) -> None:
        height = max(self.winfo_height(), 1)
        thumb = max(_MIN_THUMB, (self._last - self._first) * height)
        span = max(height - thumb, 1)
        fraction = (event.y - self._drag_offset) / span
        self._target.yview_moveto(min(max(fraction, 0.0), 1.0))
        self._cancel_hide()
