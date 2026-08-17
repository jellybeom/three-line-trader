"""화면 테마 — 라이트 / 다크 / 시스템 따름.

**두 모드 모두 `clam` 테마를 쓴다.** Windows 기본 테마(`vista`)는 배경색 변경을 무시해
다크를 만들 수 없고, 모드마다 테마를 바꾸면 위젯 크기가 미묘하게 달라져 지금까지 맞춰 온
폭·높이(검색줄 높이, 매매일 라벨 폭 등)를 모드별로 다시 검증해야 한다. 같은 테마에
색만 갈아끼우면 배치는 그대로다.

clam 기본 배경(`#dcdad5`)은 누런 기가 돌아 Windows 기본(`#d9d9d9`)에 가까운 중립 회색으로
덮는다.

색은 **의미**로 정해 두고 모드마다 값을 바꾼다. 어두운 배경에서는 같은 빨강·파랑이
읽히지 않기 때문이다(실측: `#c62828` 은 다크 배경 대비 2.7 로 기준 4.5 미달).
라이트에서도 주황(1.7)·회색(2.4)이 미달이라 함께 손봤다.
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from dataclasses import dataclass
from tkinter import ttk

LIGHT = "라이트"
DARK = "다크"
SYSTEM = "시스템"
MODES = (SYSTEM, LIGHT, DARK)
DEFAULT_MODE = SYSTEM

# 버튼 여백 (좌우, 위아래). 위아래 0 이면 입력칸·콤보와 같은 높이가 된다.
_BUTTON_PADDING = (6, 0)


@dataclass(frozen=True)
class Palette:
    """한 모드의 색 묶음. 이름은 **뜻**이지 색이 아니다."""

    name: str
    bg: str  # 창·그룹 바탕
    fg: str  # 본문 글자
    field: str  # 입력칸 안쪽
    row: str  # 표 바탕
    head: str  # 표 머리글
    border: str
    select_bg: str
    select_fg: str
    hover: str

    profit: str  # 이익 · 실전 모드 · 휴장 (국내 관례로 빨강)
    loss: str  # 손실 (파랑)
    ok: str  # 정상 · 연결됨
    warn: str  # 진행 중 · 주의
    muted: str  # 비활성 · 부가 설명
    link: str  # 누를 수 있는 글자 (＋추가 행)


LIGHT_PALETTE = Palette(
    name=LIGHT,
    bg="#d9d9d9",  # Windows 기본과 같은 중립 회색 (clam 기본은 누렇다)
    fg="#1a1a1a",
    field="#ffffff",
    row="#ffffff",
    head="#e8e8e8",
    border="#a0a0a0",
    select_bg="#cce0f5",
    select_fg="#000000",
    hover="#e6e6e6",
    # 회색 바탕(#d9d9d9)은 흰색보다 어두워 기존 색으로는 대비가 모자란다
    # (#c62828 은 3.98). 표(흰 바탕)와 창(회색 바탕) 양쪽에서 4.5 를 넘도록 낮췄다.
    profit="#b71c1c",
    loss="#0d47a1",
    ok="#1b5e20",
    warn="#8a5000",  # 기존 #f9a825 는 흰 배경에서 대비 1.7 로 거의 안 보였다
    muted="#55595d",  # 기존 #9e9e9e 는 2.4
    link="#0d47a1",
)

DARK_PALETTE = Palette(
    name=DARK,
    bg="#232629",
    fg="#e6e6e6",
    field="#1b1e20",
    row="#1b1e20",
    head="#2e3236",
    border="#4a4f54",
    select_bg="#2f5d8a",
    select_fg="#ffffff",
    hover="#31363b",
    profit="#ff6b6b",  # 어두운 배경에서 #c62828 은 대비 2.7 → 5.5
    loss="#64b5f6",  # 2.6 → 6.9
    ok="#66bb6a",  # 3.0 → 6.4
    warn="#ffca28",
    muted="#9aa0a6",
    link="#64b5f6",
)

_current = LIGHT_PALETTE


def _setting_file(db_dir: Path) -> Path:
    return db_dir / "theme.txt"


def read_mode(db_dir: str | Path = "data") -> str:
    """저장된 테마 설정. 파일이 없거나 이상하면 '시스템'.

    DB 가 아니라 작은 파일에 둔다 — 창을 만들기 **전에** 읽어야 하는데 DB 는 코어
    스레드 것이라 그 시점에 열려 있지 않다. 투자 모드(mode.txt)와 같은 방식이다.
    """
    try:
        value = _setting_file(Path(db_dir)).read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_MODE
    return value if value in MODES else DEFAULT_MODE


def write_mode(mode: str, db_dir: str | Path = "data") -> None:
    directory = Path(db_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _setting_file(directory).write_text(mode, encoding="utf-8")


def palette() -> Palette:
    """지금 적용된 색 묶음. 위젯을 만들 때 이 값을 쓴다."""
    return _current


def resolve(mode: str) -> Palette:
    """설정값 → 실제 팔레트. '시스템' 이면 운영체제 설정을 읽는다."""
    if mode == DARK:
        return DARK_PALETTE
    if mode == LIGHT:
        return LIGHT_PALETTE
    return DARK_PALETTE if system_prefers_dark() else LIGHT_PALETTE


def system_prefers_dark() -> bool:
    """운영체제가 다크 모드인가. 알 수 없으면 False(라이트).

    Windows 는 레지스트리에서 읽는다. **시작할 때 한 번만** 본다 — 실행 중 설정이
    바뀌어도 따라가지 않는다(감시하려면 계속 읽어야 하는데 그만한 이득이 없다).
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        with key:
            # AppsUseLightTheme: 1=라이트, 0=다크
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return value == 0
    except (ImportError, OSError, ValueError):
        return False


