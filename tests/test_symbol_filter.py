"""메인 창 종목 검색(필터) — 살아 있는 화면을 가리는 기능이라 특히 촘촘히 본다.

필터는 '보이지 않게 하는' 기능이다. 잘못 만들면 체결·손절이 난 종목이 화면에서 사라진
채로 남거나(가장 위험), 반대로 숨긴 종목이 틱마다 되살아나 필터가 무의미해진다.
Treeview 의 detach 동작을 실측해 확인한 것들을 여기에 고정한다.

- detach 된 행에도 set/item 은 통한다 → 숨은 동안에도 값이 갱신된다
- detach 하면 selection 은 Tk 가 비우지만 focus 는 남는다 → 직접 비워야 한다
- get_children() 은 숨긴 행을 빼고 준다 → 정렬·전체 삭제에 쓰면 누락된다
- 없는 iid 에 set/detach/move 는 TclError → 항상 exists() 를 먼저 본다
"""

from __future__ import annotations

import pytest

from trader.state_machine import Params, Position, State
from trader.ui import bus

_ADD_ROWS = ("__add__", "__csv__")


@pytest.fixture
def app():
    tk = pytest.importorskip("tkinter")
    from trader.ui.app import App

    try:
        window = App(bus.Bus())
    except tk.TclError:
        pytest.skip("표시 장치가 없는 환경")
    window.geometry("1400x800+3000+3000")
    window.update()
    yield window
    window.destroy()


def _params() -> Params:
    return Params(10_000, 9_500, 9_000, 100_000, 100_000)


@pytest.fixture
def filled(app):
    """종목 6개를 등록한 상태."""
    for code, name in (
        ("005930", "삼성전자"),
        ("079650", "서산"),
        ("228340", "동양파일"),
        ("123330", "제닉"),
        ("053260", "금강철강"),
        ("352480", "씨앤씨인터내셔널"),
    ):
        app.positions.upsert(code, name, Position(), _params())
    app.update()
    return app


def _visible(app) -> list[str]:
    return [i for i in app.positions.tree.get_children() if i not in _ADD_ROWS]


def _search(app, text: str) -> None:
    app._open_search()
    app._search.entry.delete(0, "end")
    app._search.entry.insert(0, text)
    app._search._on_key()
    app.update()


# ── 기본 동작 ─────────────────────────────────────────────────


def test_검색줄은_평소_숨어_있다가_Ctrl_F_로_뜬다(filled):
    assert not filled._search_bar.winfo_ismapped()
    filled._open_search()
    filled.update()
    assert filled._search_bar.winfo_ismapped()


def test_종목명으로_거른다(filled):
    _search(filled, "서산")
    assert _visible(filled) == ["079650"]


def test_종목코드_일부로도_거른다(filled):
    _search(filled, "3524")
    assert _visible(filled) == ["352480"]


def test_메모나_상태는_검색하지_않는다(filled):
    """대상을 넓히면 '왜 이게 걸리지?' 가 생긴다 — 이름과 코드만 본다."""
    filled.positions.upsert(
        "005930", "삼성전자", Position(), _params(), memo="서산 관련주"
    )
    filled.update()
    _search(filled, "서산")
    assert _visible(filled) == ["079650"]  # 메모가 걸리면 삼성전자도 나왔을 것


def test_맞는_종목이_없으면_비어_보인다(filled):
    _search(filled, "없는종목")
    assert _visible(filled) == []
    assert "0/6" in filled._search_count.cget("text")


def test_추가_행은_필터_중에도_맨_아래_남는다(filled):
    """0건일 때 바로 '등록하자' 로 이어져야 한다."""
    _search(filled, "없는종목")
    children = filled.positions.tree.get_children()
    assert children[-len(_ADD_ROWS) :] == _ADD_ROWS


# ── 숨긴 종목이 되살아나지 않는가 (가장 중요) ─────────────────


