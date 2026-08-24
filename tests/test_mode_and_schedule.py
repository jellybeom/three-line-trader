"""모드별 DB 분리, 자동 스케줄 설정, 일일 요약 — 실전 운용 안전장치 테스트."""

import asyncio
from datetime import time as dtime

import pytest

from trader.core import (
    Core,
    _load_fee_rates,
    _load_schedule,
    db_path_for,
    read_mode,
    write_mode,
)
from trader.notifier import format_daily_summary
from trader.state_machine import Params, Position
from trader.ui import bus

P = Params(
    line1=10_000, line2=9_000, line3=8_000, buy1_amount=1_000_000, buy2_amount=900_000
)


# ── 모드별 DB 분리 ─────────────────────────────────────────────


def test_모드는_DB_밖의_파일에_저장된다(tmp_path):
    """어느 DB 를 열지 정하려면 모드를 먼저 알아야 하므로 DB 안에 둘 수 없다."""
    assert read_mode(tmp_path) is False  # 파일 없으면 안전하게 모의
    write_mode(True, tmp_path)
    assert read_mode(tmp_path) is True
    assert (tmp_path / "mode.txt").exists()


def test_모드마다_다른_DB_파일(tmp_path):
    assert db_path_for(False, tmp_path) != db_path_for(True, tmp_path)
    assert "mock" in db_path_for(False, tmp_path)
    assert "real" in db_path_for(True, tmp_path)


def test_모드_전환시_관심종목이_섞이지_않는다(tmp_path):
    core = Core(bus.Bus(), db_dir=str(tmp_path))
    core._date = "2026-07-27"
    core._open_store()

    async def scenario():
        await core._handle_command(bus.Register("005930", "삼성전자", P, Position()))
        await core._handle_command(bus.SetMode(True))  # 실전 DB 로 교체
        assert core._entries == {}
        await core._handle_command(bus.Register("000660", "하이닉스", P, Position()))
        await core._handle_command(bus.SetMode(False))  # 모의로 복귀
        assert list(core._entries) == ["005930"]

    asyncio.run(scenario())
    core._store.close()


# ── 설정 로더 ──────────────────────────────────────────────────


def test_스케줄_설정_읽기(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[schedule]\nenabled = true\nstart = "09:05"\nstop = "15:20"\n',
        encoding="utf-8",
    )
    s = _load_schedule(str(cfg))
    assert s["enabled"] is True
    assert s["start"] == dtime(9, 5) and s["stop"] == dtime(15, 20)
    assert s["summary"] == dtime(15, 35)  # 미지정 항목은 기본값


def test_설정_파일이_없거나_형식이_틀리면_안전한_기본값(tmp_path):
    assert _load_schedule(str(tmp_path / "없음.toml"))["enabled"] is False
    cfg = tmp_path / "config.toml"
    cfg.write_text('[schedule]\nenabled = true\nstart = "이상한값"\n', encoding="utf-8")
    assert _load_schedule(str(cfg))["start"] == dtime(8, 55)


def test_거래비용_설정_읽기(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[fees]\ncommission_rate = 0.0001\ntax_rate = 0.002\n", encoding="utf-8"
    )
    assert _load_fee_rates(str(cfg)) == (0.0001, 0.002)


# ── 일일 요약 ──────────────────────────────────────────────────


