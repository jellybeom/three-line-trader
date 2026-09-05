"""notifier 단위 테스트 — 알림 수준 필터, 메시지·embed 형식 (발송은 봇 담당)."""

import pytest

from trader.notifier import format_message, should_notify

# ── 알림 수준 필터 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "level,symbol,kind,expected",
    [
        ("전체", "시스템", "연결", True),
        ("전체", "005930", "체결", True),
        ("매매만 (시스템 제외)", "005930", "체결", True),
        ("매매만 (시스템 제외)", "005930", "에러", True),
        ("매매만 (시스템 제외)", "시스템", "연결", False),
        ("에러만", "005930", "체결", False),
        ("에러만", "005930", "에러", True),
        ("에러만", "시스템", "경고", True),
        ("끔", "005930", "에러", False),
    ],
)
def test_알림_수준_필터(level, symbol, kind, expected):
    assert should_notify(level, symbol, kind) is expected


def _report():
    symbols = [
        {
            "symbol": "010060",
            "name": "OCI홀딩스",
            "memo": "",
            "state": "종료",
            "avg_price": 200_500,
            "total_bought": 1,
            "remaining": 0,
            "realized_pnl": 200,
            "fees": 69,
            "high_price": 210_000,
            "low_price": 198_000,
        },
        {
            "symbol": "475150",
            "name": "SK이터닉스",
            "memo": "",
            "state": "2차 매수",
            "avg_price": 61_000,
            "total_bought": 8,
            "remaining": 8,
            "realized_pnl": 0,
            "fees": 66,
            "high_price": 62_900,
            "low_price": 59_800,
        },
    ]
    fills = [
        {
            "ts": "2026-07-27 09:31:00",
            "symbol": "010060",
            "side": "매수",
            "qty": 1,
            "price": 200_500,
            "reason": "",
        },
        {
            "ts": "2026-07-27 14:10:00",
            "symbol": "010060",
            "side": "매도",
            "qty": 1,
            "price": 200_700,
            "reason": "",
        },
    ]
    return symbols, fills


def test_요약_embed는_손익_부호에_따라_색이_바뀐다():
    from trader.notifier import build_daily_summary_embed

    symbols, fills = _report()
    embed = build_daily_summary_embed("2026-07-27", symbols, fills, deposit=438_191)
    assert embed["color"] == 0x2E7D32  # 세후 +65원 → 초록

    symbols[0]["realized_pnl"] = -5_000
    loss = build_daily_summary_embed("2026-07-27", symbols, fills)
    assert loss["color"] == 0xC62828


def test_요약_embed에_종목별_필드와_이월_안내가_담긴다():
    from trader.notifier import build_daily_summary_embed

    symbols, fills = _report()
    embed = build_daily_summary_embed("2026-07-27", symbols, fills, deposit=438_191)
    names = [f["name"] for f in embed["fields"]]
    assert any("OCI홀딩스(010060)" in n for n in names)
    assert any("⚠️" in n for n in names)  # 보유 중 종목 경고 아이콘
    assert any("이월" in f["value"] for f in embed["fields"])
    assert "438,191원" in embed["footer"]["text"]


def test_매매가_없으면_회색_embed():
    from trader.notifier import build_daily_summary_embed

    embed = build_daily_summary_embed("2026-07-25", [], [])
    assert embed["color"] == 0x616161
    assert "없습니다" in embed["description"]


def test_여러_건은_한_장의_embed로_묶인다():
    from trader.notifier import build_batch_embed

    items = [("등록", f"종목{i}(00000{i})", "대기, 잔량 0주") for i in range(5)]
    embed = build_batch_embed(items)
    assert "등록 5건" in embed["title"]
    assert embed["description"].count("\n") == 4  # 5줄
    assert "fields" not in embed  # 경고가 없으면 필드도 없다


def test_경고는_별도_필드로_분리되고_색이_바뀐다():
    from trader.notifier import build_batch_embed

    items = [
        ("등록", "삼성전자(005930)", "대기"),
        ("경고", "동아쏘시오(000640)", "1차 매수 예상 2주"),
    ]
    embed = build_batch_embed(items)
    assert embed["color"] == 0xEF6C00  # 주황 — 확인 필요
    assert "확인 필요 1건" in embed["fields"][0]["name"]
    assert "동아쏘시오" in embed["fields"][0]["value"]
    assert "동아쏘시오" not in embed["description"]  # 본문에는 중복되지 않는다


