"""화면 갱신이 어떤 이벤트에도 죽지 않는지 검증한다.

2026-08-13 실제 사고: 코어가 bus.Blocked 를 보냈는데 PositionsView 에 set_blocked 가
없어 AttributeError 가 났다. 그런데 진짜 문제는 그 다음이었다 — 예외가 _poll 밖으로
빠져나가면서 **다음 폴링이 예약되지 않았고**, 매매는 계속 도는데 화면만 멈췄다.
사용자는 낡은 값을 보며 판단하게 된다.

그래서 두 갈래로 시험한다.
1. 코어가 보낼 수 있는 **모든 이벤트**를 실제로 처리해 본다 (누락된 메서드 찾기)
2. 처리 도중 예외가 나도 **폴링이 계속 예약되는지** 확인한다 (고장 격리)
"""

from __future__ import annotations

import dataclasses
import queue

import pytest

from trader.state_machine import Params, Position, State
from trader.ui import bus


@pytest.fixture
def app():
    """화면이 없으면 skip. 방식은 test_ui_smoke 의 픽스처와 같게 맞춘다."""
    tk = pytest.importorskip("tkinter")
    from trader.ui.app import App

    try:
        window = App(bus.Bus())
    except tk.TclError:
        pytest.skip("표시 장치가 없는 환경")
    window.geometry("+3000+3000")
    window.update()
    yield window
    window.destroy()


def _params() -> Params:
    return Params(10_000, 9_500, 9_000, 100_000, 100_000)


def _sample(annotation, name: str):
    """필드 형에 맞는 그럴듯한 값. 이벤트가 늘어나도 시험이 따라온다."""
    text = str(annotation)
    if "tuple" in text:  # float 보다 먼저 본다 (tuple[float, ...] 이 있다)
        inner = text[text.index("[") + 1 : text.rindex("]")] if "[" in text else ""
        parts = [p for p in inner.split(",") if p.strip() and "..." not in p]
        return tuple(0.05 for _ in parts)  # 고정 길이면 그만큼, 아니면 빈 튜플
    if "bool" in text:
        return True
    if "int" in text and "float" not in text:
        return 1
    if "float" in text:
        return 10_000.0
    if "Position" in text:
        return Position(State.BUY1, 9_900, 10, 10)
    if "Params" in text:
        return _params()
    if "date" in name:
        return "2026-08-13"
    if name == "symbol":
        return "005930"
    if name == "name":
        return "삼성전자"
    return "테스트"


def _every_ui_event() -> list:
    """코어가 UI 로 보내는 **모든** 이벤트를 dataclass 정의에서 직접 만들어 낸다.

    손으로 목록을 적으면 새 이벤트가 생겼을 때 시험이 따라가지 못한다. 이번 사고도
    '보내는 쪽은 있는데 받는 쪽이 없는' 종류였으므로, 목록은 코드에서 뽑아야 한다.
    """
    import re
    from pathlib import Path

    core_src = (Path(__file__).resolve().parents[1] / "trader" / "core.py").read_text(
        encoding="utf-8"
    )
    names = sorted(set(re.findall(r"events\.put\(\s*bus\.(\w+)", core_src)))
    events = []
    for cls_name in names:
        cls = getattr(bus, cls_name)
        kwargs = {f.name: _sample(f.type, f.name) for f in dataclasses.fields(cls)}
        events.append(cls(**kwargs))
        # 불리언 필드는 반대쪽도 본다 (연결/끊김, 보류/해제처럼 갈래가 다르다)
        flips = {
            f.name: False for f in dataclasses.fields(cls) if "bool" in str(f.type)
        }
        if flips:
            events.append(cls(**{**kwargs, **flips}))
    return events


def test_모든_UI_이벤트를_처리할_수_있다(app, monkeypatch):
    """누락된 메서드·필드가 있으면 여기서 걸린다 (set_blocked 사고의 재발 방지).

    모달 상자는 막아 둔다 — 뜨면 사람이 누를 때까지 시험이 멈춘다. 이건 실제 동작에서도
    같아서, 폴링 중에 상자를 띄우면 그동안 화면 갱신이 멈춘다(의도된 동작이라 그대로 둔다).
    """
    from tkinter import messagebox

    for fn in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(messagebox, fn, lambda *a, **k: "ok")
    failures = []
    for event in _every_ui_event():
        try:
            app._dispatch(event)
            app.update()
        except Exception as err:  # noqa: BLE001 - 무엇이 터지든 모아서 보고한다
            failures.append(f"{type(event).__name__}: {type(err).__name__} {err}")
    assert not failures, "처리하지 못한 이벤트:\n" + "\n".join(failures)