@pytest.fixture
def report():
    symbols = [
        {
            "symbol": "066590",
            "name": "스모트로닉",
            "memo": "",
            "state": "종료",
            "avg_price": 2518.89,
            "total_bought": 198,
            "remaining": 0,
            "realized_pnl": 8054,
            "fees": 874,
            "high_price": 2625,
            "low_price": 2450,
        },
        {
            "symbol": "005710",
            "name": "고려산업",
            "memo": "",
            "state": "1차 매수",
            "avg_price": 1450,
            "total_bought": 34,
            "remaining": 34,
            "realized_pnl": 0,
            "fees": 7,
            "high_price": 1468,
            "low_price": 1401,
        },
        {
            "symbol": "035420",
            "name": "NAVER",
            "memo": "",
            "state": "대기",
            "avg_price": 0,
            "total_bought": 0,
            "remaining": 0,
            "realized_pnl": 0,
            "fees": 0,
            "high_price": 0,
            "low_price": 0,
        },
    ]
    fills = [
        {
            "ts": "2026-07-24 09:02:58",
            "symbol": "066590",
            "side": "매수",
            "qty": 97,
            "price": 2575,
            "reason": "",
        },
        {
            "ts": "2026-07-24 14:54:40",
            "symbol": "066590",
            "side": "매도",
            "qty": 198,
            "price": 2546,
            "reason": "",
        },
    ]
    return symbols, fills


def test_일일_요약에_세전_세후_손익과_이월_안내가_들어간다(report):
    symbols, fills = report
    text = format_daily_summary("2026-07-24", symbols, fills, deposit=1_013_820)
    assert "+8,054원" in text and "+7,173원" in text  # 세전 · 세후(비용 881 차감)
    assert "이월 필요" in text  # 보유 중 종목 안내
    assert (
        "스모트로닉(066590)" in text and "NAVER" not in text
    )  # 미진입 종목은 목록 제외
    assert "예수금 1,013,820원" in text


def test_매매가_없으면_그렇게_표시한다():
    text = format_daily_summary("2026-07-25", [], [])
    assert "체결된 매매가 없습니다" in text


def test_요약에_최고_최저_비율이_들어간다(report):
    symbols, fills = report
    text = format_daily_summary("2026-07-24", symbols, fills)
    assert "최고 +4.2%" in text and "최저 -2.7%" in text


# ── 스케줄 실행 이력 영속 (2026-07-31 실측) ───────────────────


def test_요약은_재시작해도_다시_보내지_않는다(tmp_path, monkeypatch):
    """15:35 에 보낸 뒤 프로그램을 다시 켜면 또 보내던 문제."""
    import datetime as dt

    import trader.core as core_mod
    from trader.store import Store

    # 실제 요일·시각에 좌우되지 않도록 평일 장 마감 후로 고정한다
    fixed = dt.datetime(2026, 7, 31, 15, 40)  # 금요일

    class FixedDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(core_mod, "datetime", FixedDatetime)
    sent = []

    def make_core():
        c = Core(bus.Bus(), db_dir=str(tmp_path))
        c._date = fixed.date().isoformat()
        c._store = Store(str(tmp_path / "t.db"))
        c._schedule = {
            "enabled": True,
            "start": dt.time(8, 55),
            "stop": dt.time(15, 30),
            "summary": dt.time(15, 35),
        }
        c.send_daily_summary = lambda: _record(sent)
        return c

    async def _record(box):
        box.append(1)

    async def scenario():
        c1 = make_core()
        c1._running = True  # 자동 시작 경로를 타지 않게
        await c1._check_schedule()
        c1._store.close()
        assert len(sent) == 1

        c2 = make_core()  # 재시작
        c2._running = True
        await c2._check_schedule()
        c2._store.close()

    asyncio.run(scenario())
    assert len(sent) == 1, "재시작 후 요약이 다시 발송됨"


# ── 시작 시 자동 연결 (2026-08-01) ─────────────────────────────


def test_자동_연결_설정_읽기(tmp_path):
    from trader.core import _load_auto_connect

    cfg = tmp_path / "config.toml"
    cfg.write_text("[startup]\nauto_connect = true\n", encoding="utf-8")
    assert _load_auto_connect(str(cfg)) is True
    assert _load_auto_connect(str(tmp_path / "없음.toml")) is False

    cfg.write_text("[startup]\n", encoding="utf-8")
    assert _load_auto_connect(str(cfg)) is False  # 기본값은 끔


