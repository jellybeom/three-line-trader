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


# ── CSV 불러오기 결과 기록 (2026-08-06) ───────────────────────


def test_CSV_태그와_기준봉이_등록_알림까지_전달된다(app, tmp_path):
    """파싱 → Register 명령 → 등록 알림까지 선정 근거가 끊기지 않아야 한다."""
    from unittest.mock import patch

    from trader.ui import bus

    csv_path = tmp_path / "w.csv"
    csv_path.write_text(
        "종목코드,종목명,메모,1선,2선,3선,태그,기준봉\n"
        '900290,GRT,메모 내용,3355,3215,3105,"#KOSPI상승장,#테마주",2026-08-05\n',
        encoding="utf-8-sig",
    )

    app._funds = bus.Funds(
        total=2_025_000, max_symbols=5, buy1_amount=405_000, buy2_amount=405_000
    )
    with (
        patch("trader.ui.app.filedialog.askopenfilename", return_value=str(csv_path)),
        patch("trader.ui.app.messagebox.showinfo"),
    ):
        app._import_csv()

    cmds = _drain_commands(app._bus)
    reg = [c for c in cmds if isinstance(c, bus.Register)][0]
    assert reg.tags == "KOSPI상승장,테마주" and reg.base_date == "2026-08-05"
    assert reg.memo == "메모 내용"

    notice = [c for c in cmds if isinstance(c, bus.RegistrationNotice)][0]
    row = notice.rows[0]
    assert row["tags"] == "KOSPI상승장,테마주"
    assert row["base_date"] == "2026-08-05"
    assert row["qty"] == 405_000 // 3355  # 1차 예상 수량 (소량 경고 판단용)


def test_CSV_등록_실패는_로그와_알림으로도_남는다(app, tmp_path):
    """팝업은 닫으면 사라진다 — 입력 실수는 나중에 되짚을 수 있어야 한다."""
    from unittest.mock import patch

    from trader.ui import bus

    csv_path = tmp_path / "w.csv"
    csv_path.write_text(
        "종목코드,종목명,1선,2선,3선\n"
        "005930,삼성전자,10000,9000,8000\n"
        "195870,해성디에스,4030,38350,37200\n",  # 1선 오타 (40300 → 4030)
        encoding="utf-8-sig",
    )

    app._funds = bus.Funds(
        total=1_000_000, max_symbols=5, buy1_amount=100_000, buy2_amount=100_000
    )
    with (
        patch("trader.ui.app.filedialog.askopenfilename", return_value=str(csv_path)),
        patch("trader.ui.app.messagebox.showinfo"),
        patch("trader.ui.app.messagebox.showwarning"),
    ):
        app._import_csv()

    notices, registers = [], []
    while not app._bus.commands.empty():
        cmd = app._bus.commands.get_nowait()
        if isinstance(cmd, bus.RegistrationNotice):
            notices.append(cmd)
        elif isinstance(cmd, bus.Register):
            registers.append(cmd.symbol)

    assert registers == ["005930"]  # 정상 종목만 등록
    assert notices, "등록 결과 알림이 없음"
    fail = notices[0].warnings[0]
    assert "해성디에스(195870)" in fail
    assert "4,030" in fail and "38,350" in fail  # 어떤 값이 잘못됐는지 알 수 있다


def test_문제가_없으면_요약만_남긴다(app, tmp_path):
    from unittest.mock import patch

    from trader.ui import bus

    csv_path = tmp_path / "w.csv"
    csv_path.write_text(
        "종목코드,종목명,1선,2선,3선\n005930,삼성전자,10000,9000,8000\n",
        encoding="utf-8-sig",
    )
    app._funds = bus.Funds(
        total=1_000_000, max_symbols=5, buy1_amount=100_000, buy2_amount=100_000
    )
    with (
        patch("trader.ui.app.filedialog.askopenfilename", return_value=str(csv_path)),
        patch("trader.ui.app.messagebox.showinfo"),
    ):
        app._import_csv()

    notices = [
        c for c in _drain_commands(app._bus) if isinstance(c, bus.RegistrationNotice)
    ]
    assert len(notices) == 1 and notices[0].warnings == ()


