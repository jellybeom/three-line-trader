"""watcher / kiwoom 단위 테스트 — 네트워크 없이 검증 가능한 부분.

메시지 파싱(순수 함수)과 토큰 캐시·재발급 로직을 검증한다.
실제 접속 검증은 check_kiwoom.py 를 로컬에서 직접 실행한다.
"""

import json
from datetime import datetime, timedelta

import pytest

from trader.kiwoom import KiwoomAuth, KiwoomAuthError
from trader.watcher import Tick, extract_ticks, parse_message

# ── 메시지 파싱 ────────────────────────────────────────────────


def test_핑은_그대로_되돌릴_수_있게_식별():
    kind, payload = parse_message('{"trnm": "PING"}')
    assert kind == "ping" and payload == {"trnm": "PING"}


def test_로그인_성공과_거부_식별():
    assert parse_message('{"trnm": "LOGIN", "return_code": 0}') == ("login", True)
    kind, ok = parse_message('{"trnm": "LOGIN", "return_code": 1, "return_msg": "err"}')
    assert kind == "login" and ok is False


def test_체결_메시지에서_틱_추출_및_등락부호_제거():
    raw = json.dumps(
        {
            "trnm": "REAL",
            "data": [
                {
                    "type": "0B",
                    "item": "005930",
                    "values": {"10": "-72400", "20": "140233"},
                },
                {
                    "type": "0B",
                    "item": "000660",
                    "values": {"10": "+198500", "20": "140233"},
                },
            ],
        }
    )
    kind, ticks = parse_message(raw)
    assert kind == "real"
    assert ticks == [Tick("005930", 72400, "140233"), Tick("000660", 198500, "140233")]


def test_0B가_아닌_타입과_이상_항목은_건너뜀():
    ticks = extract_ticks(
        [
            {"type": "0D", "item": "005930", "values": {"10": "-72400"}},  # 호가 → 무시
            {"type": "0B", "item": "005930", "values": {}},  # 현재가 없음 → 무시
            {
                "type": "0B",
                "item": "005930",
                "values": {"10": "abc"},
            },  # 숫자 아님 → 무시
            {
                "type": "0B",
                "item": "035720",
                "values": {"10": "41150"},
            },  # 부호 없음도 정상
        ]
    )
    assert ticks == [Tick("035720", 41150, "")]


def test_JSON이_아닌_원문은_other로_안전_처리():
    assert parse_message("not-json")[0] == "other"


# ── 토큰 캐시 / 재발급 ─────────────────────────────────────────


def _fake_response(status: int, body: dict):
    class R:
        status_code = status

        def json(self):
            return body

    return R()


def test_토큰은_캐시되고_만료_임박시_재발급(monkeypatch):
    calls = []
    expires = (datetime.now() + timedelta(hours=24)).strftime("%Y%m%d%H%M%S")

    def fake_post(url, json=None, timeout=None):
        calls.append(url)
        return _fake_response(
            200, {"return_code": 0, "token": f"T{len(calls)}", "expires_dt": expires}
        )

    monkeypatch.setattr("trader.kiwoom.requests.post", fake_post)
    auth = KiwoomAuth("app", "secret", mock=True)
    assert auth.token() == "T1"
    assert auth.token() == "T1"  # 캐시 사용 — 재요청 없음
    assert len(calls) == 1
    assert "mockapi" in calls[0]  # 모의투자 도메인

    auth._expires_at = datetime.now() + timedelta(minutes=5)  # 만료 임박 재현
    assert auth.token() == "T2"  # 자동 재발급


def test_토큰_발급_실패는_명확한_에러(monkeypatch):
    monkeypatch.setattr(
        "trader.kiwoom.requests.post",
        lambda *a, **k: _fake_response(
            401, {"return_code": 3, "return_msg": "invalid key"}
        ),
    )
    with pytest.raises(KiwoomAuthError, match="401"):
        KiwoomAuth("bad", "key").token()


def test_실전_모드는_실전_도메인(monkeypatch):
    urls = []
    monkeypatch.setattr(
        "trader.kiwoom.requests.post",
        lambda url, **k: (urls.append(url), _fake_response(200, {"token": "T"}))[1],
    )
    KiwoomAuth("app", "secret", mock=False).token()
    assert urls[0].startswith("https://api.kiwoom.com")