def test_너무_많으면_잘라내고_남은_건수를_알린다():
    from trader.notifier import build_batch_embed

    embed = build_batch_embed([("등록", f"종목{i}", "대기") for i in range(60)])
    assert "외 20건" in embed["description"]
    assert len(embed["description"]) <= 4000


# ── 경보 embed / 에러 압축 ─────────────────────────────────────


def test_API_오류는_사람이_읽을_부분만_남긴다():
    from trader.notifier import shorten_error

    raw = (
        "주문 실패(3회): kt10000 실패 (HTTP 200, code 20): [2000] "
        "(855056:매수증거금이 부족합니다. 127주 매수가능) — 30초간 재시도 보류"
    )
    out = shorten_error(raw)
    assert "매수증거금이 부족합니다. 127주 매수가능" in out
    assert "HTTP" not in out and "kt10000" not in out
    assert out.startswith("주문 실패(3회):") and out.endswith(
        "재시도 보류"
    )  # 문맥은 유지


def test_일반_괄호_설명은_그대로_둔다():
    from trader.notifier import shorten_error

    text = "주문가능금액 필드를 찾지 못함 (kt00001 응답 필드 변경 가능성)"
    assert shorten_error(text) == text


def test_경보는_종류별로_색과_아이콘이_다르다():
    from trader.notifier import build_alert_embed

    assert build_alert_embed("에러", "005930", "x")["color"] == 0xC62828
    assert build_alert_embed("경고", "005930", "x")["color"] == 0xEF6C00
    assert build_alert_embed("보류", "005930", "x")["color"] == 0xEF6C00
    assert "⛔" in build_alert_embed("에러", "005930", "x")["title"]


def test_종료_결산은_세후_손익_부호로_색이_정해진다():
    from trader.notifier import build_trade_embed

    loss = build_trade_embed(
        "대원전선", "006340", "3선 이탈 → 전량 손절", 43, 10_650, -31_850, 1_180
    )
    assert loss["color"] == 0xC62828
    assert "-33,030원" in loss["description"]  # 세후
    assert "종료" in loss["title"]

    win = build_trade_embed(
        "삼성전자", "005930", "+7% → 전량 청산", 10, 80_000, 50_000, 900
    )
    assert win["color"] == 0x2E7D32 and "💰" in win["title"]


def test_종류마다_아이콘과_색이_지정된다():
    from trader.notifier import build_alert_embed

    cases = {"삭제": "🗑️", "연결": "🔗", "감시": "👁️", "설정": "⚙️", "이월": "📦"}
    for kind, icon in cases.items():
        embed = build_alert_embed(kind, "005930", "내용")
        assert icon in embed["title"] and kind in embed["title"]
    assert build_alert_embed("연결", "시스템", "x")["color"] == 0x00838F
    assert "시스템" not in build_alert_embed("연결", "시스템", "x")["title"]


def test_묶음_제목에도_아이콘이_붙는다():
    from trader.notifier import build_batch_embed

    embed = build_batch_embed(
        [("등록", "A", "x"), ("등록", "B", "x"), ("삭제", "C", "x")]
    )
    assert "➕ 등록 2건" in embed["title"] and "🗑️ 삭제 1건" in embed["title"]


def test_요약에_계좌_평가와_추정자산이_들어간다():
    """이월 종목이 있는 날은 실현손익만으로 성과를 판단할 수 없다."""
    from trader.notifier import build_daily_summary_embed

    symbols, fills = _report()
    embed = build_daily_summary_embed(
        "2026-07-27",
        symbols,
        fills,
        deposit=1_029_296,
        account={"value": 315_425, "pnl": 6_801, "rate": 2.21, "asset": 1_344_071},
    )
    account_field = [f for f in embed["fields"] if "계좌" in f["name"]]
    assert account_field, "계좌 필드가 없음"
    assert "315,425원" in account_field[0]["value"]
    assert "+2.21%" in account_field[0]["value"]
    assert "1,344,071원" in account_field[0]["value"]
    assert "주문가능 1,029,296원" in embed["footer"]["text"]


