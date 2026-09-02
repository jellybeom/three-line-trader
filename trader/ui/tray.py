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
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    import tkinter as tk

_TITLE = "three-line-trader"


def _make_image():
    """아이콘 그림. 3선 전략이 보이도록 가로선 세 개를 그린다."""
    from PIL import Image, ImageDraw

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        [2, 2, size - 3, size - 3], radius=12, fill=(32, 34, 38, 255)
    )
    for y, color in ((20, (231, 76, 60)), (32, (241, 196, 15)), (44, (52, 152, 219))):
        draw.line([12, y, size - 12, y], fill=color, width=4)
    return image


class Tray:
    """앱에 붙는 트레이 아이콘. 만들 수 없으면 조용히 비활성 상태로 남는다."""

    def __init__(self, root: "tk.Misc", on_quit) -> None:
        self._root = root
        self._on_quit = on_quit
        self._icon = None
        self._thread = None

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
                pystray.MenuItem("종료", self._quit),
            )
            self._icon = pystray.Icon(_TITLE, _make_image(), _TITLE, menu)
            self._thread = threading.Thread(target=self._icon.run, daemon=True)
            self._thread.start()
        except Exception:  # noqa: BLE001 — 트레이 실패가 프로그램을 막지 않게
            self._icon = None
            return False
        return True

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

    def _quit(self, _icon=None, _item=None) -> None:
        self.stop()
        self._root.after(0, self._on_quit)

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass
            self._icon = None
