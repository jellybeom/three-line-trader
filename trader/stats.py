"""월간 집계 — 매매일지 3단계-5.

**숫자만 뽑는다.** 그래프는 다음 단계(3단계-6)이고, 무엇을 그릴지는 숫자가 나온 뒤에
정해진다. 전부 순수 함수라 DB·Discord 없이 시험할 수 있다.

무엇에 답하려는 것인가
----------------------
몇 주째 미뤄 둔 판단이 셋 있고, 셋 다 **자금 배분 하나로 얽혀 있다.**

1. **1차 수량이 적으면 분할 익절이 안 된다** — 2주면 `floor(2×0.4)=0` 이라 3% 단계를
   건너뛴다(2026-09-01 SK바이오팜: 3% 에서 못 팔고 결국 수동 청산).
2. **최대 종목 수 7 이 자리를 막는다** — 2026-09-02 에 5종목, 09-04 에 3종목을 놓쳤다.
3. **개장 직후 슬리피지가 크다** — 최악 네 건이 전부 09:01~09:05 였다.

종목 수를 줄이면 1번은 풀리고 2번은 나빠진다. 반대도 마찬가지다. **숫자 없이는 정할 수
없는 종류의 결정**이라, 짐작으로 바꾸기 전에 세어 본다.

표본이 작다는 점
----------------
지금은 매매가 수십 건이라 구간을 나누면 구간당 몇 건뿐이다. 승률 차이가 우연일 수
있으므로 **건수를 항상 함께 적는다** — 3건짜리 100% 를 신호로 읽지 않기 위해서다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 1차 수량 구간. 분할 익절이 되는지로 가른다 — 3주부터 3% / 5% / 7% 가 1주씩 나간다.
QTY_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("1~2주", 1, 2),  # 3% 단계를 건너뛴다
    ("3~9주", 3, 9),  # 1주씩이라도 세 단계가 돈다
    ("10주+", 10, 10**9),
)

# 슬리피지를 가르는 시각. 개장 직후가 유독 나쁘다는 관찰에서 나왔다.
OPENING_UNTIL = "09:10"


@dataclass
class Bucket:
    """한 구간의 성적."""

    label: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    net: float = 0.0
    invested: float = 0.0
    mfe_sum: float = 0.0  # 최고 도달률 합 (평단 대비)
    mfe_count: int = 0

    @property
    def win_rate(self) -> float | None:
        decided = self.wins + self.losses
        return self.wins / decided if decided else None

    @property
    def rate(self) -> float | None:
        """투입 대비 세후 수익률."""
        return self.net / self.invested if self.invested else None

    @property
    def mfe(self) -> float | None:
        """평균 최고 도달률 — '얼마까지 갔었나'."""
        return self.mfe_sum / self.mfe_count if self.mfe_count else None


def _add(bucket: Bucket, entry: dict) -> None:
    net = (entry.get("realized_pnl") or 0) - (entry.get("fees") or 0)
    bucket.trades += 1
    bucket.net += net
    if net > 0:
        bucket.wins += 1
    elif net < 0:
        bucket.losses += 1
    avg, total = entry.get("avg_price") or 0, entry.get("total_bought") or 0
    bucket.invested += avg * total
    if avg and (high := entry.get("high_price") or 0):
        bucket.mfe_sum += (high - avg) / avg
        bucket.mfe_count += 1


def by_quantity(entries: list[dict]) -> list[Bucket]:
    """1차 수량 구간별 성적.

    가르는 기준을 '분할 익절이 도는가' 로 잡았다. 수량이 적을수록 성적이 나쁘다면
    종목당 금액을 키워야 한다는 뜻이고, 차이가 없다면 종목 수를 늘려도 된다는 뜻이다.
    """
    buckets = [Bucket(label) for label, _lo, _hi in QTY_BUCKETS]
    for entry in entries:
        if (entry.get("state") or "") != "종료":
            continue  # 끝나지 않은 매매는 성적을 말할 수 없다
        qty = entry.get("total_bought") or 0
        for bucket, (_label, lo, hi) in zip(buckets, QTY_BUCKETS):
            if lo <= qty <= hi:
                _add(bucket, entry)
                break
    return buckets


def by_tag(entries: list[dict], top: int = 5) -> list[Bucket]:
    """태그별 성적 — **어떤 근거로 고른 종목이 실제로 맞았나.**

    한 종목에 태그가 여럿이면 각 태그에 모두 센다. '이 태그가 붙은 매매의 성적' 을
    보려는 것이지 태그를 배타적으로 나누려는 것이 아니다.
    """
    buckets: dict[str, Bucket] = {}
    for entry in entries:
        if (entry.get("state") or "") != "종료":
            continue
        for tag in (t.strip() for t in (entry.get("tags") or "").split(",")):
            if tag:
                _add(buckets.setdefault(tag, Bucket(tag)), entry)
    return sorted(buckets.values(), key=lambda b: -b.trades)[:top]


# 1선 대비 평단 괴리 구간. 양수는 계획보다 비싸게 샀다는 뜻이다.
GAP_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("싸게 샀음", -10.0, -0.001),
    ("계획대로", -0.001, 0.005),
    ("+0.5~1%", 0.005, 0.01),
    ("+1~2%", 0.01, 0.02),
    ("+2% 넘음", 0.02, 10.0),
)


@dataclass
class GapBucket:
    """한 괴리 구간의 성적. **3%·5% 도달률**이 핵심이다."""

    label: str
    trades: int = 0
    reached3: int = 0
    reached5: int = 0
    mfe_sum: float = 0.0
    mfe_count: int = 0
    net: float = 0.0
    invested: float = 0.0

    @property
    def rate3(self) -> float | None:
        return self.reached3 / self.trades if self.trades else None

    @property
    def rate5(self) -> float | None:
        return self.reached5 / self.trades if self.trades else None

    @property
    def mfe(self) -> float | None:
        return self.mfe_sum / self.mfe_count if self.mfe_count else None

    @property
    def rate(self) -> float | None:
        return self.net / self.invested if self.invested else None


def by_entry_gap(rows: list[dict], targets=(0.03, 0.05)) -> list[GapBucket]:
    """1선 대비 평단 괴리 구간별 성적.

    답하려는 질문: **"계획보다 비싸게 산 매매는 3% 익절까지 잘 못 가는가."**
    익절선이 평단 기준이라 평단이 높으면 목표가도 함께 밀리는데, 그 직관이 실제
    기록에서도 보이는지 본다(2026-09-09).

    도달 여부는 최고 도달률(MFE)로 판정한다 — 실제로 팔았는지가 아니라 **닿을 수
    있었는지**를 묻는 것이기 때문이다. 본절 이탈로 먼저 나온 매매도 3% 를 찍었다면
    '도달' 로 센다.
    """
    buckets = [GapBucket(label) for label, _lo, _hi in GAP_BUCKETS]
    low, high = targets
    for row in rows:
        gap, mfe = row.get("entry_gap"), row.get("mfe")
        if gap is None:
            continue
        for bucket, (_label, lo, hi) in zip(buckets, GAP_BUCKETS):
            if lo <= gap < hi:
                bucket.trades += 1
                bucket.net += row.get("net") or 0
                bucket.invested += row.get("invested") or 0
                if mfe is not None:
                    bucket.mfe_sum += mfe
                    bucket.mfe_count += 1
                    bucket.reached3 += mfe >= low
                    bucket.reached5 += mfe >= high
                break
    return buckets


@dataclass
class Slip:
    """한 구간의 체결 오차."""

    label: str
    buys: list[float] = field(default_factory=list)
    sells: list[float] = field(default_factory=list)

    def _avg(self, values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    @property
    def buy(self) -> float | None:
        return self._avg(self.buys)

    @property
    def sell(self) -> float | None:
        return self._avg(self.sells)

    @property
    def count(self) -> int:
        return len(self.buys) + len(self.sells)

    @property
    def worst(self) -> float:
        """가장 불리했던 한 건. 매수는 +, 매도는 − 가 불리다."""
        return max([*self.buys, *(-s for s in self.sells)], default=0.0)


def by_opening(rows: list[dict], until: str = OPENING_UNTIL) -> list[Slip]:
    """개장 직후와 그 뒤의 체결 오차.

    지금까지 최악 네 건이 전부 09:01~09:05 였다. 이것이 사실로 굳으면 개장 직후 판정을
    몇 분 미루는 대응이 가능하다 — 전략 변경이라 신중해야 하고, 그래서 숫자가 먼저다.
    """
    early, late = Slip(f"09:00~{until}"), Slip(f"{until} 이후")
    for row in rows:
        gap = row.get("gap")
        if gap is None:
            continue
        clock = (row.get("ts") or "")[11:16]
        target = early if clock and clock < until else late
        (target.buys if row.get("side") == "매수" else target.sells).append(gap)
    return [early, late]