def _drain_commands(b):
    out = []
    while not b.commands.empty():
        out.append(b.commands.get_nowait())
    return out


# ── 기준봉 날짜 위젯 (2026-08-07) ─────────────────────────────


def _dialog(app, edit=None):
    from trader.ui import bus
    from trader.ui.register_dialog import RegisterDialog

    sent = []
    funds = bus.Funds(
        total=1_000_000, max_symbols=5, buy1_amount=100_000, buy2_amount=100_000
    )
    dialog = RegisterDialog(app, on_submit=sent.append, funds=funds, edit=edit)
    app.update()
    return dialog, sent


def test_신규_등록의_기준봉은_비어있다(app):
    """달력 위젯이 오늘 날짜를 자동으로 채우면, 모르는 기준봉이 오늘로 기록된다."""
    dialog, _ = _dialog(app)
    assert dialog._vars["base_date"].get() == ""


def test_기준봉을_고르고_지울_수_있다(app):
    dialog, sent = _dialog(app)
    if dialog._base_date is None:
        pytest.skip("tkcalendar 미설치")

    dialog._base_date.set_date("2026-08-05")
    app.update()
    assert dialog._vars["base_date"].get() == "2026-08-05"

    dialog._vars["base_date"].set("")  # '지우기' 버튼과 같은 동작
    app.update()
    assert dialog._vars["base_date"].get() == ""


def test_선택한_기준봉과_태그가_명령에_실린다(app):
    dialog, sent = _dialog(app)
    dialog._vars["symbol"].set("005930")
    dialog._vars["name"].set("삼성전자")
    for key, value in (("line1", "10,000"), ("line2", "9,000"), ("line3", "8,000")):
        dialog._vars[key].set(value)
    dialog._vars["base_date"].set("2026-08-05")
    dialog._tag_vars["테마주"].set(True)
    dialog._submit()

    assert sent[-1].base_date == "2026-08-05"
    assert sent[-1].tags == "테마주"


def test_편집시_기존_기준봉이_유지된다(app):
    """생성 직후 비우는 동작이 프리필을 덮어쓰면 안 된다."""
    from trader.state_machine import Params, Position

    params = Params(
        line1=10_000, line2=9_000, line3=8_000, buy1_amount=100_000, buy2_amount=100_000
    )
    dialog, _ = _dialog(
        app,
        edit=(
            "005930",
            "삼성전자",
            params,
            Position(),
            "메모",
            "테마주,상한가",
            "2026-07-30",
        ),
    )
    app.update()
    assert dialog._vars["base_date"].get() == "2026-07-30"
    assert {t for t, v in dialog._tag_vars.items() if v.get()} == {"테마주", "상한가"}


# ── 다중 선택 메뉴 · 신규 열 (2026-08-10) ─────────────────────


def _fill(app, symbols):
    from trader.state_machine import Params, Position
    from trader.ui import bus

    app._dispatch(bus.TradeDate("2026-08-10"))
    params = Params(
        line1=10_000, line2=9_000, line3=8_000, buy1_amount=200_000, buy2_amount=200_000
    )
    for sym, name, base in symbols:
        app._dispatch(
            bus.PositionUpdate(sym, name, Position(), params, "메모", "테마주", base)
        )
    app.update()


def test_가격_열은_오른쪽_정렬에_여백을_둔다(app):
    """셀 끝에 바싹 붙으면 읽기 불편하다 (Treeview 는 셀 여백을 지원하지 않는다)."""
    from trader.state_machine import Params, Position
    from trader.ui import bus

    params = Params(
        line1=10_000, line2=9_000, line3=8_000, buy1_amount=200_000, buy2_amount=200_000
    )
    app._dispatch(bus.TradeDate("2026-08-10"))
    app._dispatch(
        bus.PositionUpdate(
            "005930",
            "삼성전자",
            Position(
                state=State.BUY1,
                avg_price=9_900,
                total_bought=20,
                remaining=20,
                realized_pnl=12_345,
            ),
            params,
        )
    )
    app._dispatch(bus.Tick("005930", 10_290))
    app.update()

    tree = app.positions.tree
    for col in ("price", "avg", "realized", "line1", "line2", "line3"):
        assert str(tree.column(col)["anchor"]) == "e", col
        assert tree.set("005930", col).endswith("  "), col


