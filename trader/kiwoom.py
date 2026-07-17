"""키움증권 REST API 공통 — 접속 정보와 접근토큰 관리.

모의/실전은 도메인과 앱키가 전부 다르다 (키 별도 발급).
엔드포인트·필드명은 키움 공식 가이드(https://openapi.kiwoom.com/guide/apiguide)
기준이며, API 개정 시 이 파일만 고치면 되도록 접속 상수를 한곳에 모았다.

watcher(시세)와 broker(주문)가 KiwoomAuth 하나를 공유한다 —
토큰은 캐시되고 만료가 임박하면 자동 재발급된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import requests

REAL_HOST = "https://api.kiwoom.com"
MOCK_HOST = "https://mockapi.kiwoom.com"
REAL_WS_URL = "wss://api.kiwoom.com:10000/api/dostk/websocket"
MOCK_WS_URL = "wss://mockapi.kiwoom.com:10000/api/dostk/websocket"

_REFRESH_MARGIN = timedelta(minutes=10)  # 만료 10분 전부터 미리 재발급


class KiwoomAuthError(RuntimeError):
    """토큰 발급 실패 (키 오류, 네트워크 등)."""


@dataclass
class KiwoomAuth:
    appkey: str
    secretkey: str
    mock: bool = True  # 모의투자 기본. 실전은 False + 실전용 키 필요

    _token: str = field(default="", repr=False)
    _expires_at: datetime = field(default=datetime.min, repr=False)

    @property
    def host(self) -> str:
        return MOCK_HOST if self.mock else REAL_HOST

    @property
    def ws_url(self) -> str:
        return MOCK_WS_URL if self.mock else REAL_WS_URL

    def token(self) -> str:
        """유효한 접근토큰 반환. 없거나 만료 임박이면 새로 발급받는다."""
        if self._token and datetime.now() < self._expires_at - _REFRESH_MARGIN:
            return self._token
        self._issue()
        return self._token

    def _issue(self) -> None:
        resp = requests.post(
            f"{self.host}/oauth2/token",
            json={
                "grant_type": "client_credentials",
                "appkey": self.appkey,
                "secretkey": self.secretkey,
            },
            timeout=10,
        )
        data = resp.json()
        token = data.get("token") or data.get("access_token")
        if resp.status_code != 200 or not token:
            raise KiwoomAuthError(f"토큰 발급 실패 (HTTP {resp.status_code}): {data}")
        self._token = token
        self._expires_at = _parse_expires(data.get("expires_dt", ""))


def _parse_expires(expires_dt: str) -> datetime:
    """만료 시각 'YYYYMMDDHHMMSS' 파싱. 형식이 다르면 보수적으로 12시간 뒤로 간주."""
    try:
        return datetime.strptime(expires_dt, "%Y%m%d%H%M%S")
    except ValueError:
        return datetime.now() + timedelta(hours=12)
