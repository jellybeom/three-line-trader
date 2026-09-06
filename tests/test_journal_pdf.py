"""매매일지 A4 PDF (3단계-4).

레이아웃은 눈으로 봐야 하는 것이라, 테스트는 **틀리면 조용히 잘못 나오는 것**만 잡는다.
여백 계산, 표에 담기는 값, 그리고 '있어야 할 것이 빠지지 않았는가' 다.
"""

from __future__ import annotations

import pytest

from trader.journal_pdf import PdfError, fill_rows, info_rows, register_fonts
from trader.state_machine import Decision, Params, Position, Side, State
from trader.store import Store

P = Params(
    line1=3_900, line2=3_750, line3=3_600, buy1_amount=82_000, buy2_amount=78_000
)


def _entry(**over) -> dict:
    base = {
        "trade_date": "2026-09-03",
        "symbol": "037440",
        "name": "희림",
        "state": "종료",
        "avg_price": 3_900.0,
        "total_bought": 21,
        "realized_pnl": 3_750.0,
        "fees": 120.0,
        "high_price": 4_181.0,
        "low_price": 3_857.0,
        "tags": "테마주,시장을이기는종목",
        "base_date": "2026-08-26",
        "memo": "반도체 장비",
        "good": "박스 하단을 정확히 잡았다.",
        "bad": "급등 자리 청산이라 슬리피지가 있었다.",
    }
    return {**base, **over}


def _cycle() -> list[dict]:
    return [
        {
            "ts": "2026-09-02 09:13:36",
            "trade_date": "2026-09-02",
            "side": "매수",
            "qty": 21,
            "price": 3_900,
            "trigger_price": 3_892,
            "to_state": "1차 매수",
            "reason": "1선 이탈 → 1차 매수",
        },
        {
            "ts": "2026-09-03 11:42:10",
            "trade_date": "2026-09-03",
            "side": "매도",
            "qty": 8,
            "price": 4_020,
            "trigger_price": 4_041,
            "to_state": "1차 매수 + 3% 익절",
            "reason": "1차 평단 +3% 도달 → 1차 익절",
        },
        {
            "ts": "2026-09-03 14:20:41",
            "trade_date": "2026-09-03",
            "side": "매도",
            "qty": 3,
            "price": 4_180,
            "trigger_price": 4_175,
            "to_state": "종료",
            "reason": "1차 평단 +7% 도달 → 전량 청산",
        },
    ]


# ── 매매 정보 ───────────────────────────────────────────────────


def test_당일_등락은_담지_않는다():
    """여러 날에 걸친 매매에서는 청산일 하루치일 뿐이라 성적으로 오해된다."""
    labels = [label for label, _v in info_rows(_entry(), _cycle())]

    assert "당일 등락" not in labels
    assert labels[0] == "평단 / 수량"


def test_값이_없는_항목은_넣지_않는다():
    """빈 줄이 남으면 무엇이 없는 건지 헷갈린다."""
    labels = [label for label, _v in info_rows(_entry(tags="", memo=""), _cycle())]

    assert "태그" not in labels and "메모" not in labels


def test_평단과_수량은_같은_구분자를_쓴다():
    """라벨이 `평단 / 수량` 인데 값이 `3,900원 · 21주` 면 짝이 안 맞는다."""
    rows = dict(info_rows(_entry(), _cycle()))

    assert rows["평단 / 수량"] == "3,900원 / 21주"


def test_평단이_없어도_손익은_적는다():
    """강제 복구된 포지션은 평단이 0 일 수 있다 — 0 으로 나누면 터진다."""
    rows = dict(info_rows(_entry(avg_price=0, total_bought=0), _cycle()))

    assert rows["실현손익 (세후)"] == "+3,630원"
    assert "최고 / 최저" not in rows


# ── 체결 표 ─────────────────────────────────────────────────────


def test_체결은_네_열이고_오차가_체결가에_붙는다():
    """판정가는 3선으로 차트에 이미 있다 — 숫자로 또 적을 이유가 없다."""
    rows = fill_rows(_cycle())

    assert len(rows) == 3
    assert len(rows[0]) == 4
    assert rows[0] == ("09:13", "매수", "21주", "3,900 (+0.21%)")


