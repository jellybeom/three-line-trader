"""환경 설정 다이얼로그 — `config.toml` 의 값들.

여기서 실수하면 **키가 지워지거나 스케줄이 어긋난다.** 화면 배치보다 그 두 가지를 본다.
"""

from __future__ import annotations

import pytest

from trader import config_io
from trader.ui.env_dialog import (
    EnvSettingsDialog,
    check_times,
    is_masked,
    mask,
    parse_users,
)

VALUES = {
    "mode_real": False,
    "kiwoom.real.appkey": "REALAPPKEY1234",
    "kiwoom.real.secretkey": "REALSECRET9b71",
    "kiwoom.real.account": "5790-8081 위탁종합",
    "kiwoom.mock.appkey": "MOCKKEY",
    "kiwoom.mock.secretkey": "MOCKSECRET",
    "kiwoom.mock.account": "",
    "discord.bot_token": "TOKENabcdKa7X",
    "discord.channel_id": "8793",
    "discord.journal_channel_id": "4558",
    "discord.log_channel_id": "1277",
    "discord.allowed_users": ["111"],
    "startup.auto_connect": True,
    "schedule.enabled": False,
    "schedule.start": "08:55",
    "schedule.stop": "15:30",
    "schedule.summary": "15:35",
    "chart.font": "",
    "pdf.font": "",
    "pdf.font_bold": "",
}


# ── 비밀값 가리기 ───────────────────────────────────────────────


def test_비밀값은_뒤_네_자리만_보여_준다():
    """원격 접속 화면이 노출되거나 화면을 공유할 때 토큰이 그대로 보인다."""
    assert mask("REALAPPKEY1234") == "••••••••1234"
    assert mask("") == ""  # 빈 값은 가릴 것이 없다
    assert mask("abc") == "••••••••"  # 짧으면 뒷자리도 안 보여 준다


def test_안_건드린_비밀값은_저장하지_않는다(app):
    """덮어쓰면 `••••1234` 가 그대로 저장되어 **키가 망가진다.**"""
    dlg = EnvSettingsDialog(app, VALUES, lambda _u: None, lambda _r: True)
    app.update()

    updates = dlg.updates()

    assert "kiwoom.real.appkey" not in updates
    assert "discord.bot_token" not in updates
    dlg.destroy()


def test_새로_입력한_비밀값은_저장한다(app):
    dlg = EnvSettingsDialog(app, VALUES, lambda _u: None, lambda _r: True)
    app.update()
    dlg._vars["discord.bot_token"].set("새토큰")

    assert dlg.updates()["discord.bot_token"] == "새토큰"
    dlg.destroy()


def test_가려진_칸을_클릭하면_비운다():
    """뒤에 이어 쓰면 `••••1234새키` 가 된다."""
    assert is_masked("••••••••1234")
    assert not is_masked("새로쓴키")


# ── 허용 사용자 ─────────────────────────────────────────────────


def test_허용_사용자는_쉼표든_공백이든_받는다():
    """긴 숫자를 옮겨 적는 자리라 구분자를 하나로 강요하면 실수가 는다."""
    assert parse_users("111, 222 333\n444") == ["111", "222", "333", "444"]
    assert parse_users("") == []


# ── 스케줄 시각 ─────────────────────────────────────────────────


def test_시각_형식이_아니면_거부한다():
    with pytest.raises(ValueError, match="HH:MM"):
        check_times({**_times(), "schedule.start": "8시 55분"})
    with pytest.raises(ValueError, match="HH:MM"):
        check_times({**_times(), "schedule.stop": "25:00"})


def test_순서가_뒤집히면_거부한다():
    """중지가 시작보다 이르면 그날 감시가 아예 안 돈다."""
    with pytest.raises(ValueError, match="순서"):
        check_times({**_times(), "schedule.stop": "08:00"})


def test_요약이_중지보다_이르면_거부한다():
    """아직 끝나지 않은 매매로 요약을 만들게 된다."""
    with pytest.raises(ValueError, match="순서"):
        check_times({**_times(), "schedule.summary": "15:00"})


def test_바른_시각은_통과한다():
    check_times(_times())  # 예외가 없으면 통과


def _times() -> dict:
    return {
        "schedule.start": "08:55",
        "schedule.stop": "15:30",
        "schedule.summary": "15:35",
    }


# ── 저장 ────────────────────────────────────────────────────────


