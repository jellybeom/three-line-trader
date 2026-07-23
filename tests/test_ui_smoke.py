"""UI 스모크 테스트 — 창을 실제로 띄워 '클릭해도 아무 일도 안 나는' 류의 버그를 잡는다.

로직 테스트와 달리 Tk 위젯을 실제로 생성하므로, 화면이 없는 환경(CI 등)에서는 자동으로
건너뛴다. Windows 개발 환경에서는 그대로 실행되어 다음을 지킨다:

- 편집/등록 다이얼로그가 실제로 열리는지 (튜플 언패킹 불일치 등으로 조용히 실패하지 않는지)
- 편집 프리필이 상태·수량·메모·콤마까지 정확한지

Tk 콜백 안에서 난 예외는 콘솔에만 찍히고 화면에는 아무 반응이 없어 놓치기 쉽다.
"""

import time
import tkinter as tk

import pytest

from trader.state_machine import Params, Position, State
from trader.ui import bus

P = Params(
    line1=10_000, line2=9_000, line3=8_000, buy1_amount=1_000_000, buy2_amount=900_000
)


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    """모달 대화상자는 사용자의 응답을 기다리며 테스트를 멈춰 세운다 — 전부 무력화."""
    for name, result in (
        ("showwarning", None),
        ("showinfo", None),
        ("showerror", None),
        ("askyesno", True),
    ):
        monkeypatch.setattr(f"trader.ui.app.messagebox.{name}", lambda *a, **k: result)


@pytest.fixture
def app():
    """화면이 없으면 skip. 있으면 App 을 띄우고 초기 이벤트까지 반영한다.

    Tk 루트는 프로세스에 하나만 있어야 안정적이므로, 화면 감지용 임시 루트를 따로
    만들지 않고 App 생성 자체의 성공 여부로 판별한다.
    """
    from trader.ui.app import App

    b = bus.Bus()
    try:
        window = App(b)
    except tk.TclError:
        pytest.skip("표시 장치가 없는 환경 — UI 스모크 생략")
    # 창을 숨기면(withdraw) 모달 다이얼로그의 grab_set 이 멈추므로 숨기지 않는다.
    # 대신 화면 밖으로 치워 테스트 중 시야를 가리지 않게 한다.
    window.geometry("+3000+3000")
    b.events.put(bus.Funds(10_000_000, 10, 500_000, 500_000))
    b.events.put(bus.TradeDate("2026-07-22"))
    _pump(window, lambda: window._funds is not None)  # 폴링 주기(200ms)를 기다린다
    yield window
    for child in window.winfo_children():  # 열린 모달 창부터 정리 (grab 해제)
        if isinstance(child, tk.Toplevel):
            child.grab_release()
            child.destroy()
    window.update()
    window.destroy()


def _pump(window, until=None, seconds: float = 3.0) -> None:
    """조건이 만족될 때까지(또는 제한 시간까지) Tk 이벤트를 처리한다."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        window.update()
        if until is not None and until():
            return
        time.sleep(0.05)


def _dialogs(window):
    from trader.ui.register_dialog import RegisterDialog

    return [w for w in window.winfo_children() if isinstance(w, RegisterDialog)]


def _add_symbol(window, memo: str = "", position: Position | None = None) -> None:
    position = position or Position(
        state=State.BUY1, avg_price=10_000, total_bought=5, remaining=5
    )
    window._bus.events.put(bus.PositionUpdate("005930", "삼성전자", position, P, memo))
    _pump(window, lambda: "005930" in window._registry)


def test_등록_다이얼로그가_열린다(app):
    app._open_register()
    app.update()
    assert _dialogs(app), "종목 추가 창이 열리지 않음"


def test_편집_다이얼로그가_열리고_프리필된다(app):
    _add_symbol(app, memo="메모테스트")
    app._open_edit("005930")
    app.update()
    dialogs = _dialogs(app)
    assert dialogs, "편집 창이 열리지 않음 (콜백 예외 가능성)"
    d = dialogs[0]
    assert d._vars["symbol"].get() == "005930"
    assert d._vars["line1"].get() == "10,000"  # 콤마 프리필
    assert d._vars["avg_price"].get() == "10,000"
    assert d._vars["remaining"].get() == "5"
    assert d._vars["memo"].get() == "메모테스트"
    assert d._state.get() == State.BUY1.value  # 상태까지 수정 가능해야 함


def test_편집_저장이_상태까지_바꾸는_명령으로_나간다(app):
    """외부에서 직접 손절한 경우 등 — 편집으로 상태·잔량을 계좌와 맞출 수 있어야 한다."""
    _add_symbol(app)
    app._open_edit("005930")
    app.update()
    d = _dialogs(app)[0]
    d._state.set(State.CLOSED.value)
    d._vars["remaining"].set("0")
    d._submit()
    app.update()

    commands = []
    while not app._bus.commands.empty():
        commands.append(app._bus.commands.get_nowait())
    registers = [c for c in commands if isinstance(c, bus.Register)]
    assert registers, "편집 저장이 명령으로 나가지 않음"
    cmd = registers[-1]
    assert cmd.edit is True  # 기존 종목 덮어쓰기가 허용되는 편집 경로
    assert cmd.position.state is State.CLOSED
    assert cmd.position.remaining == 0
    assert cmd.position.total_bought == 5  # 건드리지 않은 값은 유지


def test_감시_중_편집은_차단되고_창이_열리지_않는다(app):
    _add_symbol(app)
    app._bus.events.put(bus.WatchStatus(True))
    _pump(app, lambda: app._running)
    app._open_edit("005930")
    app.update()
    assert not _dialogs(app), "감시 중인데 편집 창이 열림"


def test_행_클릭이_각_버튼_콜백으로_연결된다(app):
    """✎ / ✕ / 📈 열 인덱스가 실제 콜백과 맞는지 (열 추가 시 밀리는 사고 방지)."""
    from trader.ui import positions_view as pv

    _add_symbol(app)
    called = []
    app.positions._on_edit = lambda s: called.append(("edit", s))
    app.positions._on_chart = lambda s: called.append(("chart", s))
    app.positions._confirm_delete = lambda s: called.append(("del", s))

    class FakeEvent:
        x = y = 0

    for col in ("chart", "edit", "del"):
        event = FakeEvent()
        app.positions.tree.identify_row = lambda _y: "005930"
        index = pv._COLUMNS.index(col) + 1
        app.positions.tree.identify_column = lambda _x, i=index: f"#{i}"
        app.positions._on_click(event)

    assert called == [("chart", "005930"), ("edit", "005930"), ("del", "005930")]
