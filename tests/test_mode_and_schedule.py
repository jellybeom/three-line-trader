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