def test_판정가가_없으면_오차를_적지_않는다():
    """수동 청산처럼 판정 없이 나간 주문은 비교할 대상이 없다."""
    cycle = [{"ts": "2026-09-03 15:10:00", "side": "매도", "qty": 5, "price": 4_000}]

    assert fill_rows(cycle)[0][3] == "4,000"


def test_주문_없는_전이는_체결로_세지_않는다():
    """'매도 수량 0 → 상태만 전이' 는 주문이 나가지 않았다."""
    cycle = [{"ts": "2026-09-03 10:00:00", "side": None, "to_state": "종료"}]

    assert fill_rows(cycle) == []


# ── 폰트 ────────────────────────────────────────────────────────


def test_없는_폰트를_지정하면_설치된_것으로_물러난다():
    """지정이 틀렸다고 PDF 를 아예 못 만들면 곤란하다."""
    regular, bold = register_fonts("/없는/경로.ttf", "/없는/경로.ttf")

    assert regular and bold


def test_폰트를_하나도_못_찾으면_고치는_법을_알려준다(monkeypatch):
    monkeypatch.setattr("trader.journal_pdf.FONT_CANDIDATES", ())
    with pytest.raises(PdfError, match="폰트"):
        register_fonts()


# ── 파일 생성 ───────────────────────────────────────────────────


@pytest.fixture
def store_with_trade(tmp_path):
    store = Store(tmp_path / "t.db")
    for date in ("2026-09-02", "2026-09-03"):
        store.register_symbol(
            date,
            "037440",
            "희림",
            P,
            memo="반도체 장비",
            tags="테마주",
            base_date="2026-08-26",
        )
    store.save_transition(
        "2026-09-02",
        "037440",
        State.WAITING,
        Position(State.BUY1, 3_900, 21, 21),
        Decision(State.BUY1, Side.BUY, 21, "1차 매수"),
        3_900,
        3_892,
    )
    store.save_transition(
        "2026-09-03",
        "037440",
        State.BUY1,
        Position(
            State.CLOSED,
            3_900,
            21,
            0,
            realized_pnl=3_750,
            fees=120,
            high_price=4_181,
            low_price=3_857,
        ),
        Decision(State.CLOSED, Side.SELL, 21, "전량 청산"),
        4_180,
        4_175,
    )
    yield store
    store.close()


def test_청산된_날에만_PDF를_만든다(store_with_trade, tmp_path):
    """마크다운과 같은 규칙이다 — 매매 사이클 하나에 한 장."""
    from trader.journal_export import export_pdf

    assert export_pdf(store_with_trade, "2026-09-02", tmp_path / "j") == []

    made = export_pdf(store_with_trade, "2026-09-03", tmp_path / "j")
    assert len(made) == 1
    assert made[0].name == "037440-희림.pdf"
    assert made[0].stat().st_size > 1_000


def test_종목을_지정하면_그것만_만든다(store_with_trade, tmp_path):
    from trader.journal_export import export_pdf

    assert (
        export_pdf(store_with_trade, "2026-09-03", tmp_path / "j", symbol="999999")
        == []
    )
    assert (
        len(export_pdf(store_with_trade, "2026-09-03", tmp_path / "j", symbol="037440"))
        == 1
    )


def test_차트가_없어도_PDF는_만들어진다(store_with_trade, tmp_path):
    """예전 매매의 차트를 정리했다고 출력이 막히면 안 된다."""
    from trader.journal_export import export_pdf

    made = export_pdf(store_with_trade, "2026-09-03", tmp_path / "j")

    assert made and made[0].exists()


def test_PDF는_마크다운_옆에_둔다(store_with_trade, tmp_path):
    from trader.journal_export import export_day, export_pdf

    export_day(store_with_trade, "2026-09-03", tmp_path / "j")
    made = export_pdf(store_with_trade, "2026-09-03", tmp_path / "j")

    assert made[0].parent == (tmp_path / "j" / "2026-09" / "2026-09-03")
    assert (made[0].parent / "037440-희림.md").exists()


def test_PDF는_커밋하지_않는다():
    """마크다운이 원본이고 PDF 는 출력물이다 — 매번 커밋하면 저장소가 커진다."""
    from pathlib import Path

    ignore = Path(__file__).resolve().parents[1] / ".gitignore"
    assert "*.pdf" in ignore.read_text(encoding="utf-8")