def test_계좌_정보가_없으면_계좌_필드도_없다():
    from trader.notifier import build_daily_summary_embed

    symbols, fills = _report()
    embed = build_daily_summary_embed("2026-07-27", symbols, fills)
    assert not [f for f in embed["fields"] if "계좌" in f["name"]]


# ── 1선 근접도 (2026-08-05) ────────────────────────────────────


def _waiting(symbol, name, line1, day_low):
    return {
        "symbol": symbol,
        "name": name,
        "memo": "",
        "line1": line1,
        "state": "대기",
        "avg_price": 0,
        "total_bought": 0,
        "remaining": 0,
        "realized_pnl": 0,
        "fees": 0,
        "high_price": 0,
        "low_price": 0,
        "day_low": day_low,
    }


def test_근접도는_1선에_가까운_순으로_보여준다():
    """진입이 없는 날, 설정이 보수적인지 시장이 안 맞는지 구분하는 근거."""
    from trader.notifier import build_daily_summary_embed

    symbols = [
        _waiting("000660", "SK하이닉스", 50_000, 60_000),  # +20%
        _waiting("005430", "한국공항", 79_100, 79_700),  # +0.8%
        _waiting("098460", "고영", 20_000, 21_900),
    ]  # +9.5%
    embed = build_daily_summary_embed("2026-08-05", symbols, [])
    field = [f for f in embed["fields"] if "근접도" in f["name"]][0]
    lines = field["value"].splitlines()
    assert "한국공항" in lines[0]  # 가장 가까운 종목이 맨 위
    assert "SK하이닉스" in lines[2]
    assert "3% 이내 **1종목**" in field["value"]
    assert "10% 이내 **2종목**" in field["value"]


def test_진입한_종목은_근접도에서_빠진다():
    from trader.notifier import build_daily_summary_embed

    entered = _waiting("005930", "삼성전자", 10_000, 9_800)
    entered["total_bought"] = 10  # 실제로 진입한 종목
    symbols = [entered, _waiting("005430", "한국공항", 79_100, 79_700)]
    field = [
        f
        for f in build_daily_summary_embed("2026-08-05", symbols, [])["fields"]
        if "근접도" in f["name"]
    ][0]
    assert "삼성전자" not in field["value"]
    assert "총 1종목" in field["value"]


def test_틱이_없어_최저가가_없으면_근접도를_넣지_않는다():
    """감시를 켜지 않은 날이나 휴장일에 빈 섹션이 붙지 않게."""
    from trader.notifier import build_daily_summary_embed

    embed = build_daily_summary_embed(
        "2026-08-05", [_waiting("005430", "한국공항", 79_100, 0)], []
    )
    assert not [f for f in embed["fields"] if "근접도" in f["name"]]


# ── 관심종목 등록 알림 (2026-08-08) ───────────────────────────


def test_기준봉_경과일_계산():
    from trader.notifier import base_date_label

    assert base_date_label("2026-08-05", "2026-08-07") == "D+2"
    assert base_date_label("2026-08-07", "2026-08-07") == "D0"
    assert base_date_label("", "2026-08-07") == ""  # 기준봉 미입력
    assert base_date_label("이상한값", "2026-08-07") == ""  # 형식 오류도 조용히


def test_브리핑은_나열_대신_집계로_보여준다():
    """종목을 일일이 적으면 수십 줄이 되고 Discord 제한에 걸린다."""
    from trader.notifier import build_briefing_embed

    embed = build_briefing_embed(
        "2026-08-10",
        _watch_rows(),
        {"total": 2_025_000, "max_symbols": 5, "per_symbol": 405_000},
        1_793_453,
    )
    body = embed["description"]
    assert "관심종목 3종목" in embed["title"]
    assert "`#테마주` 2" in body and "`#섹터주` 1" in body
    assert "D+5 1종목" in body  # 기준봉 경과일 분포
    assert "총 2,025,000원" in body and "**주문가능** 1,793,453원" in body
    assert "GRT" not in body  # 개별 종목은 나열하지 않는다
    assert "footer" not in embed  # 안내 문구는 두지 않는다