def test_프로그램_시작_직후_바로_연결을_시도한다(tmp_path):
    """PC 를 켠 직후 실행해도 첫 시도를 건너뛰지 않아야 한다.

    time.monotonic() 은 부팅 후 경과 시간이라, 마지막 시도 시각의 초기값을 0.0 으로
    두면 '방금 시도했다' 고 오판해 첫 연결이 통째로 생략된다(2026-08-04 실측 버그).
    """
    from trader.store import Store

    core = Core(bus.Bus(), db_dir=str(tmp_path))
    core._date = "2026-08-04"
    core._store = Store(str(tmp_path / "t.db"))
    core._auto_connect = True

    tries = []

    async def fake_connect(quiet=False):
        tries.append(quiet)

    core._connect = fake_connect
    asyncio.run(core._tick_auto_connect())
    assert tries == [False], "시작 직후 연결을 시도하지 않음"

    asyncio.run(core._tick_auto_connect())  # 재시도 간격 이내에는 중복 시도하지 않는다
    assert len(tries) == 1
    core._store.close()


def test_자동_연결은_실패해도_주기적으로_다시_시도한다(tmp_path, monkeypatch):
    """증권사 서버 점검처럼 일시적 사유로 실패할 수 있다."""
    import trader.core as core_mod
    from trader.store import Store

    core = Core(bus.Bus(), db_dir=str(tmp_path))
    core._date = "2026-08-03"
    core._store = Store(str(tmp_path / "t.db"))
    core._auto_connect = True
    core._notifier = object()  # Discord 는 이미 연결된 상태로 둔다

    attempts = []

    async def fake_connect(quiet=False):
        attempts.append(quiet)

    core._connect = fake_connect
    monkeypatch.setattr(core_mod, "_AUTO_CONNECT_RETRY_SEC", 0)

    async def scenario():
        for _ in range(3):
            await core._tick_auto_connect()

    asyncio.run(scenario())
    assert len(attempts) == 3  # 실패해도 계속 재시도
    assert attempts[0] is False and attempts[-1] is True  # 이후 시도는 조용히
    core._store.close()


def test_연결되면_자동_연결_시도를_멈춘다(tmp_path):
    from trader.store import Store

    core = Core(bus.Bus(), db_dir=str(tmp_path))
    core._date = "2026-08-03"
    core._store = Store(str(tmp_path / "t.db"))
    core._auto_connect = True
    core._broker = object()
    core._notifier = object()

    called = []
    core._connect = lambda quiet=False: called.append(1)
    asyncio.run(core._tick_auto_connect())
    assert called == []
    core._store.close()


def test_거래세_기본값은_실제와_같은_0_20퍼센트다(tmp_path):
    """낮게 잡으면 세후 손익이 실제보다 좋아 보이고 매수 수량도 많이 나온다.

    0.15% 로 두었더니 2026-08-24 실측에서 매도대금 547,120원에 세금 1,091원(0.1994%)이
    나와 하루에 270원이 어긋났다. 코스피는 거래세 0.05% + 농특세 0.15%, 코스닥은
    거래세 0.20% 로 경로만 다르고 결과는 같다.
    """
    from trader.core import _load_fee_rates

    assert _load_fee_rates(str(tmp_path / "없음.toml")) == (0.00015, 0.002)

    cfg = tmp_path / "config.toml"
    cfg.write_text("[fees]\ncommission_rate = 0.0001\n", encoding="utf-8")
    assert _load_fee_rates(str(cfg)) == (0.0001, 0.002)  # 빠진 값만 기본값


def test_설정_예시의_거래세도_같다():
    """예시를 그대로 복사해 쓰는 사람이 손해 보지 않도록."""
    import tomllib
    from pathlib import Path

    text = (
        Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("config.toml.example")
        .read_text(encoding="utf-8")
    )
    assert tomllib.loads(text)["fees"]["tax_rate"] == 0.002
