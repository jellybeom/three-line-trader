"""`config.toml` 읽기·쓰기 — **주석을 지킨다.**

지금까지 이 파일은 손으로 적고 프로그램은 읽기만 했다. 설정창이 생기면서 프로그램이
쓰게 되는데, 통째로 다시 쓰면 **주석이 전부 날아간다.**

    # tax_rate: 2026-08 기준 코스피와 코스닥 모두 0.20% 입니다.
    #   0.15% 로 두면 세후 손익이 실제보다 좋아 보입니다
    #   (2026-08-24 실측: 매도대금 547,120원에 세금 1,091원 = 0.1994%).
    tax_rate = 0.002

몇 달에 걸쳐 실측으로 쌓은 근거이고, **이 프로젝트의 기억**이다. 한 번의 저장으로
사라지면 다시 만들 수 없다. 그래서 값이 있는 줄만 찾아 오른쪽만 갈아 끼운다.

저장은 **임시 파일에 쓰고 바꿔치기**한다. 직접 덮어쓰다 프로그램이 죽으면 파일이 반쯤
쓰인 채 남고, 다음 실행에서 아예 안 뜬다.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

CONFIG = "config.toml"


class ConfigError(RuntimeError):
    """설정 파일을 읽거나 쓸 수 없음."""


def read(path: str | Path = CONFIG) -> dict:
    """설정 전체를 dict 로. 파일이 없으면 빈 dict."""
    file = Path(path)
    if not file.exists():
        return {}
    try:
        return tomllib.loads(file.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as err:
        raise ConfigError(f"{file} 를 읽을 수 없습니다: {err}") from err


def get(data: dict, dotted: str, default=None):
    """`kiwoom.real.appkey` 처럼 점으로 이어진 경로를 따라간다."""
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _format(value) -> str:
    """TOML 값 표기. 문자열은 따옴표, 목록은 대괄호.

    담을 수 없는 형은 **여기서 거부한다.** `str()` 로 억지로 바꾸면
    `<object at 0x...>` 같은 것이 설정 파일에 그대로 적힌다.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_format(v) for v in value) + "]"
    if not isinstance(value, str):
        raise ConfigError(f"설정에 담을 수 없는 값입니다: {type(value).__name__}")
    text = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _section_of(line: str) -> str | None:
    """`[kiwoom.real]` → `kiwoom.real`. 섹션 줄이 아니면 None."""
    match = re.match(r"\s*\[([^\]]+)\]\s*(#.*)?$", line)
    return match.group(1).strip() if match else None


def apply_updates(text: str, updates: dict) -> str:
    """본문에서 **값만** 갈아 끼운다. 주석과 배치는 그대로 둔다.

    updates 는 `{"fees.tax_rate": 0.002}` 처럼 점 경로다. 없는 키는 해당 섹션 끝에
    새 줄로 넣고, 섹션 자체가 없으면 파일 끝에 만든다 — 설정 항목이 늘어도 사용자가
    손으로 섹션을 만들 필요가 없다.
    """
    remaining = dict(updates)
    lines = text.splitlines()
    out: list[str] = []
    section = ""
    # 각 섹션의 마지막 값 줄 위치 — 새 키를 그 뒤에 끼워 넣는다
    last_value_at: dict[str, int] = {}

    for line in lines:
        if (found := _section_of(line)) is not None:
            section = found
            out.append(line)
            continue
        match = re.match(r"(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$", line)
        if match:
            indent, key, eq, rest = match.groups()
            dotted = f"{section}.{key}" if section else key
            comment = ""
            if (pos := _trailing_comment(rest)) is not None:
                comment = rest[pos:]
            if dotted in remaining:
                value = _format(remaining.pop(dotted))
                line = f"{indent}{key}{eq}{value}{(' ' + comment) if comment else ''}"
            last_value_at[section] = len(out)
        out.append(line)

    for dotted, value in remaining.items():
        section, _, key = dotted.rpartition(".")
        entry = f"{key} = {_format(value)}"
        if section in last_value_at:
            out.insert(last_value_at[section] + 1, entry)
            for name, at in last_value_at.items():
                if at > last_value_at[section]:
                    last_value_at[name] = at + 1
            last_value_at[section] += 1
        else:
            out += ["", f"[{section}]" if section else "", entry]
            last_value_at[section] = len(out) - 1
    return "\n".join(out).rstrip() + "\n"


def _trailing_comment(rest: str) -> int | None:
    """값 뒤 주석의 시작 위치. 따옴표 안의 `#` 는 주석이 아니다."""
    quote = None
    for i, ch in enumerate(rest):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return i
    return None


def write(updates: dict, path: str | Path = CONFIG) -> None:
    """값을 갈아 끼워 저장한다. **임시 파일에 쓰고 바꿔치기한다.**

    직접 덮어쓰다 프로그램이 죽으면 파일이 반쯤 쓰인 채 남고, 다음 실행에서 설정을
    읽지 못해 아예 뜨지 않는다. `os.replace` 는 같은 디스크 안에서 원자적이다.
    """
    file = Path(path)
    original = file.read_text(encoding="utf-8") if file.exists() else ""
    updated = apply_updates(original, updates)
    try:
        tomllib.loads(updated)  # 쓰기 전에 읽히는지 확인한다
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(f"설정을 만들 수 없습니다: {err}") from err

    tmp = file.with_suffix(file.suffix + ".tmp")
    try:
        tmp.write_text(updated, encoding="utf-8")
        os.replace(tmp, file)
    except OSError as err:
        tmp.unlink(missing_ok=True)
        raise ConfigError(f"{file} 에 쓸 수 없습니다: {err}") from err
