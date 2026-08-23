"""매매일지 마크다운 생성기 (3단계 · 1단계).

문서는 **순수 생성물**이라는 전제를 지키는지, 그리고 담지 않기로 한 것이 새어 나가지
않는지를 본다. 렌더 함수는 순수 함수라 DB 없이 시험할 수 있다.
"""

from __future__ import annotations

import pytest

from trader.journal_export import (
    closed_cycles,
    export_day,
    metrics,
    net_pnl,
    render_day_index,
    render_trade,
    result_label,
    safe_name,
    slippage_rows,
    trade_slug,
)
from trader.state_machine import Decision, Params, Position, Side, State
from trader.store import Store
from trader.trading_calendar import TradingCalendar

P = Params(
    line1=5_200, line2=5_000, line3=4_900, buy1_amount=200_000, buy2_amount=200_000
)


def _entry(**over) -> dict:
    base = {
        "trade_date": "2026-08-21",
        "symbol": "263800",
        "name": "데이타솔루션",
        "state": "종료",
        "avg_price": 5_211.0,
        "total_bought": 76,
        "realized_pnl": -21_880.0,
        "fees": 658.0,
        "high_price": 5_270.0,
        "low_price": 4_990.0,
        "day_open": 5_180.0,
        "day_close": 5_010.0,
        "line1": 5_200.0,
        "line2": 5_000.0,
        "line3": 4_900.0,
        "tags": "테마주,거래량급증",
        "base_date": "2026-08-14",
        "memo": "",
        "good": "",
        "bad": "",
        "daily_path": "",
    }
    return {**base, **over}


def _cycle() -> list[dict]:
    return [
        {
            "ts": "2026-08-21 09:00:49",
            "trade_date": "2026-08-21",
            "side": "매수",
            "qty": 37,
            "price": 5_200,
            "trigger_price": 5_200,
            "to_state": "1차 매수",
            "reason": "1선 이탈 → 1차 매수",
        },
        {
            "ts": "2026-08-21 09:05:12",
            "trade_date": "2026-08-21",
            "side": "매수",
            "qty": 39,
            "price": 5_220,
            "trigger_price": 5_200,
            "to_state": "2차 매수",
            "reason": "2선 이탈 → 2차 매수",
        },
        {
            "ts": "2026-08-21 10:38:38",
            "trade_date": "2026-08-21",
            "side": "매도",
            "qty": 76,
            "price": 5_010,
            "trigger_price": 4_990,
            "to_state": "종료",
            "reason": "3선 이탈 → 전량 손절",
        },
    ]


# ── 담지 않기로 한 것 ───────────────────────────────────────────


def test_문서에_예수금과_계좌_평가액이_없다():
    """private repo 라도 남의 서버에 올라간다 — 계좌 규모는 복기에 필요하지 않다.

    종목·손익액·손익률은 남긴다. 그것까지 빼면 복기 자체가 되지 않는다.
    """
    doc = render_trade(_entry(), _cycle())
    index = render_day_index("2026-08-21", [_entry()])
    for text in (doc, index):
        for banned in ("예수금", "주문가능", "평가액", "추정자산", "계좌"):
            assert banned not in text
    assert "-22,538원" in doc  # 손익은 남아 있다
    assert "263800" in doc


# ── 렌더 (순수 함수) ────────────────────────────────────────────


def test_세후_손익과_수익률을_함께_적는다():
    doc = render_trade(_entry(), _cycle())
    assert "-22,538원 (-5.69%)" in doc


def test_3선을_따로_떼어_적는다():
    """사람이 판단한 유일한 지점이라 눈에 띄어야 한다."""
    doc = render_trade(_entry(), _cycle())
    assert "## 3선" in doc
    assert "| 1선 | 5,200원 |" in doc
    assert "| 3선 | 4,900원 |" in doc


def test_기준봉과_태그가_실린다():
    """종목 선정 근거다 — 이게 빠지면 '왜 골랐나' 를 복기할 수 없다."""
    doc = render_trade(_entry(), _cycle(), TradingCalendar())
    assert "기준봉 2026-08-14" in doc
    assert "`#테마주`" in doc and "`#거래량급증`" in doc


