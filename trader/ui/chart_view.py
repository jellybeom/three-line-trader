"""보관된 차트 PNG 미리보기 — 휠로 확대/축소, 드래그로 이동.

일지 창의 차트는 세로 3:4 인데 표시 영역은 가로로 넓어, 창에 맞추면 좌우가 통째로
남고 그림은 작아진다. 축소된 그림으로는 캔들을 알아볼 수 없어 매번 원본을 따로 열어야
했다(2026-08-11 피드백). 그래서 **확대·이동**을 붙였다.

Tk 의 PhotoImage 는 정수배 zoom/subsample 만 되고 화질도 거칠어, 리샘플링은 Pillow 로
한다. Pillow 는 matplotlib 의 필수 의존이라 이미 설치돼 있다(없으면 자동으로 Tk 기본
정수배 축소로 물러난다 — 미리보기가 조금 거칠어질 뿐 창은 정상 동작한다).

성능: 확대할 때마다 **보이는 영역만** 잘라 리샘플링한다. 원본 전체(약 860×1050)를
배율만큼 키우면 메모리가 배율의 제곱으로 늘지만, 이 방식은 항상 표시 영역 크기라
확대를 아무리 해도 비용이 일정하다.
"""

from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import ttk

try:  # Pillow 는 matplotlib 의 의존성으로 이미 들어와 있다
    from PIL import Image, ImageTk

    _HAS_PIL = True
except ImportError:  # pragma: no cover - 설치 환경에서는 발생하지 않는다
    _HAS_PIL = False

_ZOOM_STEP = 1.25  # 휠 한 칸당 배율
_ZOOM_MIN = 1.0  # 창에 맞춘 크기보다 작게는 줄이지 않는다
_ZOOM_MAX = 8.0
_BG = "#ffffff"