def test_틱이_와도_숨긴_종목은_나타나지_않는다(filled):
    _search(filled, "서산")
    for _ in range(100):
        filled.positions.tick("005930", 71_000)
        filled.positions.tick("228340", 3_200)
    filled.update()
    assert _visible(filled) == ["079650"]


def test_upsert_가_숨긴_종목을_되살리지_않는다(filled):
    """exists() 가 숨긴 행에도 True 라 값만 갱신될 것 같지만,
    _ensure_add_row 의 move 와 신규 insert 경로에서 되살아난다."""
    _search(filled, "서산")
    filled.positions.upsert(
        "005930", "삼성전자", Position(State.BUY1, 70_000, 10, 10), _params()
    )
    filled.positions.upsert(
        "228340",
        "동양파일",
        Position(State.CLOSED, 3_300, 10, 0, realized_pnl=-2_000),
        _params(),
    )
    filled.update()
    assert _visible(filled) == ["079650"]


def test_새로_등록된_종목도_조건에_안_맞으면_숨는다(filled):
    _search(filled, "서산")
    filled.positions.upsert("999999", "새종목", Position(), _params())
    filled.update()
    assert _visible(filled) == ["079650"]
    assert filled.positions.count() == 7  # 목록에는 들어와 있다


def test_조건에_맞는_새_종목은_보인다(filled):
    _search(filled, "서")
    filled.positions.upsert("999999", "서울종목", Position(), _params())
    filled.update()
    assert set(_visible(filled)) == {"079650", "999999"}


def test_숨은_동안의_값_변화가_되돌리면_보인다(filled):
    """detach 된 행에도 set 은 통한다 — 숨긴 동안 멈춰 있으면 안 된다."""
    _search(filled, "서산")
    filled.positions.tick("005930", 71_000)
    filled._close_search()
    filled.update()
    assert "71,000" in filled.positions.tree.set("005930", "price")


# ── 보이지 않는 종목은 제어할 수 없다 ─────────────────────────


def test_숨긴_종목은_선택되지_않는다(filled):
    """수동 청산·차트·수정이 모두 selected() 를 쓴다."""
    _search(filled, "서산")
    filled.positions.tree.selection_set("005930")
    assert filled.positions.selected() is None


def test_선택한_종목이_숨겨지면_선택이_풀린다(filled):
    filled.positions.tree.selection_set("005930")
    assert filled.positions.selected() == "005930"
    _search(filled, "서산")
    assert filled.positions.selected() is None


def test_숨긴_종목은_focus_에도_남지_않는다(filled):
    """selection 은 Tk 가 비우지만 focus 는 남는다 (실측)."""
    filled.positions.tree.focus("005930")
    _search(filled, "서산")
    current = filled.positions.tree.focus()
    assert current == "" or current in _ADD_ROWS


# ── 정렬과의 관계 ─────────────────────────────────────────────


def test_정렬은_숨긴_종목까지_포함한다(filled):
    """보이는 것만 정렬하면 필터를 풀었을 때 순서가 뒤죽박죽이 된다."""
    _search(filled, "서산")
    filled.positions._sort("code")
    filled._close_search()
    filled.update()
    codes = _visible(filled)
    assert codes == sorted(codes, reverse=True) or codes == sorted(codes)
    assert len(codes) == 6  # 숨었던 것도 제자리를 찾았다


def test_필터를_껐다_켜도_정렬이_유지된다(filled):
    filled.positions._sort("code")
    before = _visible(filled)
    _search(filled, "서")
    filled._close_search()
    filled.update()
    assert _visible(filled) == before


# ── 닫기 동작 ─────────────────────────────────────────────────