def test_판정가_대비_체결가_오차를_표로_남긴다():
    """시장가 슬리피지는 3선 설정만큼 성적에 영향을 준다 — 여태 로그에만 있었다."""
    doc = render_trade(_entry(), _cycle())
    assert "## 체결" in doc
    assert "| 09:05 | 매수 | 39주 | 5,200 | 5,220 | +0.38% |" in doc
    assert "| 10:38 | 매도 | 76주 | 4,990 | 5,010 | +0.40% |" in doc


def test_판정가가_없는_체결은_오차표에서_뺀다():
    """수동 청산 등 판정 없이 나간 주문은 비교할 대상이 없다."""
    rows = slippage_rows(
        [{"side": "매도", "price": 5_010, "trigger_price": 0}, {"side": "매수"}]
    )
    assert rows == []


def test_안_쓴_칸은_미작성으로_남긴다():
    """빈 제목만 있으면 안 쓴 건지 쓸 게 없었던 건지 구분되지 않는다."""
    doc = render_trade(_entry(), _cycle())
    assert doc.count("_(미작성)_") == 2

    doc = render_trade(
        _entry(good="손절 규칙 지킴", bad="2선이 1선과 가까웠다"), _cycle()
    )
    assert "손절 규칙 지킴" in doc and "2선이 1선과 가까웠다" in doc
    assert "_(미작성)_" not in doc


def test_값이_없는_항목은_빈_줄로_남기지_않는다():
    """이월 종목은 당일 등락이 없고, 기준봉을 안 적은 종목도 흔하다."""
    doc = render_trade(_entry(day_open=0, day_close=0, base_date="", tags=""), _cycle())
    assert "당일 등락" not in doc
    assert "기준봉" not in doc
    # 표에 값이 빈 줄이나 '-' 로 채운 줄이 없어야 한다
    rows = [
        line
        for line in doc.splitlines()
        if line.startswith("| ")
        and "---" not in line
        and line.count("|") == 3
        and line != "| | |"
    ]
    assert rows
    for line in rows:
        value = line.split("|")[2].strip()
        assert value and value != "-", line


def test_보유_중이면_결과가_손익_부호로_갈리지_않는다():
    """아직 안 끝난 매매를 '익절' 이라고 부르면 집계가 어긋난다."""
    assert result_label(_entry(state="2차 매수")) == "보유 중"
    assert result_label(_entry(realized_pnl=1_000, fees=100)) == "익절"
    assert result_label(_entry(realized_pnl=100, fees=100)) == "본전"


def test_차트가_없으면_깨진_이미지_링크를_넣지_않는다():
    assert "![" not in render_trade(_entry(), _cycle(), charts={})
    assert "![" not in render_trade(_entry(), _cycle(), charts={"일봉": ""})


def test_일봉과_3분봉을_둘_다_싣는다():
    """일봉은 '3선을 어디에 그었나', 3분봉은 '그래서 어떻게 체결됐나' 를 보여준다.

    복기의 질문이 서로 달라 한쪽만으로는 답이 나오지 않는다.
    """
    doc = render_trade(
        _entry(),
        _cycle(),
        charts={
            "일봉": "263800-데이타솔루션-daily.png",
            "3분봉": "263800-데이타솔루션-minute.png",
        },
    )
    assert "![데이타솔루션 일봉](263800-데이타솔루션-daily.png)" in doc
    assert "![데이타솔루션 3분봉](263800-데이타솔루션-minute.png)" in doc
    assert doc.index("daily.png") < doc.index("minute.png")  # 일봉 먼저


def test_한쪽_차트만_있으면_있는_것만_싣는다():
    doc = render_trade(_entry(), _cycle(), charts={"3분봉": "x-minute.png"})
    assert "3분봉" in doc and "일봉" not in doc


# ── 인덱스 ──────────────────────────────────────────────────────


