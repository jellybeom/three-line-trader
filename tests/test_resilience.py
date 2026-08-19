"""매매가 멈추는 경로를 막는다.

2026-08-19 전체 점검에서 찾은 것들이다. 공통점은 **조용히 멈춘다**는 것 —
화면은 멀쩡하고 오류창도 안 뜨는데 주문만 나가지 않는다. 그래서 실패를 격리하고,
격리했으면 반드시 눈에 보이게 남긴다.
"""

from __future__ import annotations

import asyncio

import pytest

from trader.broker import BrokerError
from trader.watcher import Tick


def _core():
    from trader.core import Core

    core = Core.__new__(Core)
    core.logs = []
    core._log = lambda sym, kind, text, notify=True: core.logs.append((kind, text))
    core._last_cycle_error = ""
    core._tick_errors = set()
    core._last_ws_tick = 0.0
    return core


# ── 코어 루프 ─────────────────────────────────────────────────


def test_주기_처리가_실패해도_루프는_돈다():
    """예전에는 방어가 없어 예외 하나로 코어 스레드가 통째로 죽었다."""
    core = _core()

    async def boom():
        raise RuntimeError("일부러 실패")

    core._cycle = boom
    ran = []

    async def loop_twice():
        for _ in range(2):
            try:
                await core._cycle()
            except Exception:  # noqa: BLE001 — core.run 과 같은 구조
                core._report_cycle_error()
            ran.append(True)

    asyncio.run(loop_twice())
    assert ran == [True, True]  # 두 바퀴 다 돌았다
    assert any(kind == "에러" for kind, _ in core.logs)


def test_같은_실패는_한_번만_알린다():
    """5초마다 도는 루프라 그대로 두면 로그가 넘친다."""
    core = _core()
    for _ in range(5):
        try:
            raise RuntimeError("같은 실패")
        except RuntimeError:
            core._report_cycle_error()
    assert len(core.logs) == 1


def test_원인이_바뀌면_다시_알린다():
    core = _core()
    for error in (RuntimeError("A"), ValueError("B")):
        try:
            raise error
        except Exception:  # noqa: BLE001
            core._report_cycle_error()
    assert len(core.logs) == 2


def test_루프는_취소_요청을_삼키지_않는다():
    """종료 신호까지 잡아버리면 프로그램이 안 꺼진다."""
    import inspect

    from trader.core import Core

    source = inspect.getsource(Core.run)
    assert "asyncio.CancelledError" in source
    assert source.index("CancelledError") < source.index("except Exception")


# ── 틱 처리 ───────────────────────────────────────────────────


def test_틱_처리_실패가_연결을_끊지_않는다():
    """예외가 수신 루프까지 올라가면 재연결로 위장되고, 매 틱마다 반복되면 무한 재연결이다."""
    from trader.core import Core

    core = _core()

    async def boom(_tick):
        raise ValueError("판정 버그")

    core._on_tick = boom
    asyncio.run(
        Core._on_ws_tick(core, Tick("005930", 70_000, "090001"))
    )  # 예외가 안 난다
    assert any(kind == "에러" for kind, _ in core.logs)


def test_틱_실패는_종목_원인별로_한_번만_알린다():
    """초당 수십 건이 오므로 그대로 두면 로그가 순식간에 찬다."""
    from trader.core import Core

    core = _core()

    async def boom(_tick):
        raise ValueError("판정 버그")

    core._on_tick = boom
    for _ in range(10):
        asyncio.run(Core._on_ws_tick(core, Tick("005930", 70_000, "090001")))
    assert len(core.logs) == 1


def test_틱_실패해도_수신_시각은_갱신된다():
    """정체 감시가 '틱이 안 온다' 로 오해하면 안 된다 — 오고는 있다."""
    from trader.core import Core

    core = _core()

    async def boom(_tick):
        raise ValueError("판정 버그")

    core._on_tick = boom
    asyncio.run(Core._on_ws_tick(core, Tick("005930", 70_000, "090001")))
    assert core._last_ws_tick > 0


# ── 브로커 예외 ───────────────────────────────────────────────


def _broker_raising(error: Exception):
    from trader.broker import Broker

    b = Broker.__new__(Broker)
    b._lock = __import__("threading").Lock()
    b._throttle = lambda: None
    b._post = lambda *a, **k: (_ for _ in ()).throw(error)
    return b


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("timed out"),
        ConnectionError("연결 실패"),
        OSError("네트워크 없음"),
        ValueError("토큰 갱신 실패"),
    ],
)
def test_모든_호출_실패는_BrokerError_로_모인다(error):
    """호출부는 BrokerError 만 잡는다. 그 밖의 예외가 새면 주문 경로에서 위로 튀어
    WebSocket 까지 끊고(재연결로 위장) 실패 표시도 안 남아 중복 주문 위험이 생긴다."""
    broker = _broker_raising(error)
    with pytest.raises(BrokerError):
        broker._request_once("/x", "kt10000", {})


def test_BrokerError_는_그대로_전달된다():
    broker = _broker_raising(BrokerError("이미 변환됨"))
    with pytest.raises(BrokerError, match="이미 변환됨"):
        broker._request_once("/x", "kt10000", {})


# ── 뒤에서 도는 일 ────────────────────────────────────────────


def test_백그라운드_실패를_삼키지_않는다():
    """create_task 로 띄운 코루틴의 예외는 회수될 때에야 경고가 찍힌다 — 창 없이 돌리면 못 본다."""
    from trader.core import Core

    core = _core()

    async def scenario():
        async def boom():
            raise RuntimeError("전송 실패")

        Core._spawn(core, boom(), "알림 발송")
        await asyncio.sleep(0)  # 태스크가 끝날 틈을 준다
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert any("알림 발송" in text for _kind, text in core.logs)


def test_성공한_백그라운드_작업은_조용하다():
    from trader.core import Core

    core = _core()

    async def scenario():
        async def fine():
            return None

        Core._spawn(core, fine(), "알림 발송")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert core.logs == []


def test_오래_도는_태스크가_끝나면_알린다():
    """시세 수신·봇이 죽으면 매매가 멈추는데 create_task 는 조용히 끝난다."""
    from trader.core import Core

    core = _core()

    async def scenario():
        async def boom():
            raise RuntimeError("소켓 종료")

        task = asyncio.ensure_future(boom())
        task.add_done_callback(Core._task_died(core, "시세 수신"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert any("시세 수신" in text for _kind, text in core.logs)


# ── 코어 스레드 사망 ──────────────────────────────────────────


def test_코어가_죽으면_화면에_알린다():
    """데몬 스레드라 프로세스는 살아 있고 창도 멀쩡한데 매매만 멈춘다."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(
        encoding="utf-8"
    )
    assert "bus.LogLine" in source  # 이벤트 큐로 알린다
    assert "코어가 멈췄습니다" in source
