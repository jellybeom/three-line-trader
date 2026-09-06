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


# ── 시점 분리 · 줄바꿈 (2026-09-06) ─────────────────────────────


def test_진입_청산_기준봉을_각각의_행으로_둔다():
    """한 줄로 묶으면 좁은 기둥에서 줄이 접혀 어느 값이 무엇인지 흐려진다."""
    from trader.trading_calendar import TradingCalendar

    labels = [label for label, _v in info_rows(_entry(), _cycle(), TradingCalendar())]

    assert "시점" not in labels
    for expected in ("진입", "청산", "기준봉"):
        assert expected in labels


def test_같은_날_끝났으면_시각만_적는다():
    from trader.journal import entry_exit_stamps

    cycle = [
        {"ts": "2026-09-03 09:13:36", "side": "매수", "to_state": "1차 매수"},
        {"ts": "2026-09-03 14:20:41", "side": "매도", "to_state": "종료"},
    ]

    assert entry_exit_stamps(cycle) == ("09:13", "14:20")


def test_날짜를_넘겼으면_날짜까지_적는다():
    from trader.journal import entry_exit_stamps

    assert entry_exit_stamps(_cycle()) == ("09-02 09:13", "09-03 14:20")


def test_아직_보유_중이면_청산은_비어_있다():
    from trader.journal import entry_exit_stamps

    cycle = [{"ts": "2026-09-03 09:13:36", "side": "매수", "to_state": "1차 매수"}]

    assert entry_exit_stamps(cycle) == ("09:13", "")


def test_화살표는_뒤따르는_단계와_함께_줄을_넘긴다(tmp_path):
    """`… → 5%` / `익절 → 7% 익절` 처럼 갈라지면 읽기 나쁘다."""
    from reportlab.pdfgen import canvas

    from trader.journal_pdf import _Sheet, register_fonts

    sheet = _Sheet(canvas.Canvas(str(tmp_path / "t.pdf")), *register_fonts())
    lines = sheet.wrap("1차 매수 → 3% 익절 → 5% 익절 → 7% 익절", 8.2, 30)

    assert len(lines) > 1  # 실제로 접혔다
    for line in lines[1:]:
        assert line.startswith("→")  # 이어지는 줄은 화살표로 시작한다
    assert not any(line.rstrip().endswith(("→", "%")) for line in lines)


# ── 코멘트 잇기 · 항목 순서 (2026-09-06) ────────────────────────


def test_여러_줄로_쓴_코멘트는_가운뎃점으로_잇는다():
    """스레드에 답글을 여러 번 달면 줄이 나뉜다.

    띄어쓰기로만 이으면 문장 경계가 사라져 한 문장처럼 읽힌다(2026-09-06 실측:
    "…지지받은 듯 기준봉이 생기고…"). 슬래시는 이 문서에서 `평단 / 수량` 처럼
    두 값을 나누는 뜻으로 이미 쓰므로 쓰지 않는다.
    """
    from trader.journal_pdf import comment_text

    entry = {"good": "박스에서 지지받은 듯\n기준봉이 생기고 첫 반등지점"}

    assert (
        comment_text(entry, "good")
        == "박스에서 지지받은 듯 · 기준봉이 생기고 첫 반등지점"
    )


def test_한_줄이면_구분자를_넣지_않는다():
    from trader.journal_pdf import comment_text

    assert comment_text({"good": "박스 하단을 잡았다"}, "good") == "박스 하단을 잡았다"


def test_빈_줄은_버린다():
    """답글 사이에 빈 줄이 섞여도 구분자가 겹치지 않는다."""
    from trader.journal_pdf import comment_text

    assert comment_text({"bad": "첫째\n\n\n둘째"}, "bad") == "첫째 · 둘째"


def test_안_쓴_코멘트는_대시로_남긴다():
    from trader.journal_pdf import comment_text

    assert comment_text({}, "good") == "-"
    assert comment_text({"bad": "   "}, "bad") == "-"


def test_매매_정보는_시간_순서로_세운다():
    """왜 골랐고(기준봉) → 언제 들어가 언제 나왔으며 → 얼마나 들고 있었나."""
    from trader.trading_calendar import TradingCalendar

    labels = [label for label, _v in info_rows(_entry(), _cycle(), TradingCalendar())]
    order = [labels.index(x) for x in ("기준봉", "진입", "청산", "보유")]

    assert order == sorted(order)