def test_인덱스는_손익_큰_순으로_세우고_작성_여부를_표시한다():
    entries = [
        _entry(symbol="A", name="가", realized_pnl=-21_880, fees=658),
        _entry(symbol="B", name="나", realized_pnl=5_000, fees=200, good="잘함"),
    ]
    index = render_day_index("2026-08-21", entries)
    assert index.index("나(B)") < index.index("가(A)")
    assert "익절 1 · 손절 1 · 승률 50.0%" in index
    assert index.count("✔") == 1


def test_청산된_매매가_없는_날도_문서가_말이_된다():
    assert "청산된 매매 없음" in render_day_index("2026-08-15", [])


def test_인덱스_링크가_매매_문서_경로와_맞는다():
    """링크가 깨지면 폰에서 인덱스만 보이고 아무 데도 못 들어간다."""
    entry = _entry()
    index = render_day_index("2026-08-21", [entry])
    assert f"(2026-08-21/{trade_slug(entry)}.md)" in index


# ── 파일명 ──────────────────────────────────────────────────────


def test_파일명에_못_쓰는_글자를_바꾼다():
    """한 종목 때문에 그날 생성이 통째로 실패하면 곤란하다."""
    assert safe_name("A/B*C?") == "A_B_C_"
    assert safe_name("삼기 에너지") == "삼기_에너지"
    assert safe_name("") == "unknown"


# ── 파일로 쓰기 ─────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def _record(store, date="2026-08-21"):
    store.register_symbol(
        date, "263800", "데이타솔루션", P, tags="테마주", base_date="2026-08-14"
    )
    steps = [
        (
            State.WAITING,
            State.BUY1,
            Side.BUY,
            37,
            5_200,
            5_200,
            Position(State.BUY1, 5_200, 37, 37),
        ),
        (
            State.BUY1,
            State.BUY2,
            Side.BUY,
            39,
            5_220,
            5_200,
            Position(State.BUY2, 5_211, 76, 76),
        ),
        (
            State.BUY2,
            State.CLOSED,
            Side.SELL,
            76,
            5_010,
            4_990,
            Position(
                State.CLOSED,
                5_211,
                76,
                0,
                realized_pnl=-21_880,
                fees=658,
                high_price=5_270,
                low_price=4_990,
            ),
        ),
    ]
    for frm, to, side, qty, price, trigger, pos in steps:
        store.save_transition(
            date, "263800", frm, pos, Decision(to, side, qty, "테스트"), price, trigger
        )


def test_그날의_매매를_파일로_쓴다(store, tmp_path):
    _record(store)
    root = tmp_path / "journal"

    written = export_day(store, "2026-08-21", root, TradingCalendar())

    doc = root / "2026-08" / "2026-08-21" / "263800-데이타솔루션.md"
    index = root / "2026-08" / "2026-08-21.md"
    assert doc.exists() and index.exists()
    assert set(written) == {doc, index}
    assert "데이타솔루션(263800)" in doc.read_text(encoding="utf-8")


def test_다시_돌리면_덮어쓴다(store, tmp_path):
    """문서는 순수 생성물이다 — 코멘트를 채운 뒤 다시 돌려 반영할 수 있어야 한다."""
    _record(store)
    root = tmp_path / "journal"
    export_day(store, "2026-08-21", root)
    doc = root / "2026-08" / "2026-08-21" / "263800-데이타솔루션.md"
    assert "_(미작성)_" in doc.read_text(encoding="utf-8")

    store.save_journal("2026-08-21", "263800", good="손절 규칙 지킴")
    export_day(store, "2026-08-21", root)

    text = doc.read_text(encoding="utf-8")
    assert "손절 규칙 지킴" in text
    assert text.count("_(미작성)_") == 1  # 아쉬운 점만 남는다


def test_매매가_없는_날은_폴더를_만들지_않는다(store, tmp_path):
    """장이 열리지 않은 날까지 빈 폴더가 쌓이면 git 이 지저분해진다."""
    root = tmp_path / "journal"
    assert export_day(store, "2026-08-15", root) == []
    assert not (root / "2026-08" / "2026-08-15").exists()