def test_브리핑은_소량_종목만_따로_짚는다():
    from trader.notifier import build_briefing_embed

    embed = build_briefing_embed("2026-08-10", _watch_rows())
    field = embed["fields"][0]
    assert "소량 진입 예상 1종목" in field["name"]
    assert "효성(004800) 1차 1주" in field["value"]
    assert "GRT" not in field["value"]


def _watch_rows():
    return [
        {
            "symbol": "900290",
            "name": "GRT",
            "tags": "KOSPI상승장,테마주",
            "base_date": "2026-08-05",
            "memo": "거래대금 급증",
            "qty": 120,
        },
        {
            "symbol": "004800",
            "name": "효성",
            "tags": "섹터주",
            "base_date": "2026-08-07",
            "memo": "",
            "qty": 1,
        },
        {
            "symbol": "043260",
            "name": "성호전자",
            "tags": "상한가,테마주",
            "base_date": "",
            "memo": "",
            "qty": 25,
        },
    ]


def test_관심종목_조회는_태그_메모_기준봉을_모두_보여준다():
    from trader.notifier import build_watchlist_embed

    body = build_watchlist_embed("2026-08-10", _watch_rows())["description"]
    assert "**GRT**(`900290`)" in body
    assert "`#KOSPI상승장`" in body and "기준봉 D+5" in body
    assert "📝 거래대금 급증" in body
    assert "기준봉" not in body.split("성호전자")[1]  # 기준봉 없으면 표기 생략


def test_관심종목_조회는_페이지로_나뉜다():
    """수십 종목이어도 '…외 N종목' 으로 잘리지 않고 전부 볼 수 있어야 한다."""
    from trader.notifier import build_watchlist_embed

    rows = [
        {
            "symbol": f"00593{i:02d}",
            "name": f"종목{i:02d}",
            "tags": "",
            "base_date": "",
            "memo": "",
            "qty": 10,
        }
        for i in range(40)
    ]
    first = build_watchlist_embed("2026-08-10", rows, page=1, per_page=15)
    assert first["footer"]["text"] == "1/3 쪽"
    assert "종목00" in first["description"] and "종목20" not in first["description"]

    last = build_watchlist_embed("2026-08-10", rows, page=3, per_page=15)
    assert "종목39" in last["description"]
    assert "…외" not in last["description"]  # 생략 없이 끝까지

    over = build_watchlist_embed("2026-08-10", rows, page=99, per_page=15)
    assert over["footer"]["text"] == "3/3 쪽"  # 범위를 넘으면 마지막 쪽


def test_관심종목_조회를_태그로_거를_수_있다():
    from trader.notifier import build_watchlist_embed

    embed = build_watchlist_embed("2026-08-10", _watch_rows(), tag="테마주")
    assert "관심종목 2종목" in embed["title"] and "#테마주" in embed["title"]
    assert "효성" not in embed["description"]


# ── 알림 수준과 사고 알림 ──────────────────────────────────────


def test_매매만_이어도_사고는_알린다():
    """'매매만' 은 시스템 **잡음**을 걸러 달라는 뜻이지 사고를 숨기라는 뜻이 아니다.

    예전에는 종류와 무관하게 시스템 로그를 전부 막아, 키움 연결 실패·자동 시작 실패·
    휴장 안내까지 가지 않았다(2026-08-17 확인).
    """
    from trader.notifier import should_notify

    level = "매매만 (시스템 제외)"
    assert should_notify(level, "시스템", "에러")
    assert should_notify(level, "시스템", "경고")
    assert not should_notify(level, "시스템", "연결")  # 잡음은 그대로 걸린다
    assert not should_notify(level, "시스템", "설정")


def test_끔은_사고도_보내지_않는다():
    """일부러 껐다면 그 뜻을 존중한다."""
    from trader.notifier import should_notify

    assert not should_notify("끔", "시스템", "에러")
    assert not should_notify("끔", "005930", "체결")


def test_에러만은_매매_알림을_거른다():
    from trader.notifier import should_notify

    assert should_notify("에러만", "005930", "에러")
    assert not should_notify("에러만", "005930", "체결")


def test_전체는_모두_보낸다():
    from trader.notifier import should_notify

    for kind in ("체결", "전이", "연결", "경고", "에러"):
        assert should_notify("전체", "005930", kind)


