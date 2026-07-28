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
from datetime import datetime

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
    if "--deposit" in sys.argv:  # 예수금 응답 전 필드 확인 (어느 값이 주문가능금액인지)
        for qry_tp, label in (("2", "일반조회"), ("3", "추정조회")):
            try:
                data = broker.deposit_detail(qry_tp)
            except Exception as e:  # noqa: BLE001
                print(f"      [{label}] 실패 — {e}")
                continue
            items = [
                (k, v)
                for k, v in data.items()
                if isinstance(v, str)
                and v.strip().lstrip("+-").replace(".", "").isdigit()
                and float(v) != 0
            ]
            print(f"      [{label}] 0 아닌 숫자 필드 {len(items)}개")
            for k, v in items:
                print(f"        {k:<28} {float(v):>15,.0f}")

    if "--raw" in sys.argv:  # 응답 원본 확인 (캔들이 영웅문 통합차트와 다를 때 진단용)
        from trader.broker import _PATH_CHART, _TR_DAILY_CHART  # noqa: PLC0415

        # 두 번째 인자가 8자리 날짜면 그 날짜 전후를 보여준다 (기본: 최근 3봉)
        target = next((a for a in args[1:] if len(a) == 8 and a.isdigit()), "")
        # 거래소 범위 확인: 기본(KRX 추정) / _AL(통합) / _NX(대체거래소)
        # 영웅문 '통합키움차트' 는 KRX+NXT 합산이라 시가·종가·거래량이 KRX 단독과 다르다.
        for code, label in (
            (symbol, "기본"),
            (f"{symbol}_AL", "통합(_AL)"),
            (f"{symbol}_NX", "NXT(_NX)"),
        ):
            try:
                raw = broker._request(  # noqa: SLF001 — 진단 전용
                    _PATH_CHART,
                    _TR_DAILY_CHART,
                    {
                        "stk_cd": code,
                        "base_dt": datetime.now().strftime("%Y%m%d"),
                        "upd_stkpc_tp": "1",
                    },
                )
                rows = broker._first_list(raw)  # noqa: SLF001
            except Exception as e:  # noqa: BLE001
                print(f"      [{label}] 조회 실패 — {e}")
                continue
            if not rows:
                print(f"      [{label}] 0행 — 이 코드 형식은 지원되지 않음")
                continue
            if target:
                idx = next(
                    (i for i, r in enumerate(rows) if r.get("dt", "") <= target), 0
                )
                window = rows[max(0, idx - 2) : idx + 3]
                print(f"      [{label}] {len(rows)}행 · {target} 전후 {len(window)}봉")
            else:
                window = rows[:3]
                print(f"      [{label}] {len(rows)}행 · 최근 3봉")
            for row in window:
                print(
                    f"        {row.get('dt', '?')} "
                    f"시{row.get('open_pric', '?')} 고{row.get('high_pric', '?')} "
                    f"저{row.get('low_pric', '?')} 종{row.get('cur_prc', '?')} "
                    f"거래량{row.get('trde_qty', '?')}"
                )

    if "--chart" in sys.argv:  # 차트 TR 실측: 일봉/분봉/지수 응답 규격 확인
        for label, fetch in (
            ("일봉", lambda: broker.daily_chart(symbol)),
            ("3분봉", lambda: broker.minute_chart(symbol)),
            ("KOSPI", lambda: broker.index_daily()),
        ):
            try:
                bars = fetch()
            except Exception as e:  # noqa: BLE001
                print(f"      {label}: 실패 — {e}")
                continue
            if bars:
                print(
                    f"      {label}: {len(bars)}개 · 처음 {bars[0][0]} 종가 {bars[0][4]:,.0f}"
                    f" · 마지막 {bars[-1][0]} 종가 {bars[-1][4]:,.0f}"
                )
            else:
                print(
                    f"      {label}: 0개 — 응답 필드명이 후보와 다를 수 있음 (원본 확인 필요)"
                )

    deposit = broker.deposit()
    print(f"      주문가능금액: {deposit:,.0f}")
    if deposit == 0:  # 실전 필드 진단: 일반/추정 조회 각각 0이 아닌 필드를 보여준다
        for qry_tp, label in (("2", "일반조회"), ("3", "추정조회")):
            print(f"      [진단] 예수금 응답({label})의 0이 아닌 필드:")
            for k, v in broker.deposit_detail(qry_tp).items():
                try:
                    if float(v) != 0:
                        print(f"        {k} = {v}")
                except (TypeError, ValueError):
                    continue
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