def test_Esc_는_두_단계다(filled):
    """1단계: 글자만 지운다(줄은 남는다) / 2단계: 줄을 닫는다."""
    _search(filled, "서산")
    filled._on_search_escape()
    filled.update()
    assert filled._search_bar.winfo_ismapped()  # 줄은 남아 있다
    assert filled._search.get() == ""
    assert len(_visible(filled)) == 6  # 필터는 풀렸다

    filled._on_search_escape()
    filled.update()
    assert not filled._search_bar.winfo_ismapped()


def test_X_버튼은_글자가_있어도_바로_닫는다(filled):
    _search(filled, "서산")
    filled._close_search()
    filled.update()
    assert not filled._search_bar.winfo_ismapped()
    assert len(_visible(filled)) == 6


def test_검색줄을_닫으면_필터도_풀린다(filled):
    """줄이 없는데 종목만 줄어 있으면 왜 안 보이는지 알 수 없다."""
    _search(filled, "서산")
    assert len(_visible(filled)) == 1
    filled._close_search()
    filled.update()
    assert len(_visible(filled)) == 6


# ── 목록 교체·삭제 ────────────────────────────────────────────


def test_매매일_전환은_숨긴_종목까지_지운다(filled):
    """get_children() 만 지우면 필터에 걸린 종목이 유령처럼 남는다."""
    _search(filled, "서산")
    filled.positions.clear()
    filled.update()
    assert filled.positions.tree.get_children() == _ADD_ROWS
    assert filled.positions.count() == 0


def test_매매일_전환은_필터를_초기화한다(filled):
    """다른 날의 종목 구성에 어제 검색어를 적용하면 결과가 엉뚱해진다."""
    _search(filled, "서산")
    filled.positions.clear()
    filled.positions.upsert("111111", "새날종목", Position(), _params())
    filled.update()
    assert _visible(filled) == ["111111"]


def test_필터_중에_종목을_지워도_터지지_않는다(filled):
    """없는 iid 에 set/detach/move 는 TclError 다 — exists 확인이 빠지면 터진다."""
    _search(filled, "서")
    filled.positions.remove("079650")
    filled.positions.upsert("005930", "삼성전자", Position(), _params())
    filled.update()
    assert filled.positions.count() == 5


def test_개수_표시가_전체와_보이는_수를_함께_준다(filled):
    _search(filled, "서산")
    assert filled._search_count.cget("text") == "1/6종목"
    filled._close_search()
    filled.update()
    assert filled._search_count.cget("text") == ""


# ── 입력 반영 시점 ────────────────────────────────────────────


def test_글자가_들어오는_즉시_필터가_걸린다(filled):
    """KeyRelease 에만 걸면 한글 조합이 확정되는 순간을 놓친다.

    실측(2026-08-15): '서산' 을 치면 '산' 이 조합 중이라 위젯에는 '서' 만 들어 있고,
    다른 곳을 클릭해야 '산' 이 확정되며 들어왔다. 그때 KeyRelease 는 오지 않으므로
    필터가 갱신되지 않았다. 변수 변경을 보면 어떤 경로로 들어오든 잡힌다.
    """
    filled._open_search()
    filled._search.entry.insert(0, "서")  # 키 이벤트 없이 값만 넣는다
    filled.update()
    assert filled._search_count.cget("text").startswith("1/")

    filled._search.entry.insert("end", "산")  # 조합 확정으로 뒤늦게 들어온 글자
    filled.update()
    assert _visible(filled) == ["079650"]


def test_안내_문구는_검색어로_치지_않는다(filled):
    """안내 문구도 위젯에 들어가는 글자라, 변수 변경이 그것까지 잡으면 안 된다."""
    filled._open_search()
    filled._close_search()
    filled.update()
    assert len(_visible(filled)) == 6  # 안내 문구로 걸러지지 않았다


# ── 검색줄 생김새 ─────────────────────────────────────────────


def test_무엇을_검색하는지_라벨이_있다(filled):
    labels = [
        w.cget("text")
        for w in filled._search_bar.winfo_children()
        if w.winfo_class() == "TLabel"
    ]
    assert any("검색" in text for text in labels)