# ── 종료 결산 알림 ────────────────────────────────────────────


def _closed_embed(**kwargs):
    from trader.notifier import build_trade_embed

    base = dict(
        name="동양파일",
        symbol="228340",
        reason="3선 이탈 → 전량 손절",
        qty=125,
        price=3_065,
        realized=-22_393,
        fees=692,
        path="2차 매수 → 손절",
        avg_price=3_244,
        total_bought=125,
    )
    base.update(kwargs)
    return build_trade_embed(**base)


def test_상태_경로가_결과_자리를_대신한다():
    """'전량 손절' 만으로는 1차에서 죽었는지 2차까지 갔는지 알 수 없다."""
    embed = _closed_embed()
    assert "2차 매수 → 손절" in embed["description"]
    assert "125주" not in embed["description"]  # 잔량 주수는 뺐다


def test_경로가_없으면_사유로_대신한다():
    """옛 기록이나 경로 계산 실패에도 알림은 나가야 한다."""
    assert "전량 손절" in _closed_embed(path="")["description"]


def test_수익률을_함께_보여준다():
    """금액만으로는 큰 손실인지 알 수 없다 — 저가주는 절대액이 커 보인다."""
    assert "-5.69%" in _closed_embed()["description"]


def test_청산가는_평균이다():
    """3단 익절이면 판 가격이 제각각이라 마지막 한 건으로는 답이 안 된다."""
    embed = _closed_embed(
        name="제닉",
        symbol="123330",
        realized=9_050,
        fees=343,
        price=32_100,  # 마지막 체결가
        avg_price=30_600,
        total_bought=6,
        path="1차 매수 → 3% 익절 → 5% 익절 → 7% 익절",
    )
    # 평단 30,600 + 주당 세전손익(9,050/6=1,508) = 32,108
    assert "평단 30,600 → 청산 32,108" in embed["description"]


def test_투입_금액을_읽기_쉽게_줄인다():
    """폰에서 자릿수를 세지 않아도 규모가 잡히게."""
    from trader.notifier import _short_won

    assert _short_won(406_000) == "40.6만원"
    assert _short_won(9_999) == "9,999원"  # 만원 미만은 그대로
    assert _short_won(120_000_000) == "1.20억원"
    assert _short_won(8_500) == "8,500원"
    assert (
        "투입 40.5만원"
        in _closed_embed(avg_price=3_244, total_bought=125)["description"]
    )


def test_진입하지_않고_끝나면_가격_줄이_없다():
    """3선 이하 갭 시가 — 살 것도 팔 것도 없었다."""
    embed = _closed_embed(
        path="진입 금지", avg_price=0, total_bought=0, realized=0, fees=0, qty=0
    )
    assert "평단" not in embed["description"]
    assert "%" not in embed["description"]  # 0 으로 나누지 않는다


def test_이익과_손실은_색과_아이콘이_다르다():
    profit = _closed_embed(realized=9_050, fees=343)
    loss = _closed_embed()
    assert profit["title"].startswith("💰")
    assert loss["title"].startswith("🛑")
    assert profit["color"] != loss["color"]


def test_세후_기준이다():
    """비용을 뺀 값이 실제로 번 돈이다."""
    embed = _closed_embed(realized=1_000, fees=300)
    assert "+700원" in embed["description"]
    assert "세전" not in embed["description"]  # 분해는 로그·대조에서 본다


# ── CSV 등록 경고 (2026-08-31) ──────────────────────────────────


def test_등록_알림은_고쳐야_할_것만_담는다():
    """경고 한 건 때문에 80종목 목록이 폰으로 쏟아지면 정작 고쳐야 할 줄이 묻힌다.

    편성 결과 전체는 08:55 개장 브리핑이 맡고, 언제든 `/관심종목` 으로도 볼 수 있다.
    """
    from trader.notifier import build_registration_warning_embed

    warns = ["코스메카코리아(241710) 1선 142,000 — 매수 금액으로 1주를 살 수 없습니다"]
    embed = build_registration_warning_embed("2026-08-31", warns, total=80)

    assert "확인 필요 1건" in embed["title"]
    assert "코스메카코리아" in embed["description"]
    assert "80종목" in embed["footer"]["text"]  # 나머지는 정상이라는 안심만
    assert len(embed["description"]) < 300  # 종목 목록이 딸려 오지 않는다


