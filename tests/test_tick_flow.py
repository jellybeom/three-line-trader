"""시세가 끊겼는지 감시한다.

2026-08-18 실제 사고: 09:33 프로토콜 오류로 WebSocket 이 끊겼고 09:34 재연결에
**성공**했는데, 그 뒤 15:30 까지 틱이 한 건도 오지 않았다. 프로그램은 6시간 내내
'연결됨' 이었고 아무도 이상을 알아채지 못했다. 그날 1선을 이탈한 11종목이 통째로
진입하지 못했다.

연결이 살아 있는데 데이터만 안 오는 상태는 라이브러리 keepalive 로 잡히지 않는다
(서버가 protocol ping 에는 답한다). 그래서 **틱이 오는지를 직접 본다.**
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from trader.watcher import Tick


class _FakeWatcher:
    def __init__(self):
        self.reconnects = 0

    async def force_reconnect(self) -> bool:
        self.reconnects += 1
        return True


def _core(running: bool = True):
    from trader.core import Core

    core = Core.__new__(Core)
    core._running = running
    core._watcher = _FakeWatcher()
    core._last_ws_tick = 0.0
    core.logs = []
    core._log = lambda sym, kind, text, notify=True: core.logs.append((kind, text))
    core._on_tick = _noop
    return core


async def _noop(_tick):
    return None


def _run(core, when: dt.datetime):
    """_check_tick_flow 를 특정 시각으로 돌린다."""
    import trader.core as core_mod

    real = core_mod.datetime

    class _Fixed(real):
        @classmethod
        def now(cls, tz=None):
            return when

    core_mod.datetime = _Fixed
    try:
        asyncio.run(core._check_tick_flow())
    finally:
        core_mod.datetime = real


def test_틱이_끊기면_다시_연결한다():
    import time

    from trader.core import _TICK_STALL_SEC

    core = _core()
    core._last_ws_tick = time.monotonic() - (_TICK_STALL_SEC + 10)
    _run(core, dt.datetime(2026, 8, 18, 10, 0))

    assert core._watcher.reconnects == 1
    assert any(kind == "경고" for kind, _ in core.logs)


def test_경고는_알림_수준을_넘어_전달된다():
    """'매매만' 으로 써도 폰으로 와야 한다 — 6시간을 모르고 지나갈 수는 없다."""
    import time

    from trader.core import _TICK_STALL_SEC
    from trader.notifier import should_notify

    core = _core()
    core._last_ws_tick = time.monotonic() - (_TICK_STALL_SEC + 10)
    _run(core, dt.datetime(2026, 8, 18, 10, 0))
    kind = next(k for k, _ in core.logs)
    assert should_notify("매매만 (시스템 제외)", "시스템", kind)


def test_틱이_오고_있으면_건드리지_않는다():
    import time

    core = _core()
    core._last_ws_tick = time.monotonic() - 5
    _run(core, dt.datetime(2026, 8, 18, 10, 0))
    assert core._watcher.reconnects == 0
    assert core.logs == []


def test_감시_중이_아니면_보지_않는다():
    import time

    from trader.core import _TICK_STALL_SEC

    core = _core(running=False)
    core._last_ws_tick = time.monotonic() - (_TICK_STALL_SEC + 100)
    _run(core, dt.datetime(2026, 8, 18, 10, 0))
    assert core._watcher.reconnects == 0


@pytest.mark.parametrize(
    "when",
    [
        dt.datetime(2026, 8, 18, 8, 40),  # 개장 전
        dt.datetime(2026, 8, 18, 16, 0),  # 마감 후
        dt.datetime(2026, 8, 15, 10, 0),  # 토요일
    ],
)
def test_장중이_아니면_보지_않는다(when):
    """틱이 없는 게 정상인 시간에 재연결을 반복하면 로그만 시끄러워진다."""
    import time

    from trader.core import _TICK_STALL_SEC

    core = _core()
    core._last_ws_tick = time.monotonic() - (_TICK_STALL_SEC + 100)
    _run(core, when)
    assert core._watcher.reconnects == 0


def test_첫_틱_전에는_기다린다():
    """감시를 막 시작한 순간을 정체로 오해하면 안 된다."""
    core = _core()
    core._last_ws_tick = 0.0
    _run(core, dt.datetime(2026, 8, 18, 9, 0))
    assert core._watcher.reconnects == 0
    assert core._last_ws_tick > 0  # 이제부터 잰다


def test_재연결_뒤에도_계속_조용하면_다시_시도한다():
    """한 번 끊었다고 끝이 아니다 — 살아날 때까지 본다."""
    import time

    from trader.core import _TICK_STALL_SEC

    core = _core()
    for _ in range(3):
        core._last_ws_tick = time.monotonic() - (_TICK_STALL_SEC + 10)
        _run(core, dt.datetime(2026, 8, 18, 10, 0))
    assert core._watcher.reconnects == 3


def test_REST_가격_보정은_살아있음으로_치지_않는다():
    """보정은 재연결 직후 한 번뿐이다 — 그것까지 세면 정체를 영영 못 잡는다."""
    import inspect

    from trader.core import Core

    ws_path = inspect.getsource(Core._on_ws_tick)
    assert "_last_ws_tick" in ws_path  # WebSocket 경로에서만 시각을 남긴다
    assert "_last_ws_tick" not in inspect.getsource(Core._on_tick)


def test_감시자가_없으면_조용히_넘어간다():
    """연결 전에도 루프는 돈다."""
    core = _core()
    core._watcher = None
    core._last_ws_tick = 1.0
    _run(core, dt.datetime(2026, 8, 18, 10, 0))  # 예외가 나지 않는다


def test_강제_재연결은_소켓이_없어도_터지지_않는다():
    from trader.watcher import Watcher

    watcher = Watcher.__new__(Watcher)
    watcher._ws = None
    assert asyncio.run(watcher.force_reconnect()) is False


def test_강제_재연결은_이미_죽은_소켓도_견딘다():
    from trader.watcher import Watcher

    class _Dead:
        async def close(self):
            raise ConnectionResetError("이미 끊김")

    watcher = Watcher.__new__(Watcher)
    watcher._ws = _Dead()
    assert asyncio.run(watcher.force_reconnect()) is True


def test_틱을_받으면_시각이_갱신된다():
    from trader.core import Core

    core = _core()
    core._last_ws_tick = 0.0
    asyncio.run(Core._on_ws_tick(core, Tick("005930", 70_000, "090001")))
    assert core._last_ws_tick > 0