def test_입력칸이_창_너비만큼_늘어나지_않는다(filled):
    """늘리면 입력칸만 덩그러니 길어져 보기 나쁘다."""
    filled._open_search()
    filled.update()
    filled.update_idletasks()
    assert filled._search.winfo_width() < filled.winfo_width() // 3


def test_닫기와_지우기는_모양으로_구분된다(filled):
    """✕ 가 둘이면 어느 쪽이 무엇인지 알 수 없다 — 지우기는 아이콘을 쓴다."""
    buttons = [
        w.cget("text")
        for w in filled._search_bar.winfo_children()
        if w.winfo_class() == "TButton"
    ]
    assert buttons == ["✕"]  # 줄에 직접 붙은 버튼은 '닫기' 하나뿐

    clear = filled._search._clear
    if filled._search._clear_on is not None:
        assert "pyimage" in str(clear.cget("image"))  # 아이콘이 붙어 있다
        assert clear.cget("text") == ""  # 글자와 겹쳐 보이지 않는다
    else:
        assert clear.cget("text") == "✕"  # 아이콘을 못 읽으면 글자로 물러난다


def test_검색줄과_표_사이에_최소_간격만_둔다(filled):
    """붙여 놓으면 입력칸 테두리와 표 머리글이 한 선처럼 보인다.

    예전에는 한글 조합 창을 피하려고 10px 를 뒀는데 검색줄이 세로를 너무 먹었다
    (2026-08-18 피드백). 조합 중 머리글에 잠깐 걸칠 수 있지만 머리글은 가려도
    잃는 정보가 없어 세로 공간을 택했다.
    """
    filled._open_search()
    filled.update()
    pady = filled._search_bar.pack_info()["pady"]
    bottom = pady[1] if isinstance(pady, (tuple, list)) else int(str(pady).split()[-1])
    assert 1 <= int(bottom) <= 4


# ── 매매일 개장/휴장 표시 ─────────────────────────────────────


def test_휴장일은_사유까지_빨갛게_보여준다(app):
    """달력의 '빨간 날' 관례. 주황은 이 프로그램에서 '진행 중·주의' 를 뜻한다."""
    app._dispatch(bus.TradeDate("2026-08-17", "휴장", "광복절(대체휴일)"))
    app.update()
    assert app._weekday.cget("text") == "(월) · 휴장 · 광복절(대체휴일)"
    from trader.ui import theme

    assert str(app._weekday.cget("foreground")) == theme.palette().profit


def test_개장일은_기본색이다(app):
    app._dispatch(bus.TradeDate("2026-08-18", "개장", ""))
    app.update()
    assert app._weekday.cget("text") == "(화) · 개장"
    assert str(app._weekday.cget("foreground")) == ""


def test_확인_불가는_회색이다(app):
    app._dispatch(bus.TradeDate("2027-03-02", "확인 불가", ""))
    app.update()
    from trader.ui import theme

    assert "확인 불가" in app._weekday.cget("text")
    assert str(app._weekday.cget("foreground")) == theme.palette().muted


def test_개장_여부를_모르면_요일만_보여준다(app):
    """옛 이벤트(market 없음)를 받아도 화면이 깨지지 않는다."""
    app._dispatch(bus.TradeDate("2026-08-18"))
    app.update()
    assert app._weekday.cget("text") == "(화)"


def test_날짜를_넘겨도_폭이_변하지_않는다(app):
    """폭이 들쭉날쭉하면 옆 그룹(키움·Discord·자금)이 밀린다."""
    app._dispatch(bus.TradeDate("2026-08-17", "휴장", "광복절(대체휴일)"))
    app.update()
    app.update_idletasks()
    wide = app._weekday.winfo_width()
    app._dispatch(bus.TradeDate("2026-08-18", "개장", ""))
    app.update()
    app.update_idletasks()
    assert app._weekday.winfo_width() == wide