def test_경고가_많아도_열_건까지만_보낸다():
    from trader.notifier import build_registration_warning_embed

    embed = build_registration_warning_embed(
        "2026-08-31", [f"종목{i} 경고" for i in range(30)], total=80
    )
    assert embed["description"].count("•") == 10


# ── 요약 손익 기준 (2026-09-03) ─────────────────────────────────


def _summary_rows():
    """이월돼 오늘 청산한 종목 하나 + 오늘 부분 익절만 하고 이월되는 종목 하나."""
    return [
        {
            "symbol": "038680",
            "name": "에스넷",
            "state": "종료",
            "avg_price": 3_908.0,
            "total_bought": 70,
            "remaining": 0,
            "realized_pnl": 2_822.0,
            "fees": 593.0,  # 오늘 몫
            "high_price": 4_135.0,
            "low_price": 3_880.0,
            "day_open": 3_900.0,
            "day_close": 3_880.0,
            "memo": "",
            "tags": "",
            "base_date": "",
            "line1": 0,
            "day_low": 0,
        },
        {
            "symbol": "134580",
            "name": "탑코미디어",
            "state": "2차 매수",
            "avg_price": 3_287.0,
            "total_bought": 83,
            "remaining": 83,
            "realized_pnl": 1_500.0,
            "fees": 100.0,  # 오늘 부분 익절
            "high_price": 3_400.0,
            "low_price": 3_200.0,
            "day_open": 3_360.0,
            "day_close": 3_215.0,
            "memo": "",
            "tags": "",
            "base_date": "",
            "line1": 0,
            "day_low": 0,
        },
    ]


def test_종료된_매매는_사이클_전체_손익을_적는다():
    """이월된 매매는 며칠에 걸쳐 있다 — 하루치만 적으면 전날 익절이 통째로 빠진다."""
    from trader.notifier import build_daily_summary_embed

    embed = build_daily_summary_embed(
        "2026-09-03",
        _summary_rows(),
        [],
        cycle_pnl={"038680": (6_822.0, 613.0)},  # 어제 익절 4,000 포함
    )
    field = next(f for f in embed["fields"] if "에스넷" in f["name"])

    assert "매매 손익 **+6,209원**" in field["value"]  # 6,822 − 613
    assert "세전 +6,822" in field["value"]
    assert "+2,229" not in field["value"]  # 하루치가 아니다


def test_보유_중인_종목에는_손익을_적지_않는다():
    """아직 끝나지 않은 매매의 중간 손익은 오해를 부른다."""
    from trader.notifier import build_daily_summary_embed

    embed = build_daily_summary_embed("2026-09-03", _summary_rows(), [])
    field = next(f for f in embed["fields"] if "탑코미디어" in f["name"])

    assert "손익" not in field["value"]
    assert "다음 매매일로 이월하세요" in field["value"]


def test_머리글은_오늘_실현분_합계다():
    """날짜별로 더했을 때 총합이 맞아야 하므로 그날 몫만 센다.

    보유 종목의 오늘 익절도 계좌에 들어온 돈이라 합계에는 들어간다 — 종목별 줄에서만
    안 보일 뿐이다.
    """
    from trader.notifier import build_daily_summary_embed

    embed = build_daily_summary_embed(
        "2026-09-03", _summary_rows(), [], cycle_pnl={"038680": (6_822.0, 613.0)}
    )
    head = embed["description"]

    # (2,822−593) + (1,500−100) = 3,629 — 사이클 합산이 섞여 들지 않았다
    assert "오늘 실현 **+3,629원**" in head
    assert "기준이 다르다" not in head  # 군더더기 없이 라벨로만 구분


def test_사이클_손익이_없으면_그날_행으로_물러난다():
    """요약이 통째로 빠지는 것보다 하루치라도 보이는 편이 낫다."""
    from trader.notifier import build_daily_summary_embed

    embed = build_daily_summary_embed("2026-09-03", _summary_rows(), [])
    field = next(f for f in embed["fields"] if "에스넷" in f["name"])

    assert "매매 손익 **+2,229원**" in field["value"]


