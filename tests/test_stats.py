"""월간 집계 (3단계-5).

집계는 순수 함수라 DB·Discord 없이 시험한다. 표본이 작을 때 잘못 읽지 않도록 하는
장치들(건수 표시, 값이 없으면 None)을 특히 본다.
"""

from __future__ import annotations

from trader import stats


def _entry(**over) -> dict:
    base = {
        "symbol": "005930",
        "name": "삼성전자",
        "state": "종료",
        "total_bought": 10,
        "avg_price": 5_000.0,
        "realized_pnl": 1_000.0,
        "fees": 100.0,
        "high_price": 5_400.0,
        "tags": "",
    }
    return {**base, **over}


# ── 1차 수량별 ──────────────────────────────────────────────────


def test_수량_구간은_분할_익절이_도는지로_가른다():
    """2주면 floor(2×0.4)=0 이라 3% 단계를 건너뛴다 — 3주부터 세 단계가 돈다."""
    entries = [
        _entry(symbol="A", total_bought=1),
        _entry(symbol="B", total_bought=2),
        _entry(symbol="C", total_bought=3),
        _entry(symbol="D", total_bought=9),
        _entry(symbol="E", total_bought=10),
    ]

    buckets = {b.label: b.trades for b in stats.by_quantity(entries)}

    assert buckets == {"1~2주": 2, "3~9주": 2, "10주+": 1}


def test_끝나지_않은_매매는_성적에_넣지_않는다():
    """진행 중인 매매의 중간 손익으로 승률을 매기면 답이 흔들린다."""
    entries = [_entry(), _entry(symbol="X", state="2차 매수", realized_pnl=9_999)]

    total = sum(b.trades for b in stats.by_quantity(entries))

    assert total == 1


def test_승률은_본전을_빼고_센다():
    """세후 0 원은 이긴 것도 진 것도 아니다."""
    entries = [
        _entry(symbol="A", realized_pnl=1_000, fees=100),  # 이김
        _entry(symbol="B", realized_pnl=100, fees=100),  # 본전
        _entry(symbol="C", realized_pnl=0, fees=100),  # 짐
    ]

    bucket = next(b for b in stats.by_quantity(entries) if b.trades)

    assert (bucket.wins, bucket.losses) == (1, 1)
    assert bucket.win_rate == 0.5
    assert bucket.trades == 3  # 건수에는 본전도 들어간다


def test_판정할_수_없으면_None_이지_0_이_아니다():
    """없는 것과 0 은 다르다 — 화면에서 `-` 로 두려면 구분되어야 한다."""
    empty = stats.Bucket("빈 구간")

    assert empty.win_rate is None
    assert empty.rate is None
    assert empty.mfe is None


def test_최고_도달률은_평단_대비다():
    """'얼마까지 갔었나' 는 익절 비중을 고칠 근거다."""
    bucket = next(
        b
        for b in stats.by_quantity([_entry(avg_price=5_000, high_price=5_400)])
        if b.trades
    )

    assert bucket.mfe == 0.08


def test_평단이_없으면_최고_도달률을_세지_않는다():
    """강제 복구된 포지션은 평단이 0 일 수 있다 — 0 으로 나누면 터진다."""
    bucket = next(
        b
        for b in stats.by_quantity([_entry(avg_price=0, high_price=5_400)])
        if b.trades
    )

    assert bucket.mfe is None


# ── 태그별 ──────────────────────────────────────────────────────


def test_태그가_여럿이면_각각에_센다():
    """'이 태그가 붙은 매매의 성적' 을 보려는 것이지 배타적으로 나누려는 게 아니다."""
    entries = [
        _entry(symbol="A", tags="테마주,상한가"),
        _entry(symbol="B", tags="테마주"),
    ]

    counts = {b.label: b.trades for b in stats.by_tag(entries)}

    assert counts == {"테마주": 2, "상한가": 1}


def test_태그가_없으면_아무_구간도_만들지_않는다():
    assert stats.by_tag([_entry(tags="")]) == []


def test_태그는_건수_많은_순으로_자른다():
    entries = [
        _entry(symbol=f"{i}", tags=",".join(f"t{j}" for j in range(i + 1)))
        for i in range(8)
    ]

    tags = stats.by_tag(entries, top=3)

    assert len(tags) == 3
    assert [b.trades for b in tags] == sorted((b.trades for b in tags), reverse=True)


# ── 체결 오차 ───────────────────────────────────────────────────


def _slip(ts, side, gap):
    return {"ts": ts, "side": side, "gap": gap}


def test_개장_직후와_그_뒤를_나눈다():
    """지금까지 최악 네 건이 전부 09:01~09:05 였다."""
    rows = [
        _slip("2026-09-01 09:01:12", "매수", 0.0414),
        _slip("2026-09-01 09:02:20", "매도", -0.0304),
        _slip("2026-09-01 13:22:10", "매도", -0.0037),
    ]

    early, late = stats.by_opening(rows)

    assert early.count == 2 and late.count == 1
    assert early.buy == 0.0414
    assert round(early.sell, 4) == -0.0304
    assert late.buy is None  # 그 구간에 매수가 없으면 None


def test_최악은_손해_방향으로_고른다():
    """매수는 비싸게 사면, 매도는 싸게 팔면 손해다 — 부호를 통일해 비교한다."""
    rows = [
        _slip("2026-09-01 13:00:00", "매수", 0.01),
        _slip("2026-09-01 13:10:00", "매도", -0.03),
    ]

    _early, late = stats.by_opening(rows)

    assert late.worst == 0.03  # 매도 -3% 가 매수 +1% 보다 나쁘다


def test_경계_시각은_그_뒤로_센다():
    """`09:10` 정각은 '개장 직후' 가 아니다 — 경계를 한 번만 정해 둔다."""
    early, late = stats.by_opening([_slip("2026-09-01 09:10:00", "매수", 0.01)])

    assert early.count == 0 and late.count == 1


def test_판정가가_없는_체결은_세지_않는다():
    """수동 청산처럼 판정 없이 나간 주문은 비교할 대상이 없다."""
    early, late = stats.by_opening([{"ts": "2026-09-01 09:01:00", "side": "매도"}])

    assert early.count == 0 and late.count == 0