def test_휴장_사유가_잘리지_않는다(app):
    """Tk 의 width 는 **'0' 문자 폭**이 단위다. 한글은 1.5~2배 넓어 글자 수를 그대로
    넣으면 폰트에 따라 뒷부분이 잘린다(2026-08-17 Windows 실측: '개천절(대체휴일' )."""
    from tkinter import font as tkfont

    metrics = tkfont.Font(font=app._weekday.cget("font") or "TkDefaultFont")
    app.update_idletasks()
    for note in ("광복절(대체휴일)", "석가탄신일(대체휴일)", "연말휴장일", "주말"):
        app._dispatch(bus.TradeDate("2026-10-05", "휴장", note))
        app.update()
        app.update_idletasks()
        text = app._weekday.cget("text")
        assert metrics.measure(text) <= app._weekday.winfo_width(), f"{note} 가 잘린다"


def test_폭_계산은_폰트를_실측한다():
    """글자 수로 어림하면 폰트가 바뀔 때 다시 잘린다."""
    tk = pytest.importorskip("tkinter")
    from tkinter import font as tkfont, ttk

    from trader.ui.app import _MARKET_SAMPLE, _width_in_chars

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("표시 장치가 없는 환경")
    root.withdraw()
    longest = "(월) · 휴장 · 석가탄신일(대체휴일)"
    for size in (9, 12, 16, 24):  # 폰트가 커져도 여유가 남아야 한다
        font = tkfont.Font(family="DejaVu Sans", size=size)
        label = ttk.Label(root, font=font)
        chars = _width_in_chars(label, _MARKET_SAMPLE)
        assert chars * font.measure("0") >= font.measure(longest)
    root.destroy()


# ── 창 아이콘 ─────────────────────────────────────────────────