def test_일봉과_3분봉을_둘_다_문서_옆으로_복사한다(store, tmp_path):
    """상대 경로로 두면 git 에 올렸을 때 폰에서 그림이 깨진다.

    save_journal 은 두 경로를 다 저장하는데 생성기가 일봉만 복사하고 있었다
    (2026-08-22 발견). 3분봉이 없으면 체결 시점의 흐름을 복기할 수 없다.
    """
    _record(store)
    charts = tmp_path / "data" / "charts"
    charts.mkdir(parents=True)
    (charts / "d.png").write_bytes(b"\x89PNGdaily")
    (charts / "m.png").write_bytes(b"\x89PNGminute")
    store.save_journal(
        "2026-08-21",
        "263800",
        daily_path=str(charts / "d.png"),
        minute_path=str(charts / "m.png"),
    )
    root = tmp_path / "journal"

    export_day(store, "2026-08-21", root)

    day = root / "2026-08" / "2026-08-21"
    assert (day / "263800-데이타솔루션-daily.png").read_bytes() == b"\x89PNGdaily"
    assert (day / "263800-데이타솔루션-minute.png").read_bytes() == b"\x89PNGminute"
    doc = (day / "263800-데이타솔루션.md").read_text(encoding="utf-8")
    assert "(263800-데이타솔루션-daily.png)" in doc  # 같은 폴더의 파일명만 건다
    assert "(263800-데이타솔루션-minute.png)" in doc


def test_없어진_차트_경로는_조용히_넘어간다(store, tmp_path):
    """예전 매매의 차트를 지웠다고 생성이 멈추면 안 된다 — 있는 것만 싣는다."""
    _record(store)
    kept = tmp_path / "m.png"
    kept.write_bytes(b"\x89PNGminute")
    store.save_journal(
        "2026-08-21",
        "263800",
        daily_path=str(tmp_path / "없음.png"),
        minute_path=str(kept),
    )

    export_day(store, "2026-08-21", tmp_path / "journal")

    day = tmp_path / "journal" / "2026-08" / "2026-08-21"
    doc = (day / "263800-데이타솔루션.md").read_text(encoding="utf-8")
    assert "일봉" not in doc
    assert "(263800-데이타솔루션-minute.png)" in doc
    assert (day / "263800-데이타솔루션-minute.png").exists()


def test_세후_손익_정의가_화면과_같다():
    """익절/손절 판정이 두 곳에서 갈리면 집계가 어긋난다."""
    from trader.ui.journal_dialog import net_pnl as ui_net

    entry = _entry()
    assert net_pnl(entry) == ui_net(entry) == -22_538


def test_지표에_MFE와_MAE가_들어간다():
    """'얼마까지 갔는데 얼마 먹었나' 가 익절 비중 조정의 근거다."""
    rows = dict(metrics(_entry(), _cycle()))
    assert rows["최고 / 최저"] == "+1.1% / -4.2%"


# ── CLI ─────────────────────────────────────────────────────────


def test_CLI가_프로그램과_같은_규칙으로_DB를_고른다(tmp_path, monkeypatch, capsys):
    """모드별로 DB 파일이 나뉘어 있다 — 고정 이름을 쓰면 아무것도 못 찾는다.

    `data/trader.db` 라는 파일은 존재하지 않는다. mode.txt 를 읽어 trader-real.db 나
    trader-mock.db 를 열어야 한다.
    """
    import export_journal
    from trader.core import write_mode

    write_mode(True, tmp_path)
    store = Store(tmp_path / "trader-real.db")
    _record(store)
    store.close()

    monkeypatch.setattr(
        "sys.argv",
        ["export_journal.py", "--data", str(tmp_path), "--out", str(tmp_path / "j")],
    )
    assert export_journal.main() == 0
    assert "trader-real.db" in capsys.readouterr().out
    assert (tmp_path / "j" / "2026-08" / "2026-08-21.md").exists()


def test_DB가_없으면_경로를_알려주고_멈춘다(tmp_path, monkeypatch, capsys):
    import export_journal

    monkeypatch.setattr(
        "sys.argv", ["export_journal.py", "--mock", "--data", str(tmp_path)]
    )
    assert export_journal.main() == 1
    assert "trader-mock.db" in capsys.readouterr().err


