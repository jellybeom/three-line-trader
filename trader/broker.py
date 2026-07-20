"""키움 REST 주문·계좌 조회 (broker) + 체결통보(00) 파싱.

REST 호출 규격 (키움 공식 가이드 기준 — TR/필드는 개정될 수 있어 상수로 격리):
- POST {host}/api/dostk/{경로},  headers: authorization(Bearer), api-id(TR)
- 주문:   ordr    / kt10000(매수) kt10001(매도)  — 시장가(trde_tp='3'), ord_uv 빈값
- 계좌:   acnt    / kt00001(예수금상세현황) kt00018(계좌평가잔고내역)
- 종목:   stkinfo / ka10001(주식기본정보: 종목명·현재가)

체결통보는 WebSocket 실시간 타입 '00' 으로 수신된다 (watcher 가 등록·수신 후
broker 의 extract_fill 로 해석). 필드는 키움 FID 관례:
9203=주문번호, 9001=종목코드(A 접두), 913=주문상태, 911=누적체결량,
910=체결가, 902=미체결수량.

토큰이 계좌와 연결되어 있으므로 (키 발급 시 계좌 등록) 요청에 계좌번호는 없다.
모든 메서드는 동기(requests) — 코어의 asyncio 루프에서는 asyncio.to_thread 로 호출한다.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

from trader.kiwoom import KiwoomAuth

_PATH_ORDER = "/api/dostk/ordr"
_PATH_ACCOUNT = "/api/dostk/acnt"
_PATH_STOCK = "/api/dostk/stkinfo"

_TR_BUY = "kt10000"
_TR_SELL = "kt10001"
_TR_DEPOSIT = "kt00001"
_TR_HOLDINGS = "kt00018"
_TR_STOCK_INFO = "ka10001"

_MARKET_ORDER = "3"  # 매매구분: 시장가
_EXCHANGE = "KRX"


class BrokerError(RuntimeError):
    """주문/조회 실패 (거부, 필드 누락, 네트워크 등)."""


@dataclass(frozen=True)
class Fill:
    """체결통보(00) 해석 결과."""

    order_no: str
    symbol: str
    status: str  # 접수 / 체결 / 확인 등
    filled_qty: int  # 누적 체결량
    fill_price: float  # 체결가 (0 이면 미체결 통보)
    unfilled_qty: int  # 미체결 수량


def extract_fill(values: dict) -> Fill | None:
    """WebSocket '00' values → Fill. 해석 불가 항목은 None (호출부가 raw 로깅)."""
    try:
        symbol = values.get("9001", "").lstrip("A")
        return Fill(
            order_no=values.get("9203", ""),
            symbol=symbol,
            status=values.get("913", ""),
            filled_qty=int(values.get("911") or 0),
            fill_price=abs(float(values.get("910") or 0)),
            unfilled_qty=int(values.get("902") or 0),
        )
    except (ValueError, TypeError):
        return None


class Broker:
    """키움 REST 주문·조회. watcher 와 KiwoomAuth 하나를 공유한다."""

    def __init__(self, auth: KiwoomAuth):
        self._auth = auth

    # ── 주문 (시장가) ───────────────────────────────────────────

    def buy(self, symbol: str, qty: int) -> str:
        """시장가 매수. 성공 시 주문번호 반환 — 체결 확정은 체결통보(00)로."""
        return self._order(_TR_BUY, symbol, qty)

    def sell(self, symbol: str, qty: int) -> str:
        """시장가 매도. 성공 시 주문번호 반환."""
        return self._order(_TR_SELL, symbol, qty)

    def _order(self, tr: str, symbol: str, qty: int) -> str:
        if qty <= 0:
            raise BrokerError(f"주문 수량이 0 이하: {symbol} {qty}주")
        data = self._request(
            _PATH_ORDER,
            tr,
            {
                "dmst_stex_tp": _EXCHANGE,
                "stk_cd": symbol,
                "ord_qty": str(qty),
                "ord_uv": "",  # 시장가는 가격 없음
                "trde_tp": _MARKET_ORDER,
            },
        )
        order_no = data.get("ord_no", "")
        if not order_no:
            raise BrokerError(f"주문번호 없음: {data}")
        return order_no

    # ── 계좌 조회 ───────────────────────────────────────────────

    # 주문가능금액 후보 (우선순위 순). 모의/실전 서버가 채우는 필드가 달라
    # "존재 여부"가 아니라 "0이 아닌 첫 값"을 채택한다 — 실전에서 ord_alow_amt 가
    # 0으로 오고 실제 금액이 다른 필드에 있는 경우를 실측했다.
    # fc_stk_krw_repl_set_amt: 해외주식 원화주문(통합증거금) 서비스 계좌에서
    # 원화 예수금이 대용 설정되어 이 필드에만 잡히는 사례 실측 (2026-07-21).
    _DEPOSIT_KEYS = (
        "ord_alow_amt",
        "100stk_ord_alow_amt",
        "entr",
        "d2_entra",
        "wthd_alow_amt",
        "fc_stk_krw_repl_set_amt",
    )

    def deposit(self) -> float:
        """주문가능금액 (예수금 방어의 기준). 일반조회 → 추정조회 순으로 시도한다."""
        found_any = False
        for qry_tp in ("2", "3"):
            data = self.deposit_detail(qry_tp)
            for key in self._DEPOSIT_KEYS:
                raw = data.get(key)
                if raw in (None, ""):
                    continue
                try:
                    value = float(raw)
                except (ValueError, TypeError):
                    continue
                found_any = True
                if value > 0:
                    return value
        if found_any:  # 전 후보가 0 — 실제 잔고 없음
            return 0.0
        raise BrokerError(
            "주문가능금액 필드를 찾지 못함 (kt00001 응답 필드 변경 가능성)"
        )

    def deposit_detail(self, qry_tp: str = "2") -> dict:
        """예수금상세현황(kt00001) 원본 응답 — 필드 진단용. qry_tp 2=일반, 3=추정."""
        return self._request(_PATH_ACCOUNT, _TR_DEPOSIT, {"qry_tp": qry_tp})

    def holdings(self) -> dict[str, int]:
        """계좌 실제 보유 수량 {종목코드: 잔량} — 시작 시 reconcile 용."""
        data = self._request(
            _PATH_ACCOUNT, _TR_HOLDINGS, {"qry_tp": "1", "dmst_stex_tp": _EXCHANGE}
        )
        result: dict[str, int] = {}
        for row in data.get("acnt_evlt_remn_indv_tot", []):
            symbol = row.get("stk_cd", "").lstrip("A")
            qty = int(row.get("rmnd_qty") or 0)
            if symbol and qty > 0:
                result[symbol] = qty
        return result

    # ── 종목 정보 ───────────────────────────────────────────────

    def stock_info(self, symbol: str) -> tuple[str, float]:
        """(종목명, 현재가). 등록 창 자동 조회 및 재연결 후 가격 보정용."""
        data = self._request(_PATH_STOCK, _TR_STOCK_INFO, {"stk_cd": symbol})
        name = data.get("stk_nm", "")
        try:
            price = abs(float(data.get("cur_prc") or 0))  # 등락 부호 제거
        except (ValueError, TypeError):
            price = 0.0
        if not name:
            raise BrokerError(f"종목 정보 없음: {symbol} → {data}")
        return name, price

    # ── 내부 ────────────────────────────────────────────────────

    def _request(self, path: str, api_id: str, body: dict) -> dict:
        resp = requests.post(
            f"{self._auth.host}{path}",
            headers={
                "Content-Type": "application/json;charset=UTF-8",
                "authorization": f"Bearer {self._auth.token()}",
                "api-id": api_id,
                "cont-yn": "N",
                "next-key": "",
            },
            json=body,
            timeout=10,
        )
        try:
            data = resp.json()
        except ValueError as e:
            raise BrokerError(
                f"{api_id} 응답이 JSON 이 아님 (HTTP {resp.status_code})"
            ) from e
        if resp.status_code != 200 or data.get("return_code", 0) != 0:
            raise BrokerError(
                f"{api_id} 실패 (HTTP {resp.status_code}, code {data.get('return_code')}): "
                f"{data.get('return_msg', data)}"
            )
        return data