def test_모든_창에_같은_아이콘이_붙는다(app):
    """새 창을 만들 때 아이콘을 빠뜨리기 쉬워, 한 곳에서 관리한다.

    실제로 복기 차트 창에는 아이콘이 없었고 매매일지·등록 창은 `.ico` 만 시도해
    Windows 밖에서는 조용히 실패했다(2026-08-18 점검).
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "trader" / "ui"
    for path in root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        # Toplevel 을 만들거나 상속하는 파일은 apply_icon 을 써야 한다
        makes_window = re.search(r"tk\.Toplevel\(|\(tk\.Toplevel\)|\(tk\.Tk\)", source)
        if makes_window:
            assert "apply_icon" in source, f"{path.name} 에 창 아이콘이 없다"


def test_아이콘_파일이_실제로_있다():
    from trader.ui.icons import CLEAR_ICON, CLEAR_ICON_OFF

    assert CLEAR_ICON.exists()
    assert CLEAR_ICON_OFF.exists()


def test_아이콘이_없어도_창은_뜬다(app, tmp_path):
    """아이콘은 있으면 좋은 것이지 창을 못 띄울 이유가 아니다."""
    from trader.ui.icons import apply_icon, load_photo

    assert load_photo(tmp_path / "없는파일.png") is None
    apply_icon(app)  # 예외가 나지 않는다


def test_지우기_아이콘은_비활성일_때_흐려진다(filled):
    """테마에 따라 비활성 상태에서도 그림이 그대로 진하게 나온다."""
    search = filled._search
    if search._clear_on is None:
        pytest.skip("아이콘을 못 읽는 환경")
    filled._open_search()
    filled.update()
    # cget("image") 는 튜플로 온다 — 이름만 뽑아 비교한다
    assert str(search._clear_off) in str(search._clear.cget("image"))
    assert "disabled" in search._clear.state()

    search.entry.insert(0, "서")
    filled.update()
    assert str(search._clear_on) in str(search._clear.cget("image"))
    assert "disabled" not in search._clear.state()


def test_검색줄은_입력칸보다_두꺼워지지_않는다(filled):
    """기본 버튼은 위아래 여백이 붙어 입력칸보다 커지고, 그만큼 줄이 두꺼워진다.

    검색줄은 표 위에 얹히는 보조 도구라 세로로 얇을수록 좋다(2026-08-18 피드백:
    28px → 22px).
    """
    filled.update_idletasks()
    before = filled.positions.winfo_height()
    filled._open_search()
    filled.update()
    filled.update_idletasks()
    entry_height = filled._search.entry.winfo_height()

    for widget in filled._search_bar.winfo_children():
        if widget.winfo_class() == "TButton":
            assert widget.winfo_height() <= entry_height + 2
    assert filled._search_bar.winfo_height() <= entry_height + 2
    # 줄 자체뿐 아니라 **표에서 실제로 뺏는 세로**를 본다 (아래 여백까지 포함)
    taken = before - filled.positions.winfo_height()
    assert taken <= entry_height + 6, f"검색줄이 {taken}px 를 차지한다"


# ── 보유기간 열 ─────────────────────────────────────────────────


def test_보유기간_열은_해당_칸만_갱신된다(app):
    """분 단위로만 바뀌는 값 때문에 18칸을 통째로 다시 쓰지 않는다."""
    app.positions.upsert("005930", "삼성전자", Position(), _params(), holding="47분")
    app.update()
    assert app.positions.tree.set("005930", "hold") == "47분"

    app.positions.set_holding("005930", "1시간 2분")
    app.update()
    assert app.positions.tree.set("005930", "hold") == "1시간 2분"
    assert app.positions.tree.set("005930", "name") == "삼성전자"  # 나머지는 그대로


def test_보유기간_정렬은_사전순이_아니라_기간순이다(app):
    """글자로 세우면 '2일차' 가 '47분' 앞에 온다 — 분으로 환산해 정렬한다.

    첫 클릭이 내림차순인 것은 다른 열과 같은 동작이다 (오래 들고 있는 것부터).
    """
    for code, held in (
        ("005930", "47분"),
        ("079650", "3시간 12분"),
        ("228340", "2일차"),
    ):
        app.positions.upsert(code, code, Position(), _params(), holding=held)
    app.update()

    app.positions._sort("hold")  # 첫 클릭 — 오래된 것부터
    app.update()
    assert app.positions._order[:3] == ["228340", "079650", "005930"]

    app.positions._sort("hold")  # 다시 누르면 뒤집힌다
    app.update()
    assert app.positions._order[:3] == ["005930", "079650", "228340"]


def test_보유기간이_빈_종목은_정렬에서_맨_뒤로_간다(app):
    """진입 전 종목이 '오래 들고 있는 것' 자리에 끼어들면 안 된다."""
    app.positions.upsert("005930", "삼성전자", Position(), _params(), holding="47분")
    app.positions.upsert("079650", "서산", Position(), _params())
    app.update()

    app.positions._sort("hold")
    app.update()
    assert app.positions._order[:2] == ["005930", "079650"]


def test_진입_전_종목은_보유기간이_다른_열과_같게_대시다(app):
    """한 칸만 비어 있으면 값이 없는 건지 화면이 덜 그려진 건지 구분되지 않는다."""
    app.positions.upsert("005930", "삼성전자", Position(), _params())
    app.update()
    assert app.positions.tree.set("005930", "hold") == "-"
    # 다른 열도 같은 모양이다 (오른쪽 정렬 열은 여백이 붙어 strip 해서 비교)
    assert app.positions.tree.set("005930", "avg").strip() == "-"


def test_보유기간을_지우면_다시_대시가_된다(app):
    app.positions.upsert("005930", "삼성전자", Position(), _params(), holding="47분")
    app.update()

    app.positions.set_holding("005930", "")
    app.update()
    assert app.positions.tree.set("005930", "hold") == "-"
    assert "005930" not in app.positions.holding_symbols()  # 갱신 대상에서 빠진다