def apply(root: tk.Misc, mode: str) -> Palette:
    """테마를 적용하고 쓰인 팔레트를 돌려준다.

    실패해도 예외를 올리지 않는다 — 색이 안 맞는 것보다 창이 안 뜨는 게 나쁘다.
    """
    global _current
    colors = resolve(mode)
    _current = colors
    try:
        _apply_ttk(root, colors)
    except tk.TclError:
        pass
    return colors


def _apply_ttk(root: tk.Misc, c: Palette) -> None:
    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure(
        ".",
        background=c.bg,
        foreground=c.fg,
        fieldbackground=c.field,
        bordercolor=c.border,
        lightcolor=c.bg,
        darkcolor=c.bg,
        troughcolor=c.field,
        insertcolor=c.fg,
        arrowcolor=c.fg,
        focuscolor=c.select_bg,
    )
    style.map(
        ".",
        background=[("active", c.hover)],
        foreground=[("disabled", c.muted)],
    )
    style.configure("TLabelframe", background=c.bg, bordercolor=c.border)
    style.configure("TLabelframe.Label", background=c.bg, foreground=c.muted)
    # clam 기본 버튼 여백은 5 라 버튼만 33px 로 솟아 옆 위젯(입력칸·콤보·라디오 23px)보다
    # 툭 튀어나온다. 위아래 여백을 없애 같은 높이로 맞춘다 — 좌우는 넉넉히 둬야
    # 글자가 답답해 보이지 않는다.
    style.configure(
        "TButton", background=c.hover, bordercolor=c.border, padding=_BUTTON_PADDING
    )
    style.map("TButton", background=[("active", c.select_bg), ("disabled", c.bg)])
    style.configure("TEntry", fieldbackground=c.field, foreground=c.fg)
    style.configure("TCombobox", fieldbackground=c.field, foreground=c.fg)
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", c.field)],
        foreground=[("readonly", c.fg)],
        selectbackground=[("readonly", c.select_bg)],
    )
    style.configure(
        "Treeview", background=c.row, fieldbackground=c.row, foreground=c.fg
    )
    style.map(
        "Treeview",
        background=[("selected", c.select_bg)],
        foreground=[("selected", c.select_fg)],
    )
    style.configure("Treeview.Heading", background=c.head, foreground=c.fg)
    style.map("Treeview.Heading", background=[("active", c.hover)])
    style.configure("TNotebook.Tab", background=c.head, foreground=c.muted)
    style.map(
        "TNotebook.Tab",
        background=[("selected", c.bg)],
        foreground=[("selected", c.fg)],
    )
    # 검색줄 버튼은 폭도 좁게 (아이콘·✕ 한 글자짜리)
    style.configure("Search.TButton", padding=(2, 0))
    root.configure(background=c.bg)


def classic(widget: tk.Misc, kind: str = "") -> dict:
    """고전 tk 위젯(Listbox·Text·Menu·Canvas)에 넣을 색 묶음.

    ttk 테마는 이 위젯들에 닿지 않아 직접 지정해야 한다.
    """
    c = _current
    common = {"background": c.field, "foreground": c.fg}
    if kind == "menu":
        return {
            **common,
            "activebackground": c.select_bg,
            "activeforeground": c.select_fg,
        }
    if kind == "text":
        return {
            **common,
            "insertbackground": c.fg,
            "selectbackground": c.select_bg,
            "selectforeground": c.select_fg,
        }
    if kind == "list":
        return {
            **common,
            "selectbackground": c.select_bg,
            "selectforeground": c.select_fg,
        }
    return common
