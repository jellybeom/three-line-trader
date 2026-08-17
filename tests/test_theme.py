"""화면 테마 — 라이트 / 다크 / 시스템.

색을 바꾸는 일이라 매매에는 영향이 없지만, **테마 코드에서 예외가 나면 창이 안 뜬다.**
그래서 어디서 실패하든 기본 모습으로라도 뜨는지, 그리고 두 모드 모두 글자가 읽히는지를
확인한다(실측: `#c62828` 은 다크 배경 대비 2.7 로 기준 4.5 미달이었다).
"""

from __future__ import annotations

import pytest

from trader.ui import theme


def _contrast(a: str, b: str) -> float:
    """두 색의 명암비 (WCAG). 4.5 이상이면 본문 글자로 읽을 만하다."""

    def lum(color: str) -> float:
        rgb = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        rgb = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]

    high, low = sorted((lum(a), lum(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("mode", [theme.LIGHT, theme.DARK])
def test_모든_의미색이_읽힌다(mode):
    """어두운 배경에서 같은 빨강·파랑을 쓰면 안 보인다."""
    c = theme.resolve(mode)
    for name in ("fg", "profit", "loss", "ok", "warn", "muted", "link"):
        color = getattr(c, name)
        assert _contrast(color, c.bg) >= 4.5, f"{mode} 의 {name}({color}) 이 흐리다"


@pytest.mark.parametrize("mode", [theme.LIGHT, theme.DARK])
def test_표_바탕에서도_읽힌다(mode):
    """종목 표는 창 바탕과 다른 색이다 — 거기서도 확인한다."""
    c = theme.resolve(mode)
    for name in ("fg", "profit", "loss", "muted"):
        assert _contrast(getattr(c, name), c.row) >= 4.5


def test_선택된_행의_글자가_읽힌다():
    for mode in (theme.LIGHT, theme.DARK):
        c = theme.resolve(mode)
        assert _contrast(c.select_fg, c.select_bg) >= 4.5


def test_라이트는_누런_기가_없는_중립_회색이다():
    """clam 기본(#dcdad5)은 누렇다 — Windows 기본(#d9d9d9)에 맞춘다."""
    r, g, b = (int(theme.LIGHT_PALETTE.bg[i : i + 2], 16) for i in (1, 3, 5))
    assert r == g == b, "회색이 아니다 (R·G·B 가 같아야 한다)"


def test_시스템_설정을_읽는다(monkeypatch):
    monkeypatch.setattr(theme, "system_prefers_dark", lambda: True)
    assert theme.resolve(theme.SYSTEM).name == theme.DARK
    monkeypatch.setattr(theme, "system_prefers_dark", lambda: False)
    assert theme.resolve(theme.SYSTEM).name == theme.LIGHT


def test_알_수_없는_설정은_시스템_설정을_따른다(monkeypatch):
    """설정 파일이 깨져도 화면이 이상해지지 않는다 — '시스템' 과 같게 본다.

    (예전 시험은 라이트를 기대했는데, 실행하는 PC 가 다크면 실패한다.)
    """
    monkeypatch.setattr(theme, "system_prefers_dark", lambda: True)
    assert theme.resolve("이상한값").name == theme.resolve(theme.SYSTEM).name
    monkeypatch.setattr(theme, "system_prefers_dark", lambda: False)
    assert theme.resolve("이상한값").name == theme.LIGHT


def test_설정을_저장하고_읽는다(tmp_path):
    theme.write_mode(theme.DARK, tmp_path)
    assert theme.read_mode(tmp_path) == theme.DARK


def test_설정_파일이_없으면_시스템이다(tmp_path):
    assert theme.read_mode(tmp_path / "없는폴더") == theme.SYSTEM


def test_망가진_설정_파일도_견딘다(tmp_path):
    (tmp_path / "theme.txt").write_text("이상한값", encoding="utf-8")
    assert theme.read_mode(tmp_path) == theme.SYSTEM


def test_윈도우가_아니면_라이트로_본다(monkeypatch):
    monkeypatch.setattr(theme.sys, "platform", "linux")
    assert theme.system_prefers_dark() is False


def test_고전위젯_색은_종류별로_다르다():
    """Listbox·Text·Menu 는 ttk 테마가 닿지 않아 직접 넣어야 한다."""
    theme.apply_light = None  # 미사용 표시 (린트 방지용 더미)
    text = theme.classic(None, "text")
    menu = theme.classic(None, "menu")
    listbox = theme.classic(None, "list")
    assert "insertbackground" in text  # 커서 색
    assert "activebackground" in menu  # 마우스 올린 항목
    assert "selectbackground" in listbox
    assert text["background"] == theme.palette().field


def test_테마_적용이_실패해도_예외가_나가지_않는다():
    """색이 안 맞는 것보다 창이 안 뜨는 게 나쁘다."""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("표시 장치가 없는 환경")
    root.withdraw()
    root.destroy()  # 이미 죽은 창에 적용해도 터지지 않아야 한다
    assert theme.apply(root, theme.DARK).name == theme.DARK


def test_버튼이_옆_위젯과_같은_높이다():
    """clam 기본 버튼 여백은 5 라 버튼만 33px 로 솟는다 (입력칸·콤보는 23px).

    툭 튀어나온 버튼은 줄이 들쭉날쭉해 보인다(2026-08-17 피드백).
    """
    tk = pytest.importorskip("tkinter")
    from tkinter import ttk

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("표시 장치가 없는 환경")
    root.geometry("400x200")
    theme.apply(root, theme.LIGHT)
    frame = ttk.Frame(root)
    frame.pack(fill="both", expand=True)
    widgets = {
        "button": ttk.Button(frame, text="연결"),
        "entry": ttk.Entry(frame, width=8),
        "combobox": ttk.Combobox(frame, width=8),
        "radio": ttk.Radiobutton(frame, text="모의"),
    }
    for i, w in enumerate(widgets.values()):
        w.grid(row=0, column=i)
    root.update()
    root.update_idletasks()
    heights = {k: w.winfo_height() for k, w in widgets.items()}
    root.destroy()
    assert len(set(heights.values())) == 1, f"높이가 제각각이다: {heights}"


# ── 창 제목 표시줄 (Windows 전용) ─────────────────────────────


def test_윈도우가_아니면_아무것도_하지_않는다(monkeypatch):
    tk = pytest.importorskip("tkinter")

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("표시 장치가 없는 환경")
    root.withdraw()
    monkeypatch.setattr(theme.sys, "platform", "linux")
    assert theme.apply_titlebar(root) is False
    root.destroy()


def test_제목_표시줄_적용이_실패해도_예외가_나가지_않는다(monkeypatch):
    """순수 표시용 호출이다 — 제목 색 때문에 창이 안 뜨는 쪽이 훨씬 나쁘다.

    Windows 인 척만 해서는 안 된다. 진짜 Windows 에서는 호출이 **성공**해 버려
    시험이 뒤집힌다(2026-08-18). ctypes 자체를 못 쓰게 만들어 실패를 강제한다.
    """
    tk = pytest.importorskip("tkinter")

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("표시 장치가 없는 환경")
    root.withdraw()

    class _FakeSys:
        platform = "win32"

    monkeypatch.setattr(theme, "sys", _FakeSys)
    monkeypatch.setitem(__import__("sys").modules, "ctypes", None)  # import 가 터진다

    assert theme.apply_titlebar(root) is False  # 조용히 실패
    root.destroy()


def test_색을_윈도우_형식으로_뒤집는다():
    """COLORREF 는 0x00BBGGRR 로 RGB 순서가 뒤집혀 있다."""
    assert theme._colorref("#123456") == 0x563412
    assert theme._colorref("#ff0000") == 0x0000FF
    assert theme._colorref("#000000") == 0


def test_죽은_창에_적용해도_터지지_않는다():
    tk = pytest.importorskip("tkinter")

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("표시 장치가 없는 환경")
    root.withdraw()
    dead = tk.Toplevel(root)
    dead.destroy()
    assert theme.apply_titlebar(dead) is False
    root.destroy()


def test_창_꾸미기는_한_번에_이뤄진다():
    """호출 지점을 나누면 새 창에서 한쪽을 빠뜨린다 (복기 차트 창에서 실제로 있었다)."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "trader" / "ui" / "icons.py"
    ).read_text(encoding="utf-8")
    assert "apply_titlebar" in source
