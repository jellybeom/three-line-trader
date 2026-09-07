"""매매 설정 다이얼로그.

사용자에게는 창 하나지만 **저장 경로가 셋**으로 갈린다 — 자금·익절은 DB, 거래비용은
`config.toml`, 알림 수준은 DB. 어느 하나가 빠지면 '저장했는데 안 바뀐다' 가 되므로
명령이 실제로 나가는지를 본다.
"""

from __future__ import annotations

import pytest

from trader.ui import bus
from trader.ui.settings_dialog import parse_fees, parse_funds

VALUES = {
    "total": "1930000",
    "max": "7",
    "buy1": "137857",
    "buy2": "137857",
    "rate1": "3",
    "rate2": "5",
    "rate3": "7",
    "ratio1": "40",
    "ratio2": "50",
    "ratio3": "10",
    "commission": "0.015",
    "tax": "0.2",
    "notify_level": "전체",
}


# ── 입력 검증 (순수 함수) ───────────────────────────────────────


def test_익절_비중_합이_100이_아니면_거부한다():
    """3차까지 다 팔아야 포지션이 정리된다 — 합이 안 맞으면 잔량이 남는다."""
    with pytest.raises(ValueError, match="100"):
        parse_funds({**VALUES, "ratio3": "20"})


def test_종목당_배분을_넘는_매수_금액을_거부한다():
    """1차+2차가 배분액을 넘으면 최대 종목 수를 채우기 전에 자금이 마른다."""
    with pytest.raises(ValueError, match="배분"):
        parse_funds({**VALUES, "buy1": "900000"})


def test_화면은_퍼센트_저장은_소수다():
    """`3` 을 입력하면 0.03 으로 저장된다 — 두 표기가 섞이면 백 배가 어긋난다."""
    _t, _m, _b1, _b2, rates, ratios = parse_funds(VALUES)

    assert rates == (0.03, 0.05, 0.07)
    assert ratios == (0.4, 0.5, 0.1)
    assert parse_fees(VALUES) == (0.00015, 0.002)


def test_말도_안_되는_거래비용을_거부한다():
    """수수료 15% 같은 값이 들어가면 매수 수량이 터무니없이 줄어든다."""
    with pytest.raises(ValueError, match="1%"):
        parse_fees({**VALUES, "commission": "15"})


def test_숫자가_아니면_무엇이_문제인지_알려준다():
    with pytest.raises(ValueError, match="숫자"):
        parse_funds({**VALUES, "total": "백만원"})


# ── 창 동작 ─────────────────────────────────────────────────────


@pytest.fixture
def dialog(app):
    """지금 값이 채워진 설정 창."""
    from trader.ui.settings_dialog import TradeSettingsDialog

    app._fee_rates = (0.00015, 0.002)
    for key, value in VALUES.items():
        if key in app._funds_vars:
            app._funds_vars[key].set(value)
    app._notify_combo.set("전체")

    app._open_settings()
    app.update()
    dlg = [w for w in app.winfo_children() if w.winfo_class() == "Toplevel"][-1]
    yield app, dlg
    if dlg.winfo_exists():
        dlg.destroy()


def _drain(b) -> list:
    out = []
    while not b.commands.empty():
        out.append(b.commands.get_nowait())
    return out


def test_지금_값이_채워진_채로_열린다(dialog):
    """빈 칸으로 열리면 무엇이 설정돼 있었는지 알 수 없다."""
    _app, dlg = dialog

    assert dlg._vars["total"].get() == "1930000"
    assert dlg._vars["tax"].get() == "0.2"  # config 의 0.002 를 % 로 보여 준다
    assert dlg._notify.get() == "전체"


def test_저장하면_세_경로로_모두_나간다(dialog):
    """자금·거래비용·알림이 각자의 길로 간다 — 하나라도 빠지면 조용히 안 바뀐다."""
    app, dlg = dialog
    _drain(app._bus)
    dlg._vars["total"].set("2000000")
    dlg._vars["tax"].set("0.25")
    dlg._notify.set("끔")

    dlg._save()

    kinds = {type(c).__name__ for c in _drain(app._bus)}
    assert kinds == {"SetFunds", "SetFees", "SetNotifyLevel"}


def test_안_바뀐_값은_다시_보내지_않는다(dialog):
    """config.toml 이 매번 갱신되고 로그가 지저분해진다."""
    app, dlg = dialog
    _drain(app._bus)
    dlg._vars["total"].set("2000000")  # 자금만 바꾼다

    dlg._save()

    kinds = {type(c).__name__ for c in _drain(app._bus)}
    assert kinds == {"SetFunds"}


def test_저장하면_툴바_칸도_따라_바뀐다(dialog):
    """툴바와 창이 다른 값을 보이면 어느 쪽이 맞는지 알 수 없다."""
    app, dlg = dialog
    dlg._vars["total"].set("2000000")

    dlg._save()

    assert app._funds_vars["total"].get() == "2,000,000"


def test_입력이_잘못되면_창이_닫히지_않는다(dialog, monkeypatch):
    """닫혀 버리면 무엇을 잘못 넣었는지 못 보고 처음부터 다시 채워야 한다."""
    app, dlg = dialog
    monkeypatch.setattr(
        "trader.ui.settings_dialog.messagebox.showwarning", lambda *a, **k: None
    )
    _drain(app._bus)
    dlg._vars["ratio3"].set("20")  # 합이 110%

    dlg._save()

    assert dlg.winfo_exists()
    assert _drain(app._bus) == []


def test_감시_중에는_저장할_수_없다(app):
    """진입 시점의 조건으로 끝까지 가야 한다 — 도중에 3선이 바뀌면 근거가 흔들린다."""
    from trader.ui.settings_dialog import TradeSettingsDialog

    saved = []
    dlg = TradeSettingsDialog(app, VALUES, saved.append, running=True)
    app.update()

    dlg._save()

    assert saved == []
    assert "disabled" in dlg._save_btn.state()
    dlg.destroy()


def test_종목당_배분액을_실시간으로_보여_준다(dialog):
    """1·2차 금액을 정할 때의 상한이라, 총액을 고치는 중에 보여야 쓸모가 있다."""
    _app, dlg = dialog

    dlg._vars["total"].set("2100000")
    dlg._vars["max"].set("7")
    dlg._update_per_symbol()

    assert "300,000" in dlg._per_symbol.cget("text")