def test_가격이_아닌_열은_가운데_정렬(app):
    from trader.state_machine import Params, Position
    from trader.ui import bus

    params = Params(
        line1=10_000, line2=9_000, line3=8_000, buy1_amount=200_000, buy2_amount=200_000
    )
    app._dispatch(bus.PositionUpdate("005930", "삼성전자", Position(), params))
    app.update()

    tree = app.positions.tree
    for col in (
        "code",
        "name",
        "state",
        "change",
        "qty",
        "pnl",
        "range",
        "base",
        "memo",
    ):
        assert str(tree.column(col)["anchor"]) == "center", col


def test_열_제목(app):
    tree = app.positions.tree
    assert tree.heading("range")["text"] == "1선↔3선"
    assert tree.heading("change")["text"] == "등락률"
    assert tree.heading("base")["text"] == "기준봉"


def test_등락률_열은_첫_체결가_대비로_계산된다(app):
    from trader.ui import bus

    _fill(app, [("005930", "삼성전자", "")])
    app._dispatch(bus.Tick("005930", 10_000))  # 첫 체결가가 기준이 된다
    app._dispatch(bus.Tick("005930", 10_500))
    app.update()
    assert app.positions.tree.set("005930", "change") == "+5.00%"


def test_기준봉_열은_코어가_계산한_거래일을_보여준다(app):
    """공휴일 반영은 코어(거래일 달력)가 하고, 화면은 받은 값을 표시만 한다."""
    from trader.state_machine import Params, Position
    from trader.ui import bus

    params = Params(
        line1=10_000, line2=9_000, line3=8_000, buy1_amount=200_000, buy2_amount=200_000
    )
    app._dispatch(bus.TradeDate("2026-08-18"))
    app._dispatch(
        bus.PositionUpdate(
            "005930", "삼성전자", Position(), params, "", "", "2026-08-14", 0, 1
        )
    )
    app._dispatch(
        bus.PositionUpdate(
            "000660", "하이닉스", Position(), params, "", "", "", 0, None
        )
    )
    app.update()
    tree = app.positions.tree
    assert tree.set("005930", "base") == "D+1"
    assert tree.set("000660", "base") == ""  # 기준봉이 없으면 빈칸


def test_매매일이_바뀌면_등락률_기준도_초기화된다(app):
    from trader.ui import bus

    _fill(app, [("005930", "삼성전자", "")])
    app._dispatch(bus.Tick("005930", 10_000))
    app.update()
    app.positions.clear()
    assert app.positions._day_open == {}


def test_여러_종목을_선택해_이월할_수_있다(app):
    _fill(app, [("005930", "삼성전자", ""), ("000660", "하이닉스", "")])
    called = []
    app.positions._on_carry_position = called.append
    app.positions._menu_targets = ["005930", "000660"]
    app.positions._call(app.positions._on_carry_position)
    assert called == ["005930", "000660"]


def test_한_종목_전용_기능은_다중_선택에서_동작하지_않는다(app):
    """편집·수동 청산·리셋은 한 종목씩 다뤄야 하는 기능이다."""
    _fill(app, [("005930", "삼성전자", ""), ("000660", "하이닉스", "")])
    called = []
    app.positions._on_edit = called.append
    app.positions._menu_targets = ["005930", "000660"]
    app.positions._call(app.positions._on_edit, single=True)
    assert called == []

    app.positions._menu_targets = ["005930"]  # 하나만 고르면 동작한다
    app.positions._call(app.positions._on_edit, single=True)
    assert called == ["005930"]


def test_메뉴_순서가_의도대로다(app):
    menu = app.positions._menu
    labels = [
        menu.entrycget(i, "label")
        for i in range(menu.index("end") + 1)
        if menu.type(i) == "command"
    ]
    assert labels == [
        "다음 매매일로 이월 (상태·평단·수량)",
        "다음 매매일로 이월 (전체)",
        "수동 전량 청산 (시장가)",
        "종료 → 대기 초기화",
        "차트 보기",
        "편집",
        "관심종목 제외",
    ]
