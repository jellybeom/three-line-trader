"""테스트 공용 설정.

**같은 프로세스 안에 Tk 루트를 둘 이상 만들지 않는다.**

테스트마다 `tk.Tk()` 를 새로 만들면 두 번째부터 Tcl 인터프리터가 하나 더 생기는데,
윈도우에서는 그때 Tcl 라이브러리를 다시 읽으려다 실패하는 일이 있다.

    couldn't read file ".../tcl/tk8.6/text.tcl": no such file or directory

파이썬을 재설치해도 그대로였고(2026-09-05), 실행할 때마다 건너뛰는 개수가 1~2개로
오갔다 — 파일이 아예 없었다면 늘 같은 개수여야 한다. 즉 **먼저 만들어진 루트가 남아
있느냐**가 갈랐다는 뜻이고, `pytest-randomly` 로 순서가 바뀌니 결과도 흔들렸다.

세션 내내 루트 하나를 띄워 두는 방법도 있는데, 그러면 `App`(=`tk.Tk` 상속)을 만드는
테스트가 두 번째 루트가 되어 문제가 그대로 옮겨 간다. 그래서 **이미 있으면 빌려 쓰고
없을 때만 만든다.** 빌린 루트는 부수지 않는다 — 만든 쪽이 정리한다.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def tk_root():
    """숨겨진 Tk 루트. 살아 있는 루트가 있으면 그것을 빌려 쓴다."""
    tk = pytest.importorskip("tkinter")
    existing = getattr(tk, "_default_root", None)
    if existing is not None:
        try:
            if existing.winfo_exists():
                yield existing  # 남의 것 — 부수지 않는다
                return
        except tk.TclError:
            pass  # 이미 죽은 루트가 남아 있다 — 새로 만든다
    try:
        root = tk.Tk()
    except tk.TclError as err:  # pragma: no cover - 화면 없는 환경
        pytest.skip(f"Tk 루트를 만들 수 없음: {err}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


@pytest.fixture(autouse=True)
def _close_stray_windows():
    """테스트가 남긴 창을 정리한다.

    테스트가 실패하거나 도중에 예외가 나면 뒷정리 코드가 실행되지 않아 창이 남는다.
    남은 루트는 다음 `tk.Tk()` 를 **두 번째 루트**로 만들어 위의 문제를 재현시킨다.
    그래서 **테스트 전에는 없었는데 끝나고 생긴 루트**만 골라 정리한다 — 빌려 쓰던
    남의 루트는 건드리지 않는다.
    """
    try:
        import tkinter as tk
    except ImportError:  # pragma: no cover
        yield
        return
    before = getattr(tk, "_default_root", None)
    yield
    root = getattr(tk, "_default_root", None)
    if root is None:
        return
    try:
        for child in list(root.winfo_children()):
            if isinstance(child, tk.Toplevel):
                child.destroy()
        if root is not before:  # 이 테스트가 만들어 놓고 안 치운 루트
            root.destroy()
        else:
            root.update_idletasks()
    except tk.TclError:
        pass
