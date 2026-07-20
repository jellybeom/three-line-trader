"""Discord 알림 (notifier) — webhook 발송 + 알림 수준 필터.

- 발송은 blocking(requests)이므로 코어에서는 asyncio.to_thread,
  시뮬레이터에서는 백그라운드 스레드로 호출한다 (매매 루프를 막지 않음).
- 알림 수준(UI Discord 그룹의 콤보, settings 영속):
    전체              → 모든 로그
    매매만 (시스템 제외) → 종목 관련 로그만 (symbol != "시스템")
    에러만            → 에러 / 경고만
    끔                → 발송 안 함
- Discord webhook 은 분당 약 30건 제한이 있다. '전체' 수준에서 로그가 많은 날은
  일부가 지연·거부될 수 있으니 평소엔 '매매만' 을 권장.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import requests


class NotifierError(RuntimeError):
    """webhook 미설정 또는 발송 실패."""


def load_webhook(config_path: str | Path = "config.toml") -> str:
    path = Path(config_path)
    if not path.exists():
        raise NotifierError(f"{config_path} 가 없습니다.")
    url = (
        tomllib.loads(path.read_text(encoding="utf-8"))
        .get("discord", {})
        .get("webhook_url", "")
    )
    if not url:
        raise NotifierError("config.toml 의 [discord] webhook_url 이 비어 있습니다.")
    return url


def should_notify(level: str, symbol: str, kind: str) -> bool:
    """알림 수준 필터 — 로그 한 줄을 Discord 로 보낼지 결정한다."""
    if level == "끔":
        return False
    if level == "에러만":
        return kind in ("에러", "경고")
    if level.startswith("매매만"):
        return symbol != "시스템"
    return True  # 전체


def format_message(symbol: str, kind: str, text: str) -> str:
    prefix = f"**[{kind}]**"
    return f"{prefix} {text}" if symbol == "시스템" else f"{prefix} {symbol} · {text}"


class DiscordNotifier:
    def __init__(self, webhook_url: str):
        self._url = webhook_url

    def send(self, text: str) -> None:
        """blocking 발송. Discord 는 성공 시 204(내용 없음)를 돌려준다."""
        resp = requests.post(self._url, json={"content": text[:1900]}, timeout=10)
        if resp.status_code not in (200, 204):
            raise NotifierError(
                f"Discord 발송 실패 (HTTP {resp.status_code}): {resp.text[:200]}"
            )
