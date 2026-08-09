"""관심종목 CSV 파서 테스트 — 영웅문 내보내기 형식의 변형들에 관대해야 한다."""

import pytest

from trader.ui.app import parse_watchlist_csv


def head4(path):
    """앞 4개 필드(코드·이름·메모·3선)만 비교 — 태그·기준봉은 별도 테스트에서 다룬다."""
    return [row[:4] for row in parse_watchlist_csv(path)]


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
    assert head4(path) == [
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
    assert head4(path) == [
        ("005930", "삼성전자", "대형주", (70000.0, 68000.0, 66000.0)),
        ("012690", "모나리자", "", None),
    ]


def test_기본_형식_코드와_종목명(tmp_path):
    path = _write(tmp_path, "005930,삼성전자,72400\n000660,SK하이닉스,198500\n")
    assert head4(path) == [
        ("005930", "삼성전자", "", None),
        ("000660", "SK하이닉스", "", None),
    ]


def test_A접두_코드와_헤더행_및_중복_처리(tmp_path):
    path = _write(
        tmp_path,
        "종목코드,종목명,현재가\nA005930,삼성전자,72400\nA005930,삼성전자,72400\n",
    )
    assert head4(path) == [("005930", "삼성전자", "", None)]


def test_열_순서가_달라도_인식(tmp_path):
    path = _write(tmp_path, "삼성전자,005930,+2.5%\n")
    assert head4(path) == [("005930", "삼성전자", "", None)]


def test_utf8_인코딩_폴백(tmp_path):
    path = _write(tmp_path, "005930,삼성전자\n", encoding="utf-8-sig")
    assert head4(path) == [("005930", "삼성전자", "", None)]


def test_종목명이_없으면_코드로_대체(tmp_path):
    path = _write(tmp_path, "005930,72400,+1.2%\n")
    assert head4(path) == [("005930", "005930", "", None)]


def test_코드가_없는_행은_무시(tmp_path):
    path = _write(tmp_path, "관심종목 목록\n\n005930,삼성전자\n합계,3종목\n")
    assert head4(path) == [("005930", "삼성전자", "", None)]


# ── 영문자가 섞인 종목코드 (2026-08-05 실측 누락) ─────────────


def test_영문자가_섞인_종목코드도_읽는다(tmp_path):
    """신주인수권·스팩 등은 코드에 영문자가 들어간다 (예: 아로마티카 0015N0).

    숫자 6자리만 허용하면 CSV 61종목 중 60종목만 들어오고 하나가 조용히 누락된다.
    """
    path = tmp_path / "w.csv"
    path.write_text(
        "종목코드,종목명\n0015N0,아로마티카\n037710,광주신세계\n", encoding="utf-8-sig"
    )
    rows = parse_watchlist_csv(str(path))
    assert [r[0] for r in rows] == ["0015N0", "037710"]
    assert rows[0][1] == "아로마티카"


def test_소문자_코드는_대문자로_통일된다(tmp_path):
    path = tmp_path / "w.csv"
    path.write_text("종목코드,종목명\n0015n0,아로마티카\n", encoding="utf-8-sig")
    assert parse_watchlist_csv(str(path))[0][0] == "0015N0"


def test_따옴표_A접두_영문코드_조합도_처리한다(tmp_path):
    path = tmp_path / "w.csv"
    path.write_text(
        "종목코드,종목명\n'0015N0,아로마티카\nA005930,삼성전자\n", encoding="utf-8-sig"
    )
    assert [r[0] for r in parse_watchlist_csv(str(path))] == ["0015N0", "005930"]


def test_코드가_아닌_셀은_여전히_걸러낸다(tmp_path):
    """느슨해진 패턴이 엉뚱한 값을 종목으로 잡지 않아야 한다."""
    path = tmp_path / "w.csv"
    path.write_text(
        "종목코드,종목명\n12345,다섯자리\n1234567,일곱자리\n"
        "ABCDEF,영문만\n005930.KS,접미사\n037710,광주신세계\n",
        encoding="utf-8-sig",
    )
    assert [r[0] for r in parse_watchlist_csv(str(path))] == ["037710"]


def test_헤더_없는_형식에서도_영문코드를_찾는다(tmp_path):
    path = tmp_path / "w.csv"
    path.write_text("0015N0,아로마티카,1000\n", encoding="utf-8-sig")
    rows = parse_watchlist_csv(str(path))
    assert rows[0][0] == "0015N0" and rows[0][1] == "아로마티카"


# ── 선정 태그 · 기준봉 (2026-08-07) ───────────────────────────


def test_태그와_기준봉_열을_읽는다(tmp_path):
    """태그는 기준봉 시점의 선정 근거라 종목에 고정된다."""
    path = tmp_path / "w.csv"
    path.write_text(
        "종목코드,종목명,1선,2선,3선,태그,기준봉\n"
        '005930,삼성전자,70000,68000,66000,"#KOSPI상승장, #테마주",2026-08-05\n',
        encoding="utf-8-sig",
    )
    row = parse_watchlist_csv(str(path))[0]
    assert row[4] == "KOSPI상승장,테마주"  # # 과 공백은 정리
    assert row[5] == "2026-08-05"


def test_태그는_구분자와_무관하게_같은_결과가_된다(tmp_path):
    """쉼표·공백·# 를 섞어 적어도 집계가 갈라지지 않아야 한다."""
    from trader.ui.app import _parse_tags

    for raw in (
        "#KOSPI상승장, #테마주",
        "KOSPI상승장 테마주",
        "#KOSPI상승장#테마주",
        "KOSPI상승장,,테마주",
    ):
        assert _parse_tags(raw) == "KOSPI상승장,테마주", raw
    assert _parse_tags("테마주,테마주") == "테마주"  # 중복 제거
    assert _parse_tags("") == "" and _parse_tags("   ") == ""


def test_태그_열이_없어도_읽힌다(tmp_path):
    path = tmp_path / "w.csv"
    path.write_text("종목코드,종목명\n005930,삼성전자\n", encoding="utf-8-sig")
    row = parse_watchlist_csv(str(path))[0]
    assert row[4] == "" and row[5] == ""


def test_태그_칸이_비어_있어도_오류가_없다(tmp_path):
    path = tmp_path / "w.csv"
    path.write_text(
        "종목코드,종목명,1선,2선,3선,태그,기준봉\n"
        "005930,삼성전자,70000,68000,66000,,\n"
        "000660,SK하이닉스,50000,48000,46000,#테마주,\n",
        encoding="utf-8-sig",
    )
    rows = parse_watchlist_csv(str(path))
    assert rows[0][4] == "" and rows[0][5] == ""
    assert rows[1][4] == "테마주" and rows[1][5] == ""


def test_목록에_없는_태그도_그대로_저장된다(tmp_path):
    """태그 목록은 나중에 늘어날 수 있다 — 미리 막지 않는다."""
    path = tmp_path / "w.csv"
    path.write_text(
        "종목코드,종목명,태그\n005930,삼성전자,#눌림목\n", encoding="utf-8-sig"
    )
    assert parse_watchlist_csv(str(path))[0][4] == "눌림목"


# ── 따옴표 없는 태그 복구 (2026-08-09) ────────────────────────
# CSV 는 쉼표가 열 구분자라 태그를 따옴표로 감싸지 않으면 칸이 쪼개지고 뒤가 밀린다.


def _one(tmp_path, text):
    path = tmp_path / "w.csv"
    path.write_text(text, encoding="utf-8-sig")
    return parse_watchlist_csv(str(path))[0]


def test_따옴표_없이_쉼표로_이어_써도_태그가_복구된다(tmp_path):
    row = _one(
        tmp_path,
        "종목코드,종목명,태그,기준봉\n"
        "005930,삼성전자,#KOSPI상승장,#테마주,2026-08-05\n",
    )
    assert row[4] == "KOSPI상승장,테마주"
    assert row[5] == "2026-08-05"  # 기준봉이 밀리지 않는다


def test_샵_없이_써도_뒤_칸이_날짜면_태그로_본다(tmp_path):
    row = _one(
        tmp_path,
        "종목코드,종목명,태그,기준봉\n"
        "005930,삼성전자,KOSPI상승장,테마주,2026-08-05\n",
    )
    assert row[4] == "KOSPI상승장,테마주" and row[5] == "2026-08-05"


def test_태그가_셋_이상이어도_복구된다(tmp_path):
    row = _one(
        tmp_path,
        "종목코드,종목명,태그,기준봉\n" "005930,삼성전자,#A,#B,#C,2026-08-05\n",
    )
    assert row[4] == "A,B,C" and row[5] == "2026-08-05"


def test_메모의_쉼표는_태그로_합치지_않는다(tmp_path):
    """복구 로직이 다른 열의 쉼표를 잘못 건드리면 안 된다."""
    row = _one(
        tmp_path,
        "종목코드,종목명,메모,태그,기준봉\n"
        '005930,삼성전자,"급등, 거래대금",#테마주,2026-08-05\n',
    )
    assert row[2] == "급등, 거래대금"
    assert row[4] == "테마주" and row[5] == "2026-08-05"


def test_따옴표를_쓴_기존_형식도_그대로_동작한다(tmp_path):
    row = _one(
        tmp_path,
        "종목코드,종목명,태그,기준봉\n"
        '005930,삼성전자,"#KOSPI상승장, #테마주",2026-08-05\n',
    )
    assert row[4] == "KOSPI상승장,테마주" and row[5] == "2026-08-05"


def test_영웅문_전체_형식에서도_복구된다(tmp_path):
    path = tmp_path / "w.csv"
    path.write_text(
        "분,신,종목명,현재가,등락률,L일봉H,거래대금,메모,종목코드,1선,2선,3선,태그,기준봉\n"
        '증,,GRT,"3,730",-2.1,3810 3870 3610 3730,"1,089",메모 내용,\'900290,'
        "3355,3215,3105,#KOSPI상승장,#테마주,2026-08-05\n",
        encoding="utf-8-sig",
    )
    row = parse_watchlist_csv(str(path))[0]
    assert row[0] == "900290" and row[2] == "메모 내용"
    assert row[3] == (3355.0, 3215.0, 3105.0)
    assert row[4] == "KOSPI상승장,테마주" and row[5] == "2026-08-05"


def test_태그가_마지막_열이면_구분자_없이도_합쳐진다(tmp_path):
    """새 표준 형식(1선,2선,3선,기준봉,태그)에서는 태그가 마지막 열이다."""
    row = _one(
        tmp_path,
        "종목코드,종목명,1선,2선,3선,기준봉,태그\n"
        "005930,삼성전자,10000,9000,8000,2026-08-05,상승장,테마주\n",
    )
    assert row[4] == "상승장,테마주"
    assert row[5] == "2026-08-05"
    assert row[3] == (10_000.0, 9_000.0, 8_000.0)


def test_새_열_순서에서도_모든_값이_읽힌다(tmp_path):
    row = _one(
        tmp_path,
        "종목명,메모,종목코드,1선,2선,3선,기준봉,태그\n"
        '삼성전자,급등주,005930,10000,9000,8000,2026-08-05,"#테마주 #상한가"\n',
    )
    assert row[:3] == ("005930", "삼성전자", "급등주")
    assert row[4] == "테마주,상한가" and row[5] == "2026-08-05"
