"""관심종목 CSV 파서 테스트 — 영웅문 내보내기 형식의 변형들에 관대해야 한다."""

import pytest

from trader.ui.app import parse_watchlist_csv


def _write(tmp_path, content: str, encoding: str = "cp949"):
    p = tmp_path / "watch.csv"
    p.write_bytes(content.encode(encoding))
    return str(p)


def test_영웅문_실물_형식_헤더와_따옴표_접두(tmp_path):
    content = (
        "분,신,종목명,현재가,등락률,L일봉H,거래대금,메모,종목코드\n"
        '신,,SK이노베이션,"120,300","2.82",119000 126700 116500 120300,"353,501",급등주,\'096770\n'
        '신,,모나리자,"2,170","29.94",1760 2170 1760 2170,"26,381",,\'012690\n'
    )
    path = _write(tmp_path, content)
    assert parse_watchlist_csv(path) == [
        ("096770", "SK이노베이션", "급등주", None),
        ("012690", "모나리자", "", None),
    ]


def test_사용자가_1_2_3선_열을_채우면_가격까지_읽는다(tmp_path):
    content = (
        "종목명,종목코드,메모,1선,2선,3선\n"
        '삼성전자,005930,대형주,"70,000","68,000","66,000"\n'
        "모나리자,012690,,,,\n"  # 3선 비어있음 → None
    )
    path = _write(tmp_path, content)
    assert parse_watchlist_csv(path) == [
        ("005930", "삼성전자", "대형주", (70000.0, 68000.0, 66000.0)),
        ("012690", "모나리자", "", None),
    ]


def test_기본_형식_코드와_종목명(tmp_path):
    path = _write(tmp_path, "005930,삼성전자,72400\n000660,SK하이닉스,198500\n")
    assert parse_watchlist_csv(path) == [
        ("005930", "삼성전자", "", None),
        ("000660", "SK하이닉스", "", None),
    ]


def test_A접두_코드와_헤더행_및_중복_처리(tmp_path):
    path = _write(
        tmp_path,
        "종목코드,종목명,현재가\nA005930,삼성전자,72400\nA005930,삼성전자,72400\n",
    )
    assert parse_watchlist_csv(path) == [("005930", "삼성전자", "", None)]


def test_열_순서가_달라도_인식(tmp_path):
    path = _write(tmp_path, "삼성전자,005930,+2.5%\n")
    assert parse_watchlist_csv(path) == [("005930", "삼성전자", "", None)]


def test_utf8_인코딩_폴백(tmp_path):
    path = _write(tmp_path, "005930,삼성전자\n", encoding="utf-8-sig")
    assert parse_watchlist_csv(path) == [("005930", "삼성전자", "", None)]


def test_종목명이_없으면_코드로_대체(tmp_path):
    path = _write(tmp_path, "005930,72400,+1.2%\n")
    assert parse_watchlist_csv(path) == [("005930", "005930", "", None)]


def test_코드가_없는_행은_무시(tmp_path):
    path = _write(tmp_path, "관심종목 목록\n\n005930,삼성전자\n합계,3종목\n")
    assert parse_watchlist_csv(path) == [("005930", "삼성전자", "", None)]
