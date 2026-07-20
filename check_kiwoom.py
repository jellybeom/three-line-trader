"""키움 연결 점검 — 앱키 발급 후 최초 1회, 실제 접속을 검증한다.

    uv run check_kiwoom.py [종목코드]          (기본: 005930 삼성전자)
    uv run check_kiwoom.py [종목코드] --buy1   (모의투자 한정: 시장가 1주 매수까지 검증)

프로젝트 루트에 config.toml 이 필요하다 (config.toml.example 참고).
확인 순서: ① 접근토큰 → ② REST 조회(종목정보·주문가능금액·보유잔고)
→ ③ WebSocket 로그인·등록 → ④ 틱 수신.
장 운영 시간이 아니면 ④에서 틱이 없는 게 정상이며, ③까지 성공하면 연결은 검증된 것이다.
--buy1 은 주문 API(kt10000)와 체결통보(00) 필드까지 실측하는 옵션으로,
실전(mock=false) 설정에서는 안전을 위해 거부된다. 장중에 실행해야 체결까지 확인된다.
"""

from __future__ import annotations

import asyncio
import sys

from trader.broker import Broker, extract_fill
from trader.kiwoom import load_auth
from trader.watcher import Tick, Watcher

_TIMEOUT = 30  # 초


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    symbol = args[0] if args else "005930"
    do_buy1 = "--buy1" in sys.argv
    auth = load_auth(real="--real" in sys.argv)

    print(f"[1/4] 접근토큰 발급 시도 ({'모의' if auth.mock else '실전'}투자)...")
    token = auth.token()
    print(f"      성공: {token[:10]}*** (만료 {auth._expires_at})")

    print("[2/4] REST 조회 검증...")
    broker = Broker(auth)
    name, price = broker.stock_info(symbol)
    print(f"      종목정보: {symbol} {name} · 현재가 {price:,.0f}")
    print(f"      주문가능금액: {broker.deposit():,.0f}")
    holdings = broker.holdings()
    print(f"      보유잔고: {holdings if holdings else '없음'}")

    if do_buy1 and not auth.mock:
        sys.exit("--buy1 은 모의투자(mock=true)에서만 허용됩니다.")

    received = 0
    connected = asyncio.Event()

    async def on_tick(tick: Tick) -> None:
        nonlocal received
        received += 1
        print(f"      틱 수신: {tick.symbol} {tick.price:,.0f} @ {tick.time}")

    async def on_status(msg: str) -> None:
        print(f"[3/4] {msg}")
        if "연결" in msg and "끊김" not in msg:
            connected.set()

    async def on_fill(values: dict) -> None:
        fill = extract_fill(values)
        print(f"      체결통보: {fill if fill else values}")

    watcher = Watcher(auth.ws_url, auth.token, on_tick, on_status, on_fill=on_fill)
    await watcher.update_symbols([symbol])

    print("[3/4] WebSocket 접속·로그인 시도...")
    task = asyncio.create_task(watcher.run())
    try:
        await asyncio.wait_for(connected.wait(), timeout=15)
    except TimeoutError:
        sys.exit("WebSocket 연결 실패 — 네트워크/키 확인 필요")

    if do_buy1:  # 반드시 WS 등록 '후' 주문해야 체결통보를 놓치지 않는다
        order_no = broker.buy(symbol, 1)
        print(
            f"      [모의] 시장가 1주 매수 주문 접수: 주문번호 {order_no} → 체결통보 대기"
        )

    print(
        f"[4/4] {symbol} 실시간 수신 {_TIMEOUT}초 대기 (장중이 아니면 틱 없음이 정상)"
    )
    await asyncio.sleep(_TIMEOUT)
    await watcher.stop()
    task.cancel()
    print(
        f"\n완료: 틱 {received}건 수신. REST 조회와 WS 로그인·등록까지 성공했다면 검증 OK."
    )


if __name__ == "__main__":
    asyncio.run(main())
