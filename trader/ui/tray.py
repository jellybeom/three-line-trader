"""트레이 아이콘 — 창을 닫아도 매매가 멈추지 않게 한다.

왜 필요한가
-----------
미니PC 는 모니터가 없어 원격 접속으로만 화면을 본다. 터치로 조작하다 창의 ✕ 를 잘못
누르면 그대로 프로그램이 종료되고, **그날 매매가 통째로 멈춘다**(2026-09-02 실제로 겪음).
창을 닫으면 트레이로 내려가게 하면 그 사고가 사라진다.

동작
----
- 창 ✕ → 트레이로 숨김 (프로그램은 계속 돈다)
- 트레이 아이콘 왼쪽 클릭 / '열기' → 창 복귀
- 트레이 메뉴 '종료' → **여기서만** 진짜 종료한다

'종료' 를 트레이 메뉴에만 둔 이유는 ✕ 와 멀리 떨어뜨리기 위해서다. 실수로 닫는 경로와
일부러 끄는 경로가 같은 자리에 있으면 아무 의미가 없다.

주의
----
pystray 의 아이콘 루프는 **별도 스레드**에서 돌고, Tkinter 위젯은 메인 스레드에서만
만질 수 있다. 그래서 메뉴에서 창을 조작할 때는 반드시 `root.after(0, ...)` 로 메인
스레드에 넘긴다. 직접 부르면 조용히 멎거나 알 수 없는 오류로 죽는다.

pystray 가 없거나 트레이를 못 만드는 환경(리눅스 CI 등)에서는 **아무것도 하지 않는다.**
트레이는 편의 기능이라, 이것 때문에 매매 프로그램이 안 뜨면 본말이 뒤바뀐다.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    import tkinter as tk

_TITLE = "three-line-trader"
_ICON = Path(__file__).resolve().parents[2] / "assets" / "three-line-trader-512.png"

# 상태 점 색. 작업 표시줄에서 아이콘은 16~32px 로 그려지므로, 글자나 모양이 아니라
# **색**으로만 구분해야 알아볼 수 있다.
_DOT = {
    "감시 중": (46, 204, 113),  # 초록
    "중지": (127, 133, 140),  # 회색
    "미연결": (231, 76, 60),  # 빨강
}


def _fallback_image(size: int):
    """아이콘 파일이 없을 때의 대비책 — 3선을 그린다."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    unit = size / 64
    for y, color in ((20, (231, 76, 60)), (32, (241, 196, 15)), (44, (52, 152, 219))):
        draw.line(
            [12 * unit, y * unit, size - 12 * unit, y * unit],
            fill=color,
            width=max(2, int(4 * unit)),
        )
    return image


def make_image(state: str = "중지", size: int = 128):
    """트레이 아이콘 그림. 오른쪽 아래에 **상태 점**을 얹는다.

    점을 쓰는 이유는 트레이 아이콘이 16~32px 로 줄어들기 때문이다. 그 크기에서는 글자도
    모양도 구분되지 않아 색만 남는다. 흰 테두리를 두르는 것은 밝은 작업 표시줄과 어두운
    작업 표시줄 양쪽에서 점이 묻히지 않게 하기 위해서다.
    """
    from PIL import Image, ImageDraw

    try:
        base = Image.open(_ICON).convert("RGBA").resize((size, size), Image.LANCZOS)
    except (OSError, ValueError):
        base = _fallback_image(size)

    r = size * 0.30  # 점 지름
    pad = size * 0.02
    box = [size - r - pad, size - r - pad, size - pad, size - pad]
    draw = ImageDraw.Draw(base)
    draw.ellipse(box, fill=(255, 255, 255, 255))  # 흰 테두리
    inset = size * 0.035
    draw.ellipse(
        [box[0] + inset, box[1] + inset, box[2] - inset, box[3] - inset],
        fill=_DOT.get(state, _DOT["중지"]) + (255,),
    )
    return base


class Tray:
    """앱에 붙는 트레이 아이콘. 만들 수 없으면 조용히 비활성 상태로 남는다."""

    def __init__(self, root: "tk.Misc", on_quit, on_open_folder=None) -> None:
        self._root = root
        self._on_quit = on_quit
        self._on_open_folder = on_open_folder
        self._icon = None
        self._thread = None
        self._state = "중지"
        self._tooltip = _TITLE

    @property
    def active(self) -> bool:
        return self._icon is not None

    def start(self) -> bool:
        """트레이 아이콘을 띄운다. 성공하면 True.

        실패해도 예외를 내보내지 않는다 — 트레이가 없다고 매매를 못 하면 안 된다.
        호출부는 False 일 때 ✕ 를 평소대로(=종료) 두면 된다.
        """
        try:
            import pystray
        except ImportError:
            return False
        try:
            menu = pystray.Menu(
                pystray.MenuItem("열기", self._show, default=True),
                pystray.MenuItem("폴더 열기", self._open_folder),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("종료", self._quit),
            )
            self._icon = pystray.Icon(
                _TITLE, make_image(self._state), self._tooltip, menu
            )
            self._thread = threading.Thread(target=self._icon.run, daemon=True)
            self._thread.start()
        except Exception:  # noqa: BLE001 — 트레이 실패가 프로그램을 막지 않게
            self._icon = None
            return False
        return True

    def update(self, state: str, tooltip: str) -> None:
        """상태 점과 툴팁을 갱신한다. 바뀐 것이 없으면 아무것도 하지 않는다.

        pystray 는 아이콘을 새로 그릴 때마다 OS 에 갱신을 요청하므로, 틱마다 부르면
        낭비다. 호출부가 주기를 정하고 여기서 같은 값을 걸러 낸다.
        """
        if self._icon is None or (state, tooltip) == (self._state, self._tooltip):
            return
        redraw = state != self._state
        self._state, self._tooltip = state, tooltip
        try:
            self._icon.title = tooltip
            if redraw:
                self._icon.icon = make_image(state)
        except Exception:  # noqa: BLE001
            pass

    def hide_window(self) -> None:
        """창을 트레이로 내린다."""
        self._root.withdraw()

    def _show(self, _icon=None, _item=None) -> None:
        # pystray 스레드에서 불린다 — Tkinter 는 메인 스레드에서만 만진다.
        self._root.after(0, self._show_now)

    def _show_now(self) -> None:
        self._root.deiconify()
        self._root.lift()
        self._root.focus_force()

    def _open_folder(self, _icon=None, _item=None) -> None:
        if self._on_open_folder is not None:
            self._root.after(0, self._on_open_folder)

    def _quit(self, _icon=None, _item=None) -> None:
        self._root.after(0, self._on_quit)

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass
            self._icon = None
