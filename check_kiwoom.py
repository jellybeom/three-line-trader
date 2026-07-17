"""키움 연결 점검 — 앱키 발급 후 최초 1회, 실제 접속을 검증한다.

    uv run check_kiwoom.py [종목코드]   (기본: 005930 삼성전자)

프로젝트 루트에 config.toml 이 필요하다 (config.toml.example 참고).
확인 순서: ① 접근토큰 발급 → ② WebSocket 로그인 → ③ 실시간 등록 → ④ 틱 수신.
장 운영 시간이 아니면 ④에서 틱이 없는 게 정상이며, ①~③ 성공만으로 연결은 검증된 것이다.
"""

from __future__ import annotations

import asyncio
import sys
import tomllib
from pathlib import Path

from trader.kiwoom import KiwoomAuth
from trader.watcher import Tick, Watcher

_TIMEOUT = 30  # 초


def load_auth() -> KiwoomAuth:
    path = Path("config.toml")
    if not path.exists():
        sys.exit(
            "config.toml 이 없습니다. config.toml.example 을 복사해 키를 채워주세요."
        )
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))["kiwoom"]
    return KiwoomAuth(cfg["appkey"], cfg["secretkey"], mock=cfg.get("mock", True))


async def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "005930"
    auth = load_auth()

    print(f"[1/4] 접근토큰 발급 시도 ({'모의' if auth.mock else '실전'}투자)...")
    token = auth.token()
    print(f"      성공: {token[:10]}*** (만료 {auth._expires_at})")

    received = 0

    async def on_tick(tick: Tick) -> None:
        nonlocal received
        received += 1
        print(f"      틱 수신: {tick.symbol} {tick.price:,.0f} @ {tick.time}")

    async def on_status(msg: str) -> None:
        print(f"[3/4] {msg}")

    watcher = Watcher(auth.ws_url, auth.token, on_tick, on_status)
    await watcher.update_symbols([symbol])

    print(f"[2/4] WebSocket 접속·로그인 시도...")
    print(
        f"[4/4] {symbol} 실시간 등록 후 {_TIMEOUT}초간 수신 대기 (장중이 아니면 틱 없음이 정상)"
    )
    task = asyncio.create_task(watcher.run())
    await asyncio.sleep(_TIMEOUT)
    await watcher.stop()
    task.cancel()
    print(f"\n완료: 틱 {received}건 수신. 로그인·등록까지 성공했다면 연결 검증 OK.")


if __name__ == "__main__":
    asyncio.run(main())
