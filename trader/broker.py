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

import threading
import time
from dataclasses import dataclass

import requests

from trader.kiwoom import KiwoomAuth

_PATH_ORDER = "/api/dostk/ordr"
_PATH_ACCOUNT = "/api/dostk/acnt"
_PATH_CHART = "/api/dostk/chart"
_PATH_STOCK = "/api/dostk/stkinfo"

_TR_BUY = "kt10000"
_TR_SELL = "kt10001"
_TR_DEPOSIT = "kt00001"
_TR_HOLDINGS = "kt00018"
_TR_OPEN_ORDERS = "ka10075"  # 미체결요청 — 중복 주문 방지
_TR_FILLS = "ka10076"  # 체결요청 — 당일 체결 내역 (증권사 실측 수수료·세금)
_TR_REALIZED = "ka10077"  # 당일실현손익상세 — '오늘' 의 정의가 분명해 대조에 쓴다
_TR_STOCK_INFO = "ka10001"
_TR_DAILY_CHART = "ka10081"  # 주식 일봉차트
_TR_MINUTE_CHART = "ka10080"  # 주식 분봉차트
_TR_INDEX_DAILY = "ka20006"  # 업종(지수) 일봉 — KOSPI 는 업종코드 001
_CHART_SUFFIX = "_AL"  # 차트 조회용 통합(KRX+NXT) 종목코드 접미사

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
    """키움 REST 주문·조회. watcher 와 KiwoomAuth 하나를 공유한다.

    키움 REST 는 초당 호출 수 제한이 있어, 연속 호출(재연결 시 전 종목 시세 보정 등)은
    최소 간격을 두고 순서대로 내보낸다. 실측: 22종목을 간격 없이 조회하자 12건 성공 후
    10건이 거부됨 (2026-07-24 14:00). 조회는 실패 시 재시도하되, **주문은 재시도하지
    않는다** — 응답만 유실되고 주문은 접수됐을 수 있어 중복 주문 위험이 있기 때문이다.
    """

    _MIN_INTERVAL = 0.25  # REST 호출 최소 간격 (초) — 초당 4회
    _QUERY_RETRIES = 2  # 조회 전용 재시도 횟수

    def __init__(self, auth: KiwoomAuth):
        self._auth = auth
        self._lock = threading.Lock()  # 여러 스레드(to_thread)의 호출 간격 보장
        self._last_call = 0.0
        self._deposit_hint: tuple[str, str] | None = (
            None  # (qry_tp, key) 성공 조합 기억
        )

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
    # 실측(2026-07-28) 기준 의미:
    #   100stk_ord_alow_amt  증거금 100% 종목 주문가능금액 = 현금 + 당일 매도대금 재사용분
    #   ord_alow_amt         주문가능현금 (매도대금 재사용분 제외) — 너무 보수적이라 후순위
    #   entr                 예수금 총액 (증거금 포함) — 과대평가라 최후 수단
    # 미수를 쓰지 않는 전략이므로 '증거금 100% 종목' 기준이 우리에게 맞는 상한이다.
    _DEPOSIT_KEYS = (
        "100stk_ord_alow_amt",
        "ord_alow_amt",
        "pymn_alow_amt",
        "d2_entra",
        "entr",
        "wthd_alow_amt",
        "fc_stk_krw_repl_set_amt",
    )

    def deposit(self) -> float:
        """주문가능금액 (예수금 방어의 기준). 일반조회 → 추정조회 순으로 시도한다.

        한 번 성공한 (조회구분, 필드) 조합을 기억해 다음부터는 REST 호출 1회로 끝낸다
        (계좌 유형마다 값이 실리는 필드가 달라 탐색이 필요하지만, 매번 할 필요는 없다).
        """
        if self._deposit_hint:
            qry_tp, key = self._deposit_hint
            value = self._extract_deposit(self.deposit_detail(qry_tp), (key,))
            if value:
                return value
            self._deposit_hint = None  # 힌트가 더 이상 맞지 않으면 다시 탐색

        found_any = False
        for qry_tp in ("2", "3"):
            data = self.deposit_detail(qry_tp)
            for key in self._DEPOSIT_KEYS:
                value = self._extract_deposit(data, (key,))
                if value is None:
                    continue
                found_any = True
                if value > 0:
                    self._deposit_hint = (qry_tp, key)
                    return value
        if found_any:  # 전 후보가 0 — 실제 잔고 없음
            return 0.0
        raise BrokerError(
            "주문가능금액 필드를 찾지 못함 (kt00001 응답 필드 변경 가능성)"
        )

    @staticmethod
    def _extract_deposit(data: dict, keys: tuple[str, ...]) -> float | None:
        for key in keys:
            raw = data.get(key)
            if raw in (None, ""):
                continue
            try:
                return float(raw)
            except (ValueError, TypeError):
                continue
        return None

    # ── 차트 조회 (복기 차트용) ──────────────────────────────
    # 응답의 봉 목록 키와 필드명은 TR 마다 달라 후보를 순서대로 탐색한다.
    # 가격에 붙는 +/- 부호는 등락 표시이므로 절댓값으로 파싱한다.

    _DATE_KEYS = ("dt", "date", "stck_bsop_date", "base_dt")
    _TIME_KEYS = ("cntr_tm", "tm", "cntg_tm", "stck_cntg_hour")
    _OPEN_KEYS = (
        "open_pric",
        "opn_prc",
        "opnprc",
        "open_prc",
        "stck_oprc",
        "ovrs_nmix_oprc",
        "strt_prc",
        "bsis_prc",
    )
    _HIGH_KEYS = ("high_pric", "hgprc", "hg_pric", "stck_hgpr")
    _LOW_KEYS = ("low_pric", "lwprc", "lw_pric", "stck_lwpr")
    _CLOSE_KEYS = ("cur_prc", "clsprc", "cls_pric", "close_pric", "stck_clpr")
    _VOL_KEYS = ("trde_qty", "trqu", "cntg_vol", "acml_vol", "trde_vol")
    _VALUE_KEYS = ("trde_prica", "trde_amt", "tr_prica", "acml_tr_pbmn")

    @staticmethod
    def _first_list(data: dict) -> list[dict]:
        for value in data.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
        return []

    @staticmethod
    def _num(row: dict, keys: tuple[str, ...]) -> float | None:
        for key in keys:
            raw = row.get(key)
            if raw in (None, ""):
                continue
            try:
                return abs(float(str(raw).replace(",", "")))
            except (ValueError, TypeError):
                continue
        return None

    @classmethod
    def _pick_open(cls, row: dict, low: float, high: float, close: float) -> float:
        """시가 선택 — 후보 필드 중 **저가~고가 범위 안에 드는 첫 값**을 쓴다.

        TR 마다 시가 필드명이 다르고, 이름이 비슷한 다른 값(기준가·전일종가 등)을 잘못
        읽으면 캔들이 갭으로 시작하거나 양봉/음봉이 뒤집힌다(2026-07-27 실측: 종가·고가·
        저가는 영웅문과 일치하는데 시가만 어긋남). 범위 검사가 그런 오독을 걸러낸다.
        후보가 모두 범위 밖이면 종가로 대체해 도지로 그린다(잘못된 몸통보다 안전).
        """
        for key in cls._OPEN_KEYS:
            value = cls._num(row, (key,))
            if value is not None and low <= value <= high:
                return value
        return close

    @classmethod
    def _text(cls, row: dict, keys: tuple[str, ...]) -> str:
        for key in keys:
            raw = row.get(key)
            if raw not in (None, ""):
                return str(raw).strip()
        return ""

    def _parse_bars(self, data: dict, minute: bool) -> list[tuple]:
        """(key, open, high, low, close, volume, value) 목록 — key 오름차순.

        key: 일봉 YYYYMMDD, 분봉 YYYYMMDDHHMMSS. 필드가 비거나 종가 0 인 행은 건너뛴다.
        """
        bars = []
        for row in self._first_list(data):
            close = self._num(row, self._CLOSE_KEYS)
            if not close:
                continue
            h = self._num(row, self._HIGH_KEYS) or close
            low = self._num(row, self._LOW_KEYS) or close
            o = self._pick_open(row, low, h, close)
            h, low = max(h, o, close), min(
                low, o, close
            )  # 캔들이 깨지지 않게 최종 보정
            vol = self._num(row, self._VOL_KEYS) or 0.0
            value = self._num(row, self._VALUE_KEYS) or 0.0
            if minute:
                t = self._text(row, self._TIME_KEYS)
                if len(t) == 6:  # 시각만 오면 날짜 필드와 결합
                    t = self._text(row, self._DATE_KEYS)[:8] + t
                key = t[:14]
            else:
                key = self._text(row, self._DATE_KEYS)[:8]
            if len(key) < 8:
                continue
            bars.append((key, o, h, low, close, vol, value))
        bars.sort(key=lambda b: b[0])
        return bars

    @staticmethod
    def _calibrate_value(bars: list[tuple]) -> list[tuple]:
        """거래대금 단위 자동 보정 → 원.

        TR 마다 거래대금 단위가 다를 수 있다(원/천원/백만원 — 영웅문 표기는 백만원).
        각 봉의 종가×거래량은 거래대금의 근사치(2배 이내)이므로, 응답값과의 비율을
        10ⁿ 으로 반올림해 스케일을 맞춘다. 단위가 이미 원이면 n=0 이라 그대로다.
        """
        import math
        import statistics

        ratios = [
            math.log10((c * v) / val)
            for _, _, _, _, c, v, val in bars[-20:]
            if v > 0 and val > 0 and c > 0
        ]
        if not ratios:
            return bars
        n = round(statistics.median(ratios))
        if n == 0:
            return bars
        factor = 10.0**n
        return [(k, o, h, low, c, v, val * factor) for k, o, h, low, c, v, val in bars]

    def _chart_request(
        self, api_id: str, symbol: str, body: dict, minute: bool
    ) -> list[tuple]:
        """차트 조회 — **통합(KRX+NXT) 코드를 먼저** 시도하고, 비면 원래 코드로 되돌린다.

        영웅문 '통합키움차트' 는 KRX+NXT 합산 시세라, 접미사 없는 코드로 받으면 시가·종가·
        거래량이 HTS 화면과 다르다(2026-07-27 실측: 시가 0/7 일치, 거래량 2.4배 차이).
        `_AL` 을 붙이면 5일 × 5항목이 HTS 와 정확히 일치한다. NXT 미상장 종목 등
        접미사가 통하지 않는 경우를 대비해 결과가 비면 원래 코드로 한 번 더 조회한다.
        """
        for code in (f"{symbol}{_CHART_SUFFIX}", symbol):
            data = self._request(
                _PATH_CHART,
                api_id,
                {**body, "stk_cd": code},
                retries=self._QUERY_RETRIES,
            )
            bars = self._parse_bars(data, minute=minute)
            if bars:
                return bars
        return []

    def daily_chart(self, symbol: str, count: int = 180) -> list[tuple]:
        """일봉 (최근 count 개, 오름차순). 이동평균 워밍업을 위해 표시분보다 길게 요청한다."""
        from datetime import date as _date

        bars = self._chart_request(
            _TR_DAILY_CHART,
            symbol,
            {"base_dt": _date.today().strftime("%Y%m%d"), "upd_stkpc_tp": "1"},
            minute=False,
        )
        return self._calibrate_value(bars[-count:])

    def minute_chart(self, symbol: str, interval: int = 3) -> list[tuple]:
        """분봉 (서버가 주는 최대 분량, 오름차순).

        통합 코드라 08:00 프리마켓 ~ 20:00 애프터마켓 봉까지 포함된다.
        """
        return self._chart_request(
            _TR_MINUTE_CHART,
            symbol,
            {"tic_scope": str(interval), "upd_stkpc_tp": "1"},
            minute=True,
        )

    def index_daily(self, code: str = "001", count: int = 180) -> list[tuple]:
        """업종(지수) 일봉 — KOSPI=001. TR 규격이 다르면 BrokerError 로 실패한다
        (복기 차트는 이 실패를 치명으로 보지 않고 KOSPI 패널만 생략한다).

        지수는 소수 2자리를 정수로 준다 (실측 2026-07-24: 응답 669062 = 지수 6,690.62)
        → 가격 필드만 ÷100 보정한다.
        """
        from datetime import date as _date

        data = self._request(
            _PATH_CHART,
            _TR_INDEX_DAILY,
            {"inds_cd": code, "base_dt": _date.today().strftime("%Y%m%d")},
            retries=self._QUERY_RETRIES,
        )
        bars = self._parse_bars(data, minute=False)[-count:]
        return [
            (k, o / 100, h / 100, low / 100, c / 100, v, val)
            for k, o, h, low, c, v, val in bars
        ]

    def deposit_detail(self, qry_tp: str = "2") -> dict:
        """예수금상세현황(kt00001) 원본 응답 — 필드 진단용. qry_tp 2=일반, 3=추정."""
        return self._request(
            _PATH_ACCOUNT, _TR_DEPOSIT, {"qry_tp": qry_tp}, retries=self._QUERY_RETRIES
        )

    _SELLABLE_KEYS = ("trde_able_qty", "sell_able_qty", "ord_able_qty", "rmnd_qty")

    def holdings(self) -> dict[str, int]:
        """계좌 실제 보유 수량 {종목코드: 잔량} — 시작 시 reconcile 용."""
        return {sym: qty for sym, (qty, _) in self.holdings_detail().items()}

    def account_summary(self) -> dict[str, float]:
        """계좌 전체 요약 — 매입·평가·평가손익·수익률·추정자산.

        실측(2026-08-01)으로 영웅문 [국내잔고] 화면과 값이 정확히 일치함을 확인했다.
        추정자산에는 대용금이 포함되지 않는다(대용금은 보유 주식을 담보로 환산한 값이라
        더하면 이중 계상이 된다).
        """
        data = self._request(
            _PATH_ACCOUNT,
            _TR_HOLDINGS,
            {"qry_tp": "1", "dmst_stex_tp": _EXCHANGE},
            retries=self._QUERY_RETRIES,
        )
        keys = {
            "purchase": "tot_pur_amt",
            "value": "tot_evlt_amt",
            "pnl": "tot_evlt_pl",
            "rate": "tot_prft_rt",
            "asset": "prsm_dpst_aset_amt",
        }
        result: dict[str, float] = {}
        for name, key in keys.items():
            raw = data.get(key)
            if raw in (None, ""):
                continue
            try:
                result[name] = float(str(raw).replace(",", ""))
            except (ValueError, TypeError):
                continue
        return result

    def holdings_detail(self) -> dict[str, tuple[int, int]]:
        """{종목코드: (보유수량, 매도가능수량)}.

        보유수량과 매도가능수량은 다르다 — 매도 주문이 접수·체결된 직후에는 보유수량이
        아직 10주로 보이는데 매도가능수량은 0 이다. 이 둘을 구분하지 않으면, 체결통보가
        유실됐을 때 '아직 10주 있다' 고 오판해 같은 물량을 또 팔려 한다
        (2026-07-30 실측: "매도가능수량이 부족합니다. 0주 매도가능" 3회 반복).
        """
        data = self._request(
            _PATH_ACCOUNT,
            _TR_HOLDINGS,
            {"qry_tp": "1", "dmst_stex_tp": _EXCHANGE},
            retries=self._QUERY_RETRIES,
        )
        result: dict[str, tuple[int, int]] = {}
        for row in data.get("acnt_evlt_remn_indv_tot", []):
            symbol = row.get("stk_cd", "").lstrip("A")
            qty = int(row.get("rmnd_qty") or 0)
            if not symbol or qty <= 0:
                continue
            sellable = qty
            for (
                key
            ) in self._SELLABLE_KEYS:  # 계좌 유형마다 필드명이 달라 후보를 순서대로
                raw = row.get(key)
                if raw not in (None, ""):
                    try:
                        sellable = int(float(raw))
                    except (ValueError, TypeError):
                        continue
                    break
            result[symbol] = (qty, min(sellable, qty))
        return result

    # ── 체결·미체결·실현손익 조회 (대조용) ─────────────────────
    # 셋 다 **읽기 전용**이다. 여기서 얻은 값으로 포지션을 고치지 않는다 —
    # 프로그램이 아는 것과 증권사가 아는 것이 다르면 사람이 봐야 할 일이지,
    # 자동으로 맞추면 어느 쪽이 맞는지 모르는 채로 덮어쓰게 된다.

    def open_orders(self, symbol: str = "") -> list[dict]:
        """미체결 주문 (ka10075). symbol 을 주면 그 종목만.

        전부 시장가로 매매하므로 평시에는 늘 비어 있다. **비어 있지 않다는 것 자체가
        신호다** — 주문이 아직 처리 중이거나, 프로그램이 모르는 주문이 계좌에 남아 있다는
        뜻이다. 중복 주문을 막는 근거로 쓴다.
        """
        data = self._request(
            _PATH_ACCOUNT,
            _TR_OPEN_ORDERS,
            {
                "all_stk_tp": "1" if symbol else "0",
                "trde_tp": "0",  # 매도·매수 전체
                "stk_cd": symbol,
                "stex_tp": "0",  # 통합 (KRX + NXT)
            },
            retries=self._QUERY_RETRIES,
        )
        rows = []
        for row in data.get("oso", []) or []:
            unfilled = int(float(row.get("oso_qty") or 0))
            if unfilled <= 0:
                continue
            rows.append(
                {
                    "order_no": str(row.get("ord_no") or ""),
                    "symbol": str(row.get("stk_cd") or "").lstrip("A"),
                    "name": row.get("stk_nm") or "",
                    # io_tp_nm 은 '+매수' / '-매도' 처럼 부호가 붙어 온다
                    "side": "매수" if "매수" in (row.get("io_tp_nm") or "") else "매도",
                    "qty": int(float(row.get("ord_qty") or 0)),
                    "unfilled": unfilled,
                    "status": row.get("ord_stt") or "",
                    "time": row.get("tm") or "",
                }
            )
        return rows

    def filled_orders(self, symbol: str = "") -> list[dict]:
        """당일 체결 내역 (ka10076) — 실제 체결가·수수료·세금.

        ⚠️ 응답에 **날짜 필드가 없다** (ord_tm 은 HHMMSS 뿐). 당일 조회 전용이며
        이월 종목의 과거 체결 복원에는 쓸 수 없다 — 그건 realized_pnl(ka10072) 몫이다.
        """
        data = self._request(
            _PATH_ACCOUNT,
            _TR_FILLS,
            {
                "stk_cd": symbol,
                "qry_tp": "1" if symbol else "0",
                "sell_tp": "0",  # 매도·매수 전체
                "ord_no": "",
                "stex_tp": "0",
            },
            retries=self._QUERY_RETRIES,
        )
        rows = []
        for row in data.get("cntr", []) or []:
            filled = int(float(row.get("cntr_qty") or 0))
            if filled <= 0:
                continue  # 접수만 되고 체결이 없는 행
            rows.append(
                {
                    "order_no": str(row.get("ord_no") or ""),
                    "symbol": str(row.get("stk_cd") or "").lstrip("A"),
                    "name": row.get("stk_nm") or "",
                    "side": "매수" if "매수" in (row.get("io_tp_nm") or "") else "매도",
                    "qty": filled,
                    "price": abs(float(row.get("cntr_pric") or 0)),
                    "commission": abs(float(row.get("tdy_trde_cmsn") or 0)),
                    "tax": abs(float(row.get("tdy_trde_tax") or 0)),
                    "status": row.get("ord_stt") or "",
                    "time": row.get("ord_tm") or "",
                }
            )
        return rows

    def realized_pnl(self, symbol: str = "") -> list[dict]:
        """**당일** 실현손익 상세 (ka10077).

        예전에는 ka10072(일자별)를 썼는데 응답에 일자 필드가 없어 "그날 하루" 인지
        "그날 이후" 인지 확인할 수 없었다. ka10077 은 이름 그대로 당일치만 주므로
        '오늘 얼마 벌었나' 를 대조하기에 맞다(2026-08-19 교체).

        ⚠️ stk_cd 에 'A' 접두사가 붙는다 (`A005930`). 다른 TR 과 달라 반드시 벗겨야 한다.
        ⚠️ 매입단가·손익이 **소수 문자열**("97602.9573459")이라 int() 파싱은 예외가 난다.

        문서 예시로 확인한 계산식: tdy_sel_pl = (체결가 − 매입단가) × 수량 − 수수료 − 세금.
        즉 **세후 순손익**이고, pl_rt 는 매입금액 대비 수익률이다.
        """
        data = self._request(
            _PATH_ACCOUNT,
            _TR_REALIZED,
            {"stk_cd": symbol},
            retries=self._QUERY_RETRIES,
        )
        rows = []
        for row in data.get("tdy_rlzt_pl_dtl", []) or []:
            code = str(row.get("stk_cd") or "").lstrip("A")
            if not code:
                continue
            rows.append(
                {
                    "symbol": code,
                    "name": row.get("stk_nm") or "",
                    "qty": int(float(row.get("cntr_qty") or 0)),
                    "buy_price": float(row.get("buy_uv") or 0),
                    "sell_price": abs(float(row.get("cntr_pric") or 0)),
                    "pnl": float(row.get("tdy_sel_pl") or 0),  # 세후 순손익
                    "rate": float(str(row.get("pl_rt") or 0).replace("+", "")) / 100,
                    "commission": abs(float(row.get("tdy_trde_cmsn") or 0)),
                    "tax": abs(float(row.get("tdy_trde_tax") or 0)),
                }
            )
        return rows

    # ── 종목 정보 ───────────────────────────────────────────────

    def stock_info(self, symbol: str) -> tuple[str, float]:
        """(종목명, 현재가). 등록 창 자동 조회 및 재연결 후 가격 보정용."""
        data = self._request(
            _PATH_STOCK, _TR_STOCK_INFO, {"stk_cd": symbol}, retries=self._QUERY_RETRIES
        )
        name = data.get("stk_nm", "")
        try:
            price = abs(float(data.get("cur_prc") or 0))  # 등락 부호 제거
        except (ValueError, TypeError):
            price = 0.0
        if not name:
            raise BrokerError(f"종목 정보 없음: {symbol} → {data}")
        return name, price

    # ── 내부 ────────────────────────────────────────────────────

    def _request(self, path: str, api_id: str, body: dict, retries: int = 0) -> dict:
        """REST 호출 (레이트 리밋 적용). retries 는 조회 전용 — 주문에는 쓰지 않는다."""
        last_error: BrokerError | None = None
        for attempt in range(retries + 1):
            try:
                return self._request_once(path, api_id, body)
            except BrokerError as e:
                last_error = e
                if attempt < retries:
                    time.sleep(0.5 * (attempt + 1))  # 한도 초과 완화용 백오프
        raise last_error

    def _throttle(self) -> None:
        now = time.monotonic()
        wait = self._last_call + self._MIN_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _request_once(self, path: str, api_id: str, body: dict) -> dict:
        with self._lock:
            self._throttle()
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