def test_저장하면_주석을_지킨_채_값만_바뀐다(app, tmp_path):
    """설정 파일의 주석은 왜 그렇게 정했는지의 기록이다."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config.toml.example"
    cfg = tmp_path / "config.toml"
    cfg.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    before = cfg.read_text(encoding="utf-8")

    app._config_path = str(cfg)
    app._open_env_settings()
    app.update()
    dlg = [w for w in app.winfo_children() if w.winfo_class() == "Toplevel"][-1]
    dlg._vars["discord.channel_id"].set("999")
    dlg._schedule_on.set(True)
    dlg._save()

    data = config_io.read(cfg)
    assert data["discord"]["channel_id"] == "999"
    assert data["schedule"]["enabled"] is True
    assert cfg.read_text(encoding="utf-8").count("#") == before.count("#")


def test_시각이_어긋나면_창이_닫히지_않는다(app, monkeypatch):
    monkeypatch.setattr(
        "trader.ui.env_dialog.messagebox.showwarning", lambda *a, **k: None
    )
    saved = []
    dlg = EnvSettingsDialog(app, VALUES, saved.append, lambda _r: True)
    app.update()
    dlg._vars["schedule.stop"].set("08:00")

    dlg._save()

    assert saved == []
    assert dlg.winfo_exists()
    dlg.destroy()


# ── 모드 전환 ───────────────────────────────────────────────────


def test_모드는_저장_버튼과_무관하게_즉시_처리한다(app):
    """DB 교체·연결 해제라는 부수효과가 있어, 다른 값과 함께 저장하면 순서에 좌우된다."""
    asked = []
    dlg = EnvSettingsDialog(
        app, VALUES, lambda _u: None, lambda r: asked.append(r) or True
    )
    app.update()

    dlg._mode_var.set("실전")
    dlg._switch_mode()

    assert asked == [True]
    assert (
        "mode_real" not in dlg.updates()
    )  # 파일에 쓰지 않는다 (mode.txt 가 따로 있다)
    dlg.destroy()


def test_전환을_거절하면_라디오가_되돌아간다(app):
    """확인창에서 '아니오' 를 눌렀는데 화면만 실전으로 보이면 안 된다."""
    dlg = EnvSettingsDialog(app, VALUES, lambda _u: None, lambda _r: False)
    app.update()

    dlg._mode_var.set("실전")
    dlg._switch_mode()

    assert dlg._mode_var.get() == "모의"
    dlg.destroy()


def test_감시_중에는_모드를_바꿀_수_없다(app):
    dlg = EnvSettingsDialog(app, VALUES, lambda _u: None, lambda _r: True, running=True)
    app.update()

    radios = [
        w
        for frame in dlg.winfo_children()
        for w in _walk(frame)
        if w.winfo_class() == "TRadiobutton"
    ]
    assert radios and all("disabled" in w.state() for w in radios)
    dlg.destroy()


def _walk(widget):
    for child in widget.winfo_children():
        yield child
        yield from _walk(child)


def test_탭을_전환해도_비밀값이_사라지지_않는다(app):
    """`<FocusIn>` 으로 마스크를 지우면 탭을 눌러 페이지가 보이기만 해도 날아간다.

    2026-09-07 실측: 실전 키 ↔ 모의 키 탭을 오가면 앱키 칸이 비었다.
    """
    dlg = EnvSettingsDialog(app, VALUES, lambda _u: None, lambda _r: True)
    app.update()
    tabs = next(w for w in _walk(dlg) if w.winfo_class() == "TNotebook")

    for index in (1, 0, 1, 0):
        tabs.select(index)
        app.update()

    assert dlg._vars["kiwoom.real.appkey"].get() == "••••••••1234"
    assert dlg._vars["kiwoom.mock.appkey"].get() == mask("MOCKKEY")
    assert "kiwoom.real.appkey" not in dlg.updates()  # 안 건드린 것으로 남는다
    dlg.destroy()


def test_글자를_치면_가려진_값이_통째로_지워진다(app):
    """뒤에 이어 쓰면 `••••1234새키` 가 저장되어 키가 망가진다."""
    import tkinter as tk

    dlg = EnvSettingsDialog(app, VALUES, lambda _u: None, lambda _r: True)
    app.update()
    var = dlg._vars["discord.bot_token"]

    dlg._clear_mask(tk.Event(), var)  # keysym 이 없는 일반 입력
    assert var.get() == ""
    dlg.destroy()


def test_이동_키로는_지우지_않는다(app):
    """Tab 이나 방향키는 내용을 바꾸려는 것이 아니다."""
    import tkinter as tk

    dlg = EnvSettingsDialog(app, VALUES, lambda _u: None, lambda _r: True)
    app.update()
    var = dlg._vars["discord.bot_token"]

    for keysym in ("Tab", "Left", "Control_L"):
        event = tk.Event()
        event.keysym = keysym
        dlg._clear_mask(event, var)

    assert dlg._vars["discord.bot_token"].get().startswith("•")
    dlg.destroy()
