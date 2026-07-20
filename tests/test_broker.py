"""broker 단위 테스트 — 네트워크 없이 요청 구성·응답 파싱·체결통보 해석 검증.

requests.post 를 monkeypatch 로 가로채 헤더(api-id, Bearer)·바디가 규격대로
만들어지는지, 응답 오류가 명확한 에러로 변환되는지 확인한다.
실제 접속 검증은 check_kiwoom.py 를 로컬에서 실행한다.
"""

import pytest

from trader.broker import Broker, BrokerError, Fill, extract_fill
from trader.kiwoom import KiwoomAuth


@pytest.fixture
def auth(monkeypatch):
    a = KiwoomAuth("app", "secret", mock=True)
    monkeypatch.setattr(a, "token", lambda: "TOKEN")
    return a


def _capture(monkeypatch, status=200, body=None):
    """requests.post 를 가로채 마지막 요청을 기록하고 지정 응답을 돌려준다."""
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, body=json)

        class R:
            status_code = status

            def json(self):
                return body or {}

        return R()

    monkeypatch.setattr("trader.broker.requests.post", fake_post)
    return captured


# ── 주문 ───────────────────────────────────────────────────────


def test_시장가_매수_요청_규격과_주문번호(auth, monkeypatch):
    req = _capture(monkeypatch, body={"return_code": 0, "ord_no": "0000138"})
    order_no = Broker(auth).buy("005930", 7)
    assert order_no == "0000138"
    assert req["url"] == "https://mockapi.kiwoom.com/api/dostk/ordr"
    assert req["headers"]["api-id"] == "kt10000"
    assert req["headers"]["authorization"] == "Bearer TOKEN"
    assert req["body"] == {
        "dmst_stex_tp": "KRX",
        "stk_cd": "005930",
        "ord_qty": "7",
        "ord_uv": "",
        "trde_tp": "3",
    }


def test_시장가_매도는_kt10001(auth, monkeypatch):
    req = _capture(monkeypatch, body={"return_code": 0, "ord_no": "0000139"})
    Broker(auth).sell("005930", 3)
    assert req["headers"]["api-id"] == "kt10001"


def test_수량_0이하_주문은_전송_전에_거부(auth, monkeypatch):
    req = _capture(monkeypatch)
    with pytest.raises(BrokerError, match="0 이하"):
        Broker(auth).buy("005930", 0)
    assert not req  # 요청 자체가 나가지 않음


def test_주문_거부는_명확한_에러(auth, monkeypatch):
    _capture(monkeypatch, body={"return_code": 8, "return_msg": "주문가능금액 부족"})
    with pytest.raises(BrokerError, match="주문가능금액 부족"):
        Broker(auth).buy("005930", 7)


# ── 계좌 / 종목 조회 ───────────────────────────────────────────


def test_주문가능금액_필드_후보_탐색(auth, monkeypatch):
    _capture(
        monkeypatch,
        body={"return_code": 0, "entr": "1000000", "ord_alow_amt": "950000"},
    )
    assert Broker(auth).deposit() == 950000  # 우선순위 높은 필드 사용


def test_실전처럼_우선_필드가_0이면_다음_후보의_실제_값_채택(auth, monkeypatch):
    _capture(
        monkeypatch,
        body={
            "return_code": 0,
            "ord_alow_amt": "000000000000",
            "100stk_ord_alow_amt": "0",
            "entr": "000000500000",
        },
    )
    assert Broker(auth).deposit() == 500000


def test_실측_해외주식_원화대용_계좌는_폴백_필드_채택(auth, monkeypatch):
    # 2026-07-21 실전 실측: 일반 현금 필드 전부 0, 대용 설정 필드에만 실제 금액
    _capture(
        monkeypatch,
        body={
            "return_code": 0,
            "entr": "000000000000000",
            "ord_alow_amt": "000000000000000",
            "fc_stk_krw_repl_set_amt": "000000001005766",
            "mdstrm_usfe": "000000000000145",
        },
    )
    assert Broker(auth).deposit() == 1_005_766


def test_전_후보가_0이면_잔고없음_0원(auth, monkeypatch):
    _capture(monkeypatch, body={"return_code": 0, "ord_alow_amt": "0", "entr": "0"})
    assert Broker(auth).deposit() == 0


def test_보유잔고는_A접두_제거하고_잔량만(auth, monkeypatch):
    _capture(
        monkeypatch,
        body={
            "return_code": 0,
            "acnt_evlt_remn_indv_tot": [
                {"stk_cd": "A005930", "rmnd_qty": "60"},
                {"stk_cd": "A035720", "rmnd_qty": "0"},  # 잔량 0 → 제외
            ],
        },
    )
    assert Broker(auth).holdings() == {"005930": 60}


def test_종목정보는_이름과_부호제거_현재가(auth, monkeypatch):
    _capture(
        monkeypatch, body={"return_code": 0, "stk_nm": "삼성전자", "cur_prc": "-72400"}
    )
    assert Broker(auth).stock_info("005930") == ("삼성전자", 72400)


# ── 체결통보(00) 해석 ──────────────────────────────────────────


def test_체결통보_해석():
    fill = extract_fill(
        {
            "9203": "0000138",
            "9001": "A005930",
            "913": "체결",
            "911": "7",
            "910": "-72400",
            "902": "0",
        }
    )
    assert fill == Fill("0000138", "005930", "체결", 7, 72400, 0)


def test_접수_통보는_체결량_0으로_해석():
    fill = extract_fill(
        {
            "9203": "0000138",
            "9001": "A005930",
            "913": "접수",
            "911": "",
            "910": "",
            "902": "7",
        }
    )
    assert fill.status == "접수" and fill.filled_qty == 0 and fill.unfilled_qty == 7


def test_해석_불가_통보는_None():
    assert extract_fill({"911": "abc"}) is None