# ── 매매 사이클 하나에 문서 하나 ────────────────────────────────


def _multi_day(store):
    """월 매수 → 화 1차 익절 → 수 전량 매도. 사이클 하나가 사흘에 걸친다."""
    for date in ("2026-08-24", "2026-08-25", "2026-08-26"):
        store.register_symbol(date, "263800", "데이타솔루션", P, tags="테마주")
    # 월: 진입
    store.save_transition(
        "2026-08-24",
        "263800",
        State.WAITING,
        Position(State.BUY1, 5_000, 100, 100),
        Decision(State.BUY1, Side.BUY, 100, "1선 이탈 → 1차 매수"),
        5_000,
        5_000,
    )
    # 화: 1차 익절 — 그날 손익 +4,000
    store.save_transition(
        "2026-08-25",
        "263800",
        State.BUY1,
        Position(State.BUY1_TP1, 5_000, 100, 60, realized_pnl=4_000, fees=100),
        Decision(State.BUY1_TP1, Side.SELL, 40, "1차 익절"),
        5_100,
        5_100,
    )
    # 수: 전량 매도 — 그날 손익 +6,000
    store.save_transition(
        "2026-08-26",
        "263800",
        State.BUY1_TP1,
        Position(
            State.CLOSED,
            5_000,
            100,
            0,
            realized_pnl=6_000,
            fees=150,
            high_price=5_300,
            low_price=4_900,
        ),
        Decision(State.CLOSED, Side.SELL, 60, "본절 이탈 → 잔량 전량 청산"),
        5_100,
        5_100,
    )


def test_진입일과_중간_익절일에는_문서를_만들지_않는다(store, tmp_path):
    """같은 매매의 문서가 셋으로 갈라지면 어느 것이 완성본인지 알 수 없다."""
    _multi_day(store)
    root = tmp_path / "journal"

    assert export_day(store, "2026-08-24", root) == []  # 진입만 한 날
    assert export_day(store, "2026-08-25", root) == []  # 일부만 익절한 날
    assert not (root / "2026-08" / "2026-08-24").exists()

    written = export_day(store, "2026-08-26", root)  # 청산된 날에만
    assert (root / "2026-08" / "2026-08-26" / "263800-데이타솔루션.md") in written


def test_손익은_사이클_전체를_합산한다(store, tmp_path):
    """v12 에서 손익이 날짜별로 쪼개졌다 — 청산일 행만 쓰면 전날 익절이 빠진다."""
    _multi_day(store)

    entries = closed_cycles(store, "2026-08-26")

    assert len(entries) == 1
    assert entries[0]["realized_pnl"] == 10_000  # 4,000 + 6,000
    assert entries[0]["fees"] == 250  # 100 + 150
    assert net_pnl(entries[0]) == 9_750


def test_이월_중인_종목은_문서가_되지_않는다(store, tmp_path):
    """내일로 넘어간 종목에 미리 빈 문서를 만들면 '미작성' 만 쌓인다."""
    store.register_symbol("2026-08-24", "263800", "데이타솔루션", P)
    store.save_transition(
        "2026-08-24",
        "263800",
        State.WAITING,
        Position(State.BUY1, 5_000, 100, 100),
        Decision(State.BUY1, Side.BUY, 100, "1선 이탈 → 1차 매수"),
        5_000,
        5_000,
    )

    assert closed_cycles(store, "2026-08-24") == []
    assert export_day(store, "2026-08-24", tmp_path / "journal") == []


def test_문서에_사이클_전체_기간이_적힌다(store, tmp_path):
    """사흘짜리 매매인데 청산일 하루만 적히면 복기가 되지 않는다."""
    _multi_day(store)
    export_day(store, "2026-08-26", tmp_path / "journal")

    doc = (
        tmp_path / "journal" / "2026-08" / "2026-08-26" / "263800-데이타솔루션.md"
    ).read_text(encoding="utf-8")
    assert "진입 2026-08-24" in doc
    assert "청산 2026-08-26" in doc
    assert "+9,750원" in doc  # 사이클 전체 손익