# ── 보류 종목 (2026-09-04) ──────────────────────────────────────


def _blocked_rows():
    def sym(code, name, low, line1, bought=0):
        return {
            "symbol": code,
            "name": name,
            "state": "대기" if not bought else "종료",
            "avg_price": 0.0,
            "total_bought": bought,
            "remaining": 0,
            "realized_pnl": 0.0,
            "fees": 0.0,
            "high_price": 0.0,
            "low_price": 0.0,
            "day_open": 0.0,
            "day_close": 0.0,
            "memo": "",
            "tags": "",
            "base_date": "",
            "line1": line1,
            "day_low": low,
        }

    return [
        sym("066970", "엘앤에프", 68_200, 68_750),
        sym("189860", "서전기전", 4_010, 4_060),
        sym("241710", "코스메카코리아", 139_000, 142_000),
        sym("019010", "베뉴지", 4_335, 4_275),  # 1선에 안 닿음
    ]


_MAX = "최대 종목 수(7) 도달 — 자리가 나면 재시도합니다"
_QTY = "매수 수량 0주 — 매수 금액으로 142,000원에 1주도 살 수 없습니다"


def test_보류_사유를_묶는_이름은_설정값을_떼어_낸다():
    """괄호 안 숫자를 그대로 두면 같은 사유가 갈라진다."""
    from trader.notifier import block_reason

    assert block_reason(_MAX) == "최대 종목 수"
    assert (
        block_reason("예수금 부족(1,509,883 < 137,857) — 자금이 생기면")
        == "예수금 부족"
    )
    assert block_reason(_QTY) == "매수 수량 0주"


def test_보류_종목을_사유별로_나눠_전부_보여준다():
    """사유가 섞이면 자금을 늘려야 할지 종목 수를 늘려야 할지 알 수 없다."""
    from trader.notifier import build_blocked_fields

    fields = build_blocked_fields(
        _blocked_rows(), {"066970": _MAX, "189860": _MAX, "241710": _QTY}
    )

    assert [f["name"] for f in fields] == [
        "⏸️ 진입 못 함 · 최대 종목 수 2종목",  # 많은 사유가 먼저
        "⏸️ 진입 못 함 · 매수 수량 0주 1종목",
    ]
    assert "엘앤에프" in fields[0]["value"] and "서전기전" in fields[0]["value"]
    assert "-0.8%" in fields[0]["value"]  # 근접도도 함께


def test_보류_종목은_개수를_자르지_않는다():
    """이 목록 자체가 '몇 개나 놓쳤나' 에 답한다 — 5개만 보이면 그 답이 사라진다."""
    from trader.notifier import build_blocked_fields

    rows = [
        {
            "symbol": f"{i:06d}",
            "name": f"종목{i}",
            "total_bought": 0,
            "line1": 1_000,
            "day_low": 990,
        }
        for i in range(12)
    ]
    fields = build_blocked_fields(rows, {f"{i:06d}": _MAX for i in range(12)})

    assert "12종목" in fields[0]["name"]
    assert fields[0]["value"].count("\n") == 11  # 열두 줄 전부


def test_보류_종목은_근접도_목록에서_뺀다():
    """1선을 이미 지나 괴리율이 음수라 '내일 볼 만한 종목' 맨 위를 차지해 버린다."""
    from trader.notifier import build_proximity_field

    field = build_proximity_field(_blocked_rows(), exclude={"066970", "189860"})

    assert "엘앤에프" not in field["value"]
    assert "베뉴지" in field["value"]


def test_보류_종목이_없으면_필드도_없다():
    from trader.notifier import build_blocked_fields

    assert build_blocked_fields(_blocked_rows(), {}) == []


def test_보류_종목은_근접도보다_먼저_나온다():
    """'살 수 있었는데 못 산 것' 이 '내일 볼 만한 것' 보다 급하다."""
    from trader.notifier import build_daily_summary_embed

    embed = build_daily_summary_embed(
        "2026-09-02", _blocked_rows(), [], blocked={"066970": _MAX}
    )
    names = [f["name"] for f in embed["fields"]]

    assert names.index("⏸️ 진입 못 함 · 최대 종목 수 1종목") < names.index(
        "🎯 1선 근접도 (미진입)"
    )


