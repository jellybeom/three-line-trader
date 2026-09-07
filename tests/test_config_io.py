"""`config.toml` 읽기·쓰기.

설정창이 생기면서 프로그램이 이 파일을 **쓰게** 됐다. 통째로 다시 쓰면 몇 달에 걸쳐
실측으로 쌓은 주석이 한 번에 사라지므로, 값 줄만 갈아 끼우는지를 집중적으로 본다.
"""

from __future__ import annotations

import tomllib

import pytest

from trader import config_io

SAMPLE = """# 프로그램 설정
# 이 파일은 커밋하지 않는다.

[fees]
# tax_rate: 2026-08 기준 코스피와 코스닥 모두 0.20% 입니다.
#   0.15% 로 두면 세후 손익이 실제보다 좋아 보입니다
#   (2026-08-24 실측: 매도대금 547,120원에 세금 1,091원 = 0.1994%).
commission_rate = 0.00015
tax_rate = 0.0015

[schedule]
enabled = false
start = "08:55"    # 감시 자동 시작
"""


def test_값만_바뀌고_주석은_그대로다():
    """주석은 왜 그렇게 정했는지의 기록이다 — 저장 한 번에 사라지면 안 된다."""
    out = config_io.apply_updates(SAMPLE, {"fees.tax_rate": 0.002})

    assert "tax_rate = 0.002" in out
    assert "0.1994%" in out  # 실측 근거가 살아 있다
    assert out.count("#") == SAMPLE.count("#")


def test_값_뒤의_주석도_지킨다():
    """`start = "08:55"    # 감시 자동 시작` 의 오른쪽 설명이 사라지면 안 된다."""
    out = config_io.apply_updates(SAMPLE, {"schedule.start": "09:00"})

    assert '"09:00"' in out
    assert "# 감시 자동 시작" in out


def test_같은_이름의_키를_섹션별로_구분한다():
    """`[kiwoom.mock] appkey` 와 `[kiwoom.real] appkey` 는 다른 값이다."""
    text = '[kiwoom.mock]\nappkey = "모의"\n\n[kiwoom.real]\nappkey = "실전"\n'

    out = config_io.apply_updates(text, {"kiwoom.real.appkey": "새키"})
    data = tomllib.loads(out)

    assert data["kiwoom"]["mock"]["appkey"] == "모의"  # 건드리지 않았다
    assert data["kiwoom"]["real"]["appkey"] == "새키"


def test_없는_키는_그_섹션_끝에_넣는다():
    """설정 항목이 늘어도 사용자가 손으로 섹션을 만들 필요가 없다."""
    out = config_io.apply_updates(SAMPLE, {"schedule.summary": "15:35"})
    data = tomllib.loads(out)

    assert data["schedule"]["summary"] == "15:35"
    assert data["schedule"]["enabled"] is False  # 기존 값은 그대로


def test_없는_섹션은_파일_끝에_만든다():
    out = config_io.apply_updates(SAMPLE, {"pdf.font": "malgun.ttf"})

    assert tomllib.loads(out)["pdf"]["font"] == "malgun.ttf"


def test_윈도우_경로의_역슬래시가_살아남는다():
    r"""`C:\Windows\Fonts\malgun.ttf` 가 `\W` 로 깨지면 폰트를 못 찾는다."""
    path = r"C:\Windows\Fonts\malgun.ttf"

    out = config_io.apply_updates(SAMPLE, {"pdf.font": path})

    assert tomllib.loads(out)["pdf"]["font"] == path


def test_목록과_참거짓도_TOML_로_적는다():
    out = config_io.apply_updates(
        SAMPLE, {"discord.allowed_users": ["111", "222"], "schedule.enabled": True}
    )
    data = tomllib.loads(out)

    assert data["discord"]["allowed_users"] == ["111", "222"]
    assert data["schedule"]["enabled"] is True


def test_따옴표_안의_샵은_주석이_아니다():
    """`memo = "a#b"` 를 주석으로 오해하면 값이 잘린다."""
    text = '[x]\nmemo = "a#b"\n'

    out = config_io.apply_updates(text, {"x.other": 1})

    assert tomllib.loads(out)["x"]["memo"] == "a#b"


# ── 저장 ────────────────────────────────────────────────────────


def test_저장_뒤_다시_읽으면_같은_값이다(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(SAMPLE, encoding="utf-8")

    config_io.write({"fees.tax_rate": 0.002}, cfg)

    assert config_io.read(cfg)["fees"]["tax_rate"] == 0.002


def test_깨지는_설정은_저장하지_않는다(tmp_path):
    """반쯤 쓰인 파일이 남으면 다음 실행에서 프로그램이 아예 안 뜬다."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(SAMPLE, encoding="utf-8")

    with pytest.raises(config_io.ConfigError):
        config_io.write({"fees.tax_rate": object()}, cfg)

    assert cfg.read_text(encoding="utf-8") == SAMPLE  # 원본 그대로


def test_임시_파일을_남기지_않는다(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(SAMPLE, encoding="utf-8")

    config_io.write({"fees.tax_rate": 0.002}, cfg)

    assert list(tmp_path.glob("*.tmp")) == []


def test_파일이_없으면_빈_설정으로_읽는다(tmp_path):
    """처음 실행이라 설정이 없어도 프로그램은 떠야 한다."""
    assert config_io.read(tmp_path / "없음.toml") == {}


def test_점으로_이어진_경로로_값을_꺼낸다():
    data = {"kiwoom": {"real": {"appkey": "K"}}}

    assert config_io.get(data, "kiwoom.real.appkey") == "K"
    assert config_io.get(data, "kiwoom.mock.appkey", "기본") == "기본"
    assert config_io.get(data, "없는.경로") is None


def test_설정_예시가_그대로_읽히고_써진다():
    """실제 파일로 한 바퀴 돌려 본다 — 예시가 곧 사용자가 복사할 원본이다."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config.toml.example"
    text = example.read_text(encoding="utf-8")

    out = config_io.apply_updates(text, {"fees.tax_rate": 0.002})

    assert tomllib.loads(out)["fees"]["tax_rate"] == 0.002
    assert out.count("#") == text.count("#")  # 주석 한 줄도 잃지 않았다