def test_코어가_보내는_이벤트가_모두_화면에_연결돼_있다():
    """코어가 events 큐에 넣는 클래스는 _dispatch 에 case 가 있어야 한다.

    없으면 조용히 무시된다 — 예외는 안 나지만 화면이 실제와 어긋난다.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    core_src = (root / "trader" / "core.py").read_text(encoding="utf-8")
    app_src = (root / "trader" / "ui" / "app.py").read_text(encoding="utf-8")
    sent = set(re.findall(r"events\.put\(\s*bus\.(\w+)", core_src))
    handled = set(re.findall(r"case bus\.(\w+)", app_src))
    assert sent <= handled, f"화면이 처리하지 않는 이벤트: {sorted(sent - handled)}"


def test_이벤트_하나가_실패해도_폴링은_계속된다(app):
    """가장 위험한 고장 — 화면만 멈추고 매매는 계속되는 상태를 막는다."""
    scheduled: list = []
    app.after = lambda ms, fn=None, *a: scheduled.append((ms, fn))

    class _Boom:
        """어떤 case 에도 걸리지 않아 처리 중 예외를 일으키는 이벤트."""

    app._dispatch = _raise
    app._bus.events.put(_Boom())
    app._poll()

    assert scheduled, "예외 뒤에 다음 폴링이 예약되지 않았다"


def test_실패한_뒤에도_다음_이벤트를_처리한다(app):
    """한 건이 실패했다고 뒤에 쌓인 체결·로그까지 버리면 안 된다."""
    app.after = lambda *a, **k: None
    handled: list = []
    original = app._dispatch

    def flaky(event):
        if isinstance(event, bus.Tick):
            raise RuntimeError("일부러 실패")
        handled.append(event)
        return original(event)

    app._dispatch = flaky
    app._bus.events.put(bus.Tick("005930", 1.0))  # 실패하는 건
    app._bus.events.put(bus.LogLine("t", "005930", "체결", "그 뒤 이벤트"))
    app._poll()

    assert len(handled) == 1
    assert isinstance(handled[0], bus.LogLine)


def test_큐가_비면_조용히_끝난다(app):
    scheduled: list = []
    app.after = lambda ms, fn=None, *a: scheduled.append((ms, fn))
    app._poll()
    assert scheduled  # 다음 폴링 예약


def _raise(_event):
    raise AttributeError("'PositionsView' object has no attribute 'set_blocked'")


# ── 보류 표시 ─────────────────────────────────────────────────


def test_보류_표시가_켜지고_꺼진다(app):
    """코어는 틱마다 보류를 판정하므로 로그가 아니라 화면 상태로만 보여준다."""
    app._dispatch(bus.PositionUpdate("005930", "삼성전자", Position(), _params()))
    app.update()
    app.positions.set_blocked("005930", True, "3선 이하 갭 시가")
    assert "(보류)" in app.positions.tree.set("005930", "state")

    app.positions.set_blocked("005930", False)
    assert "(보류)" not in app.positions.tree.set("005930", "state")


def test_보류를_두_번_켜도_한_번만_붙는다(app):
    app._dispatch(bus.PositionUpdate("005930", "삼성전자", Position(), _params()))
    app.update()
    app.positions.set_blocked("005930", True, "사유")
    app.positions.set_blocked("005930", True, "사유")
    assert app.positions.tree.set("005930", "state").count("(보류)") == 1


def test_행이_없어도_보류_설정이_터지지_않는다(app):
    """등록 전에 보류가 먼저 올 수 있다 — 다음 upsert 가 반영한다."""
    app.positions.set_blocked("999999", True, "사유")
    app._dispatch(bus.PositionUpdate("999999", "없던종목", Position(), _params()))
    app.update()
    assert "(보류)" in app.positions.tree.set("999999", "state")


def test_체결대기_중에는_보류를_덧붙이지_않는다(app):
    """'(체결대기) (보류)' 처럼 겹쳐 적으면 무슨 상태인지 알 수 없다."""
    app._dispatch(
        bus.PositionUpdate(
            "005930", "삼성전자", Position(State.WAITING, pending=True), _params()
        )
    )
    app.update()
    app.positions.set_blocked("005930", True, "사유")
    state = app.positions.tree.set("005930", "state")
    assert "(체결대기)" in state and "(보류)" not in state


def test_종목을_지우면_보류_기록도_지워진다(app):
    app._dispatch(bus.PositionUpdate("005930", "삼성전자", Position(), _params()))
    app.update()
    app.positions.set_blocked("005930", True, "사유")
    app.positions.remove("005930")
    assert "005930" not in app.positions._blocked


def test_bus_이벤트_클래스는_모두_불변이다():
    """UI 스레드와 코어 스레드가 같은 객체를 보므로 변경 가능하면 위험하다."""
    mutable = [
        name
        for name in dir(bus)
        if dataclasses.is_dataclass(getattr(bus, name, None))
        and name != "Bus"  # Bus 는 이벤트가 아니라 큐를 담는 그릇이다
        and not getattr(getattr(bus, name), "__dataclass_params__").frozen
    ]
    assert mutable == [], f"frozen 이 아닌 이벤트: {mutable}"


def test_큐는_스레드_안전한_구현이다():
    """코어(별도 스레드)와 UI 가 같이 쓴다 — list 로 바뀌면 값이 유실된다."""
    b = bus.Bus()
    assert isinstance(b.events, queue.Queue)
    assert isinstance(b.commands, queue.Queue)
