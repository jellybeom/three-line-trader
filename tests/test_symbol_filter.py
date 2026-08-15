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