class ChartView(ttk.Frame):
    """PNG 한 장을 보여주는 캔버스. 휠 확대, 드래그 이동, 더블클릭 원본 열기.

    scale 은 **'창에 맞춘 크기' 대비 배율**이다. 1.0 이면 그림 전체가 보이고, 2.0 이면
    두 배로 확대된 상태다. 원본 픽셀 배율이 아니라 화면 기준이라 창 크기가 바뀌어도
    사용자가 느끼는 확대 정도는 유지된다.
    """

    def __init__(self, master, path: str | None = None):
        super().__init__(master)
        self.canvas = tk.Canvas(
            self, highlightthickness=0, borderwidth=0, background=_BG
        )
        self.canvas.pack(fill="both", expand=True)

        self._path: str | None = None
        self._source: "Image.Image | None" = None
        self._photo = None  # 참조를 유지하지 않으면 그림이 즉시 사라진다
        self._item: int | None = None
        self._scale = 1.0
        self._center = (0.5, 0.5)  # 화면 한가운데에 오는 원본 상의 상대 좌표
        self._drag: tuple[int, int] | None = None
        self._redraw_job: str | None = None

        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", lambda _e: self._set_cursor())
        self.canvas.bind("<Double-Button-1>", self._open_original)
        # 휠: Windows/macOS 는 <MouseWheel>, X11 은 Button-4/5 로 온다
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-4>", lambda e: self._on_wheel(e, delta=120))
        self.canvas.bind("<Button-5>", lambda e: self._on_wheel(e, delta=-120))
        if path:
            self.show(path)

    # ── 공개 API ───────────────────────────────────────────────

    def show(self, path: str | None) -> None:
        """차트를 바꾼다. 경로가 없거나 열 수 없으면 안내 문구를 보여준다."""
        self._path = path
        self._source = None
        self._scale, self._center = 1.0, (0.5, 0.5)
        if not path or not Path(path).exists():
            self._message("보관된 차트가 없습니다")
            return
        if not _HAS_PIL:
            self._show_with_tk(path)
            return
        try:
            image = Image.open(path)
            image.load()  # 파일 핸들을 즉시 놓아준다 (창을 오래 띄워두므로)
        except OSError as err:
            self._message(f"이미지를 열 수 없습니다: {err}")
            return
        self._source = image.convert("RGB")
        self._redraw()

    def reset(self) -> None:
        """확대를 풀고 창에 맞춘 크기로 되돌린다."""
        self._scale, self._center = 1.0, (0.5, 0.5)
        self._redraw()

    # ── 표시 ───────────────────────────────────────────────────

    def _message(self, text: str) -> None:
        self.canvas.delete("all")
        self._photo = self._item = None
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        self.canvas.create_text(
            width // 2, height // 2, text=text, fill="#9e9e9e", tags="msg"
        )
        self._set_cursor()

    def _show_with_tk(self, path: str) -> None:
        """Pillow 가 없을 때의 대비책 — Tk 기본 정수배 축소."""
        try:
            photo = tk.PhotoImage(file=path)
        except tk.TclError as err:
            self._message(f"이미지를 열 수 없습니다: {err}")
            return
        area = max(self.canvas.winfo_height(), 1)
        factor = max(1, -(-photo.height() // area))
        if factor > 1:
            photo = photo.subsample(factor, factor)
        self.canvas.delete("all")
        self._photo = photo
        self._item = self.canvas.create_image(
            self.canvas.winfo_width() // 2,
            self.canvas.winfo_height() // 2,
            image=photo,
            anchor="center",
        )

    def _fit_scale(self) -> float:
        """원본 → 창에 맞추는 배율 (가로·세로 중 더 빡빡한 쪽)."""
        if self._source is None:
            return 1.0
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        src_w, src_h = self._source.size
        return min(width / src_w, height / src_h)

    def _redraw(self) -> None:
        if self._source is None:
            return
        view_w = max(self.canvas.winfo_width(), 1)
        view_h = max(self.canvas.winfo_height(), 1)
        if view_w <= 1 or view_h <= 1:  # 아직 배치 전 — 다음 주기에
            self.after(16, self._redraw)
            return

        ratio = self._fit_scale() * self._scale  # 원본 픽셀 → 화면 픽셀
        src_w, src_h = self._source.size
        # 화면에 담을 원본 영역의 크기 (원본 픽셀 단위)
        crop_w = min(src_w, view_w / ratio)
        crop_h = min(src_h, view_h / ratio)
        cx = self._clamp(self._center[0] * src_w, crop_w / 2, src_w - crop_w / 2)
        cy = self._clamp(self._center[1] * src_h, crop_h / 2, src_h - crop_h / 2)
        self._center = (cx / src_w, cy / src_h)  # 가장자리에서 더 못 가도록 되돌린다

        box = (
            round(cx - crop_w / 2),
            round(cy - crop_h / 2),
            round(cx + crop_w / 2),
            round(cy + crop_h / 2),
        )
        out = (max(1, round(crop_w * ratio)), max(1, round(crop_h * ratio)))
        # 보이는 부분만 잘라서 리샘플링 — 확대해도 비용이 늘지 않는다
        view = self._source.resize(out, Image.LANCZOS, box=box)

        self._photo = ImageTk.PhotoImage(view)
        self.canvas.delete("all")
        self._item = self.canvas.create_image(
            view_w // 2, view_h // 2, image=self._photo, anchor="center"
        )
        self._set_cursor()

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        if high < low:  # 그림이 화면보다 작으면 가운데 고정
            return (low + high) / 2
        return min(max(value, low), high)

    def _set_cursor(self) -> None:
        movable = self._source is not None and self._scale > 1.0
        self.canvas.configure(cursor="fleur" if movable else "")

    # ── 입력 ───────────────────────────────────────────────────

    def _on_resize(self, _event=None) -> None:
        # 창 크기 조절 중에는 이벤트가 연달아 오므로, 잠깐 모았다가 한 번만 그린다
        if self._redraw_job is not None:
            try:
                self.after_cancel(self._redraw_job)
            except (tk.TclError, ValueError):
                pass
        self._redraw_job = self.after(60, self._redraw)

    def _on_wheel(self, event, delta: int | None = None) -> None:
        if self._source is None:
            return
        step = delta if delta is not None else event.delta
        old = self._scale
        factor = _ZOOM_STEP if step > 0 else 1 / _ZOOM_STEP
        self._scale = min(max(self._scale * factor, _ZOOM_MIN), _ZOOM_MAX)
        if self._scale == old:
            return
        if self._scale == _ZOOM_MIN:
            self._center = (0.5, 0.5)  # 전체 보기로 돌아오면 가운데 정렬
        else:
            self._zoom_at(event.x, event.y, old)
        self._redraw()

    def _zoom_at(self, x: int, y: int, old_scale: float) -> None:
        """커서 밑의 지점이 제자리에 남도록 중심을 옮긴다 (지도 확대와 같은 느낌)."""
        if self._source is None:
            return
        src_w, src_h = self._source.size
        ratio = self._fit_scale() * old_scale
        view_w = max(self.canvas.winfo_width(), 1)
        view_h = max(self.canvas.winfo_height(), 1)
        # 커서가 가리키는 원본 좌표
        px = self._center[0] * src_w + (x - view_w / 2) / ratio
        py = self._center[1] * src_h + (y - view_h / 2) / ratio
        new_ratio = self._fit_scale() * self._scale
        self._center = (
            (px - (x - view_w / 2) / new_ratio) / src_w,
            (py - (y - view_h / 2) / new_ratio) / src_h,
        )

    def _on_press(self, event) -> None:
        self._drag = (event.x, event.y)
        if self._scale > 1.0:
            self.canvas.configure(cursor="fleur")

    def _on_drag(self, event) -> None:
        if self._source is None or self._drag is None or self._scale <= 1.0:
            return
        dx, dy = event.x - self._drag[0], event.y - self._drag[1]
        self._drag = (event.x, event.y)
        ratio = self._fit_scale() * self._scale
        src_w, src_h = self._source.size
        self._center = (
            self._center[0] - dx / ratio / src_w,
            self._center[1] - dy / ratio / src_h,
        )
        self._redraw()

    def _open_original(self, _event=None) -> None:
        if self._path and hasattr(os, "startfile"):
            try:
                os.startfile(self._path)  # type: ignore[attr-defined]
            except OSError:
                pass