# ── 매매 로그 CSV (2026-09-05) ──────────────────────────────────


def test_로그_CSV는_화면_내보내기와_같은_형식이다():
    """엑셀 한글 호환 BOM 이 없으면 폰·엑셀에서 한글이 깨진다."""
    from trader.notifier import build_log_csv

    data = build_log_csv(
        [("2026-09-04 09:13:36", "037440", "희림", "체결", "1차 익절 → 체결 8주")]
    )

    assert data.startswith(b"\xef\xbb\xbf")  # BOM
    text = data.decode("utf-8-sig")
    assert text.splitlines()[0] == "시각,대상,종목명,종류,내용"
    assert "희림" in text


def test_내용에_쉼표가_있어도_열이_밀리지_않는다():
    """보류 사유에 `예수금 부족(1,509,883 < 137,857)` 처럼 쉼표가 들어간다."""
    import csv
    import io

    from trader.notifier import build_log_csv

    data = build_log_csv(
        [("t", "시스템", "-", "보류", "예수금 부족(1,509,883 < 137,857)")]
    )
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))

    assert len(rows[1]) == 5
    assert rows[1][4] == "예수금 부족(1,509,883 < 137,857)"


def test_로그가_없으면_빈_CSV라도_머리글은_있다():
    from trader.notifier import build_log_csv

    assert "시각,대상" in build_log_csv([]).decode("utf-8-sig")


# ── 월간 집계 embed (3단계-5) ───────────────────────────────────


def _month_entry(**over):
    base = {
        "symbol": "005930",
        "name": "삼성전자",
        "state": "종료",
        "total_bought": 10,
        "avg_price": 5_000.0,
        "realized_pnl": 1_000.0,
        "fees": 100.0,
        "high_price": 5_400.0,
        "tags": "테마주",
    }
    return {**base, **over}


def test_월간_집계는_건수를_항상_함께_적는다():
    """표본이 작을 때 3건짜리 100% 를 신호로 읽으면 그 판단이 몇 달을 간다."""
    from trader import stats
    from trader.notifier import build_monthly_embed

    entries = [_month_entry(), _month_entry(symbol="B", total_bought=2)]
    embed = build_monthly_embed(
        "2026-09", entries, stats.by_quantity(entries), stats.by_tag(entries), [], {}
    )

    qty = next(f for f in embed["fields"] if "수량별" in f["name"])
    assert "1건" in qty["value"]
    assert "표본" in embed["footer"]["text"]


def test_보유_중인_종목은_성적에서_빠지고_따로_적는다():
    from trader import stats
    from trader.notifier import build_monthly_embed

    entries = [_month_entry(), _month_entry(symbol="B", state="1차 매수")]
    embed = build_monthly_embed(
        "2026-09", entries, stats.by_quantity(entries), [], [], {}
    )

    assert "1건" in embed["description"]  # 종료된 것만 센다
    assert "보유 중 1종목" in embed["description"]


def test_값이_없는_항목은_적지_않는다():
    """없는 것과 0 은 다르다."""
    from trader.notifier import _slip_line
    from trader.stats import Slip

    line = _slip_line(Slip("09:00~09:10", buys=[0.01]))

    assert "매수 +1.00%" in line
    assert "매도" not in line


def test_비어_있는_구간은_필드로_만들지_않는다():
    """빈 표가 붙으면 무엇이 없는 건지 헷갈린다."""
    from trader.notifier import build_monthly_embed
    from trader.stats import Bucket

    embed = build_monthly_embed("2026-09", [], [Bucket("1~2주")], [], [], {})

    assert not any("수량별" in f["name"] for f in embed["fields"])


def test_보류는_횟수로_센다():
    """같은 종목이 하루에 여러 번 보류된다 — 자리가 얼마나 자주 모자랐는지가 값이다."""
    from trader.notifier import build_monthly_embed

    embed = build_monthly_embed(
        "2026-09", [], [], [], [], {"최대 종목 수": 24, "예수금 부족": 1}
    )

    field = next(f for f in embed["fields"] if "진입 못 함" in f["name"])
    assert "25회" in field["name"]
    assert "최대 종목 수 `24회`" in field["value"]
