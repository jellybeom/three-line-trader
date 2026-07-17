"""키움 WebSocket 실시간 시세 수신기 (watcher).

프로토콜 (키움 공식 가이드 기준):
- 접속 → LOGIN {'trnm':'LOGIN','token':...} → {'trnm':'LOGIN','return_code':0}
- 등록   {'trnm':'REG','grp_no':'1','refresh':'1','data':[{'item':[코드들],'type':['0B']}]}
- 수신   {'trnm':'REAL','data':[{'type':'0B','item':코드,'values':{'10':현재가,...}}]}
- 서버 PING {'trnm':'PING'} 은 받은 그대로 되돌려 보내야 연결이 유지된다.

0B(주식체결)의 '10' 필드가 현재가이며, 키움 관례상 등락 부호(+/-)가 붙어
오므로 절대값으로 파싱한다.

메시지 해석은 순수 함수(parse_message / extract_ticks)로 분리해
네트워크 없이 단위 테스트한다. Watcher 는 끊기면 지수 백오프로 자동
재연결하고, 재연결 성공 시 종목을 재등록한 뒤 on_reconnect 를 호출한다
(코어가 REST 현재가 조회로 공백 구간을 1회 보정하는 용도).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Awaitable, Callable

import websockets

_RECONNECT_BASE = 1  # 초. 재시도마다 2배, 최대 30초
_RECONNECT_MAX = 30


@dataclass(frozen=True)
class Tick:
    symbol: str
    price: float
    time: str  # 체결시간 HHMMSS (없으면 "")


# ── 순수 파싱 함수 (단위 테스트 대상) ──────────────────────────


def parse_message(raw: str) -> tuple[str, object]:
    """수신 원문 → (종류, 페이로드). 종류: ping / login / real / other."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return "other", raw
    match msg.get("trnm"):
        case "PING":
            return "ping", msg  # 받은 그대로 응답해야 함
        case "LOGIN":
            return "login", msg.get("return_code") == 0
        case "REAL":
            return "real", extract_ticks(msg.get("data", []))
        case _:
            return "other", msg


def extract_ticks(data: list) -> list[Tick]:
    """REAL 데이터에서 0B(주식체결) 항목만 Tick 으로 변환. 이상 항목은 건너뛴다."""
    ticks = []
    for entry in data:
        if entry.get("type") != "0B":
            continue
        values = entry.get("values", {})
        try:
            price = abs(float(values["10"]))  # 등락 부호(+/-) 제거
        except (KeyError, ValueError, TypeError):
            continue
        ticks.append(Tick(entry.get("item", ""), price, values.get("20", "")))
    return ticks


# ── WebSocket 수신기 ───────────────────────────────────────────


class Watcher:
    """실시간 체결가 수신. 코어의 asyncio 루프 안에서 run() 으로 구동한다."""

    def __init__(
        self,
        ws_url: str,
        token_provider: Callable[[], str],  # KiwoomAuth.token (재연결 시 재발급 반영)
        on_tick: Callable[[Tick], Awaitable[None]],
        on_status: Callable[[str], Awaitable[None]],  # 연결/재연결/오류 로그
        on_reconnect: Callable[[], Awaitable[None]] | None = None,
    ):
        self._ws_url = ws_url
        self._token_provider = token_provider
        self._on_tick = on_tick
        self._on_status = on_status
        self._on_reconnect = on_reconnect
        self._symbols: list[str] = []
        self._ws = None
        self._stopped = False

    async def update_symbols(self, symbols: list[str]) -> None:
        """감시 종목 교체. 연결 중이면 즉시 재등록한다."""
        self._symbols = list(symbols)
        if self._ws is not None:
            await self._register(self._ws)

    async def stop(self) -> None:
        self._stopped = True
        if self._ws is not None:
            await self._ws.close()

    async def run(self) -> None:
        """수신 루프. 예외든 서버측 정상 종료든, 끊기면 지수 백오프로 재연결한다."""
        delay = _RECONNECT_BASE
        first = True
        while not self._stopped:
            self._session_ok = False
            try:
                await self._session(first_connect=first)
                reason = "서버가 연결을 종료함"  # 예외 없이 수신 루프가 끝난 경우
            except Exception as e:  # noqa: BLE001 — 네트워크 계열 전부 재시도 대상
                reason = str(e) or type(e).__name__
            if self._stopped:
                break
            if self._session_ok:
                delay = _RECONNECT_BASE  # 정상 세션이었다면 백오프 리셋
            first = False
            await self._on_status(f"WebSocket 끊김({reason}) → {delay}초 후 재연결")
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RECONNECT_MAX)

    # ── 내부 ────────────────────────────────────────────────────

    async def _session(self, first_connect: bool) -> None:
        async with websockets.connect(self._ws_url) as ws:
            self._ws = ws
            try:
                await self._login(ws)
                await self._register(ws)
                self._session_ok = True  # 로그인·등록까지 성공한 정상 세션
                await self._on_status(
                    ("연결됨" if first_connect else "재연결됨")
                    + f" · 감시 {len(self._symbols)}종목"
                )
                if not first_connect and self._on_reconnect:
                    await self._on_reconnect()  # 공백 구간 보정은 코어가 REST 로 수행
                async for raw in ws:
                    await self._handle(ws, raw)
            finally:
                self._ws = None

    async def _login(self, ws) -> None:
        await ws.send(json.dumps({"trnm": "LOGIN", "token": self._token_provider()}))
        async with asyncio.timeout(10):
            async for raw in ws:
                kind, payload = parse_message(raw)
                if kind == "ping":
                    await ws.send(raw)
                    continue
                if kind == "login":
                    if payload is not True:
                        raise ConnectionError(f"WebSocket 로그인 거부: {raw}")
                    return
        raise ConnectionError("로그인 응답 없음")

    async def _register(self, ws) -> None:
        if not self._symbols:
            return
        await ws.send(
            json.dumps(
                {
                    "trnm": "REG",
                    "grp_no": "1",
                    "refresh": "1",
                    "data": [{"item": self._symbols, "type": ["0B"]}],
                }
            )
        )

    async def _handle(self, ws, raw: str) -> None:
        kind, payload = parse_message(raw)
        if kind == "ping":
            await ws.send(raw)
        elif kind == "real":
            for tick in payload:
                await self._on_tick(tick)
