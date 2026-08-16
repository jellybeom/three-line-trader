"""창 아이콘과 작은 버튼 아이콘 — 한 곳에서 관리한다.

아이콘 경로가 파일마다 흩어져 있으면 새 창을 만들 때 빠뜨리기 쉽다. 실제로 복기 차트
창에는 아이콘이 붙지 않았고, 매매일지·등록 창은 `.ico` 만 시도해 Windows 밖에서는
조용히 실패했다(2026-08-18 점검).

**모든 창은 같은 아이콘을 쓴다.** 창마다 다른 아이콘을 주는 것은 데스크톱 관례가
아니고(대화상자는 작업 표시줄에 따로 뜨지도 않는다), 한 프로그램이라는 느낌을 해친다.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[2] / "assets"

_APP_ICO = ASSETS / "three-line-trader.ico"
_APP_PNG = ASSETS / "three-line-trader-512.png"
CLEAR_ICON = ASSETS / "clear-text.png"
CLEAR_ICON_OFF = ASSETS / "clear-text-off.png"


def apply_icon(window) -> None:
    """창에 앱 아이콘을 붙인다. 실패해도 창은 그대로 뜬다.

    Windows 는 `.ico`, 그 밖의 환경은 `.png` 를 쓴다. 아이콘이 없다고 창을 못 띄우면
    안 되므로 모든 실패를 삼킨다 — 아이콘은 있으면 좋은 것이지 필수가 아니다.
    """
    try:
        if _APP_ICO.exists():
            window.iconbitmap(str(_APP_ICO))
            return
    except tk.TclError:
        pass  # Windows 밖에서는 .ico 를 못 읽는다 — 아래 png 로 간다
    try:
        if photo := load_photo(_APP_PNG, window):
            # 창에 붙여 참조를 유지한다 — 놓치면 파이썬이 회수해 아이콘이 사라진다
            window._app_icon = photo  # type: ignore[attr-defined]
            window.iconphoto(False, photo)
    except tk.TclError:
        pass


def load_photo(path: Path, master=None) -> tk.PhotoImage | None:
    """PNG 를 PhotoImage 로 (없거나 못 읽으면 None).

    **캐시하지 않고 창마다 새로 만든다.** PhotoImage 는 만들어진 Tk 인터프리터에 묶여
    있어, 캐시해 두면 창을 닫았다 다시 열 때 `image "pyimage2" doesn't exist` 로
    터진다. 대신 **부르는 쪽이 참조를 들고 있어야 한다** — 파이썬 참조가 사라지면
    그림도 함께 사라진다.
    """
    if not path.exists():
        return None
    try:
        return tk.PhotoImage(file=str(path), master=master)
    except tk.TclError:  # Tk 가 PNG 를 못 읽는 아주 오래된 환경
        return None
