"""복기 차트 렌더링 — PNG 파일 생성 전용 (화면 표시는 UI 가 담당).

성능 원칙:
- matplotlib 은 **이 모듈 함수가 처음 불릴 때** import 한다 (프로그램 시작 지연 방지).
- Agg(무화면) 백엔드 고정 — GUI 스레드 제약이 없어 코어의 백그라운드 스레드에서 안전.
- 그림 객체는 매번 close 해 메모리 누수를 막는다.
- 입력은 순수 데이터(Bar 목록·체결 목록)라 API 없이 단위 테스트할 수 있다.

색 규칙(요구사항): 양봉 빨강 / 음봉 파랑, 캔들·거래량은 속 비움(hollow),
거래대금은 단색 채움, 3선은 마젠타 굵은 수평선, 진입 ▲ 빨강 / 청산 ▼ 파랑.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_UP = "#d32f2f"  # 양봉
_DOWN = "#1565c0"  # 음봉
_MAGENTA = "#ff00ff"  # 1·2·3선
_VALUE_FILL = "#7986cb"  # 거래대금 채움
_MA_COLORS = {5: "#000000", 10: "#1565c0", 20: "#d32f2f", 60: "#2e7d32", 120: "#ef6c00"}
# 바닥 대비 % 계단 지표 — 익절 트리거(3/5/7%)와 같은 값만 둔다.
# 10% 이상 선은 캔들에서 멀리 떨어져 y축을 위로 늘리고, 그만큼 캔들이 아래로 눌린다.
_STEP_PCTS = (0.03, 0.05, 0.07)


@dataclass(frozen=True)
class Bar:
    """봉 하나. key 는 일봉이면 YYYYMMDD, 분봉이면 YYYYMMDDHHMMSS."""

    key: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    value: float = 0.0  # 거래대금


@dataclass(frozen=True)
class Fill:
    """체결 마커용 — store.daily_report 의 체결 행에서 만든다."""

    ts: str  # "YYYY-MM-DD HH:MM:SS"
    side: str  # 매수 / 매도
    price: float


def _setup_matplotlib(font: str | None):
    """지연 import + Agg 백엔드 + 한글 폰트 체인. plt 모듈을 돌려준다."""
    import matplotlib

    matplotlib.use("Agg")  # 무화면 — 어떤 스레드에서든 안전
    from matplotlib import font_manager, pyplot as plt

    installed = {f.name for f in font_manager.fontManager.ttflist}
    chain = [
        font,
        "Malgun Gothic",
        "NanumGothic",
        "Noto Sans KR",
        "Pretendard",
        "NanumBarunGothic",
        "AppleGothic",
    ]
    for name in chain:
        if name and name in installed:
            matplotlib.rcParams["font.family"] = name
            break
    matplotlib.rcParams["axes.unicode_minus"] = False  # 마이너스 부호 깨짐 방지
    return plt


def sma(values: list[float], n: int) -> list[float | None]:
    """단순이동평균 — 데이터가 n 개 미만인 구간은 None (선을 그리지 않음)."""
    out: list[float | None] = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    total = sum(values[:n])
    out[n - 1] = total / n
    for i in range(n, len(values)):
        total += values[i] - values[i - n]
        out[i] = total / n
    return out


def day_low_steps(bars: list[Bar]) -> list[float]:
    """분봉의 '그날 장중 누적 최저가' — 날짜가 바뀌면 리셋되는 계단의 바닥값."""
    lows: list[float] = []
    current_day, day_low = "", float("inf")
    for b in bars:
        day = b.key[:8]
        if day != current_day:
            current_day, day_low = day, b.low
        else:
            day_low = min(day_low, b.low)
        lows.append(day_low)
    return lows


def _unit(max_value: float) -> tuple[float, str]:
    """축 단위 자동 산정 — (나눗수, 단위 이름). 예: 2.9e11 → (1e8, "억")."""
    for divisor, name in ((1e12, "조"), (1e8, "억"), (1e4, "만"), (1e3, "천")):
        if max_value >= divisor * 10:  # 눈금이 최소 두 자리쯤 되도록
            return divisor, name
    return 1.0, ""


def _last_value_tag(ax, x: int, value: float, color: str, decimals: int = 0) -> None:
    """패널 오른쪽 끝에 현재가(마지막 값) 표시 — HTS 의 가격 태그처럼."""
    ax.annotate(
        f"{value:,.{decimals}f}",
        xy=(x, value),
        xytext=(6, 0),
        textcoords="offset points",
        va="center",
        fontsize=7.5,
        color="white",
        bbox={"boxstyle": "round,pad=0.25", "facecolor": color, "edgecolor": "none"},
        zorder=6,
        annotation_clip=False,
    )


def _comma_axis(ax) -> None:
    from matplotlib.ticker import FuncFormatter

    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))


def _candles(ax, bars: list[Bar]) -> None:
    """속 비운 캔들. 봉 수가 많아도 Rectangle 수백 개 수준이라 렌더링 부담이 작다."""
    from matplotlib.patches import Rectangle

    width = 0.6
    for i, b in enumerate(bars):
        color = _UP if b.close >= b.open else _DOWN
        body_low, body_high = min(b.open, b.close), max(b.open, b.close)
        # 심지는 몸통 위·아래 두 구간으로 나눠 긋는다 — 몸통이 투명(hollow)이라
        # 한 줄로 그으면 몸통 안을 관통하는 세로선이 비쳐 보인다.
        if b.low < body_low:
            ax.plot([i, i], [b.low, body_low], color=color, linewidth=0.7, zorder=1)
        if b.high > body_high:
            ax.plot([i, i], [body_high, b.high], color=color, linewidth=0.7, zorder=1)
        height = max(body_high - body_low, (b.high - b.low) * 0.001 or 0.01)
        ax.add_patch(
            Rectangle(
                (i - width / 2, body_low),
                width,
                height,
                facecolor="none",
                edgecolor=color,
                linewidth=0.9,
                zorder=2,
            )
        )
    ax.set_xlim(-1, len(bars) + max(2, int(len(bars) * 0.01)))


def _hollow_bars(ax, bars: list[Bar], heights: list[float]) -> None:
    from matplotlib.patches import Rectangle

    for i, (b, h) in enumerate(zip(bars, heights)):
        color = _UP if b.close >= b.open else _DOWN
        ax.add_patch(
            Rectangle(
                (i - 0.3, 0), 0.6, h, facecolor="none", edgecolor=color, linewidth=0.8
            )
        )
    ax.set_xlim(-1, len(bars) + max(2, int(len(bars) * 0.01)))
    ax.set_ylim(0, max(heights) * 1.1 if heights and max(heights) > 0 else 1)


def _hlines(ax, lines: tuple[float, float, float]) -> None:
    for level in lines:  # 캔들보다 위에 보이도록 zorder 를 높인다 (캔들 1~2, 이평 3)
        ax.axhline(level, color=_MAGENTA, linewidth=1.8, zorder=4)
        ax.annotate(
            f"{level:,.0f}",
            xy=(1.0, level),
            xycoords=ax.get_yaxis_transform(),
            xytext=(6, 0),
            textcoords="offset points",
            va="center",
            fontsize=7,
            color="white",
            zorder=6,
            annotation_clip=False,
            bbox={
                "boxstyle": "round,pad=0.2",
                "facecolor": _MAGENTA,
                "edgecolor": "none",
            },
        )


def _fill_markers(ax, bars: list[Bar], fills: list[Fill], daily: bool) -> None:
    """진입 ▲ / 청산 ▼ — 체결 시각이 속한 봉 위치에 표시한다."""
    for f in fills:
        ts = f.ts.replace("-", "").replace(":", "").replace(" ", "")  # YYYYMMDDHHMMSS
        idx = None
        if daily:
            day = ts[:8]
            idx = next((i for i, b in enumerate(bars) if b.key[:8] == day), None)
        else:
            for i, b in enumerate(bars):  # 봉 시작 시각 <= 체결 < 다음 봉 시작
                if b.key <= ts and (i + 1 == len(bars) or ts < bars[i + 1].key):
                    idx = i
                    break
        if idx is None:
            continue
        # 마커는 **실제 체결 가격**에 찍는다. 봉 위/아래로 띄우면 진입·청산 가격이
        # 비슷해도 화살표가 멀리 떨어져 큰 손익이 난 것처럼 보인다(2026-07-27 피드백).
        marker, color = ("^", _UP) if f.side == "매수" else ("v", _DOWN)
        ax.plot(
            idx,
            f.price,
            marker=marker,
            color=color,
            markersize=8,
            markeredgecolor="white",
            markeredgewidth=0.8,
            zorder=6,
        )


def _apply_xticks(axes, ticks: list[int], labels: list[str]) -> None:
    """모든 패널에 **같은 x 눈금**을 적용한다 (라벨은 맨 아래 패널만).

    패널마다 눈금이 다르면 세로 격자가 어긋나, 캔들이 몇 시쯤인지 아래 거래량과
    대조할 수 없다(2026-08-07 피드백). 눈금을 공유하면 격자가 한 줄로 이어진다.
    """
    for i, axis in enumerate(axes):
        axis.set_xticks(ticks)
        if i == len(axes) - 1:
            axis.set_xticklabels(labels, fontsize=6.5)
        else:
            axis.set_xticklabels([])


def _style(ax, show_x: bool = False) -> None:
    ax.grid(True, linewidth=0.3, alpha=0.4)
    ax.tick_params(labelsize=7)
    if not show_x:
        ax.tick_params(labelbottom=False)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


def render_daily(
    path: str | Path,
    title: str,
    bars: list[Bar],
    lines: tuple[float, float, float],
    fills: list[Fill],
    kospi: list[Bar] | None = None,
    font: str | None = None,
    show: int = 60,
) -> str:
    """일봉 복기 차트 (세로 3:4, 960×1280).

    패널: 일봉 50% / 거래량 5% / 거래대금 10% / KOSPI 35%.
    bars 는 이동평균 계산을 위해 show(60)개보다 길게 받아 마지막 show 개만 그린다.
    """
    plt = _setup_matplotlib(font)
    visible = bars[-show:]
    offset = len(bars) - len(visible)
    closes_all = [b.close for b in bars]

    fig, axes = plt.subplots(
        4,
        1,
        figsize=(9.6, 12.8),
        dpi=100,
        sharex=False,
        gridspec_kw={"height_ratios": [50, 5, 10, 35], "hspace": 0.06},
    )
    ax_main, ax_vol, ax_val, ax_kospi = axes

    _candles(ax_main, visible)
    for (
        n,
        color,
    ) in _MA_COLORS.items():  # 표시 구간보다 긴 원본으로 계산해 왼쪽 끝도 정확
        line = sma(closes_all, n)[offset:]
        xs = [i for i, v in enumerate(line) if v is not None]
        if xs:
            ax_main.plot(
                xs, [line[i] for i in xs], color=color, linewidth=0.9, zorder=3
            )
    _hlines(ax_main, lines)
    _fill_markers(ax_main, visible, fills, daily=True)
    last = visible[-1]
    _last_value_tag(
        ax_main, len(visible) - 1, last.close, _UP if last.close >= last.open else _DOWN
    )
    ax_main.set_ylabel("원", fontsize=7)
    _comma_axis(ax_main)
    ax_main.set_title(title, fontsize=11, loc="left")
    _style(ax_main)

    vol_div, vol_unit = _unit(max(b.volume for b in visible))
    vols = [b.volume / vol_div for b in visible]
    _hollow_bars(ax_vol, visible, vols)
    _last_value_tag(
        ax_vol, len(visible) - 1, vols[-1], _UP if last.close >= last.open else _DOWN
    )
    ax_vol.set_ylabel(f"거래량({vol_unit}주)", fontsize=7)
    _comma_axis(ax_vol)
    _style(ax_vol)

    val_div, val_unit = _unit(max(b.value for b in visible) or 1)
    vals = [b.value / val_div for b in visible]
    ax_val.bar(
        range(len(visible)), vals, width=0.6, color=_VALUE_FILL
    )  # 거래대금만 채움
    ax_val.set_xlim(-1, len(visible) + 2)
    _last_value_tag(ax_val, len(visible) - 1, vals[-1], _VALUE_FILL)
    ax_val.set_ylabel(f"거래대금({val_unit}원)", fontsize=7)
    _comma_axis(ax_val)
    _style(ax_val)

    if kospi:
        k_visible = kospi[-show:]
        k_offset = len(kospi) - len(k_visible)
        _candles(ax_kospi, k_visible)
        k_closes = [b.close for b in kospi]
        for n, color in {10: _MA_COLORS[10], 20: _MA_COLORS[20]}.items():
            line = sma(k_closes, n)[k_offset:]  # 표시 구간 기준으로 잘라 캔들과 정렬
            xs = [i for i, v in enumerate(line) if v is not None]
            if xs:
                ax_kospi.plot(
                    xs, [line[i] for i in xs], color=color, linewidth=0.9, zorder=3
                )
        k_last = k_visible[-1]
        _last_value_tag(
            ax_kospi,
            len(k_visible) - 1,
            k_last.close,
            _UP if k_last.close >= k_last.open else _DOWN,
            decimals=2,
        )
        ax_kospi.set_ylabel("KOSPI(pt)", fontsize=8)
        _comma_axis(ax_kospi)
    else:
        ax_kospi.text(
            0.5,
            0.5,
            "KOSPI 데이터 없음",
            transform=ax_kospi.transAxes,
            ha="center",
            fontsize=9,
            color="gray",
        )
    # 월 경계 + 그 사이 보조 눈금 — 네 패널이 같은 격자를 쓰도록 공유한다
    month_starts = [
        i
        for i in range(1, len(visible))
        if visible[i].key[4:6] != visible[i - 1].key[4:6]
    ]
    # 월 경계를 우선 배치하고, 그 사이가 넓으면 중간 눈금을 채운다.
    # 라벨이 겹치지 않도록 최소 간격을 둔다.
    marks: dict[int, str] = {
        i: f"{visible[i].key[:4]}-{visible[i].key[4:6]}" for i in month_starts
    }
    for i in range(0, len(visible), 5):
        if all(abs(i - m) >= 5 for m in marks):
            marks[i] = f"{visible[i].key[4:6]}/{visible[i].key[6:8]}"
    ticks = sorted(marks)
    labels = [marks[i] for i in ticks]
    panels = (ax_main, ax_vol, ax_val, ax_kospi)
    _apply_xticks(panels, ticks, labels)
    for axis in panels:  # 월 경계는 더 진하게 — 달이 바뀌는 지점을 한눈에
        for i in month_starts:
            axis.axvline(i - 0.5, color="gray", linewidth=0.8, alpha=0.6, zorder=0)
    _style(ax_kospi, show_x=True)

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)  # 메모리 해제 — 반복 생성 시 누수 방지
    return str(path)


def render_minute(
    path: str | Path,
    title: str,
    bars: list[Bar],
    lines: tuple[float, float, float],
    fills: list[Fill],
    font: str | None = None,
) -> str:
    """3분봉 복기 차트 (가로형, 봉 수에 따라 폭 자동 조절).

    바닥 대비 % 계단(검정): 그날 누적 최저가 × (1+3/5/7/10/15/20%) — 날마다 리셋.
    """
    plt = _setup_matplotlib(font)
    fig, (ax, ax_vol) = plt.subplots(
        2,
        1,
        figsize=(9.6, 12.8),
        dpi=100,  # 일봉과 같은 3:4 세로형
        gridspec_kw={"height_ratios": [85, 15], "hspace": 0.05},
    )

    _candles(ax, bars)
    last = bars[-1]
    _last_value_tag(
        ax, len(bars) - 1, last.close, _UP if last.close >= last.open else _DOWN
    )
    ax.set_ylabel("원", fontsize=7)
    _comma_axis(ax)
    lows = day_low_steps(bars)
    xs = range(len(bars))
    for pct in _STEP_PCTS:  # 계단 지표 — steps-post 로 각지게
        ax.plot(
            xs,
            [low * (1 + pct) for low in lows],
            color="black",
            linewidth=0.7,
            drawstyle="steps-post",
            zorder=2,
        )
    _hlines(ax, lines)
    _fill_markers(ax, bars, fills, daily=False)

    day_starts = [
        i for i in range(1, len(bars)) if bars[i].key[:8] != bars[i - 1].key[:8]
    ]
    for i in day_starts:  # 날짜 경계선 (점선으로 구분)
        ax.axvline(i - 0.5, color="gray", linewidth=0.8, linestyle="--", alpha=0.7)
        ax_vol.axvline(i - 0.5, color="gray", linewidth=0.8, linestyle="--", alpha=0.7)
    ax.set_title(title, fontsize=11, loc="left")
    _style(ax)

    mvol_div, mvol_unit = _unit(max(b.volume for b in bars))
    mvols = [b.volume / mvol_div for b in bars]
    _hollow_bars(ax_vol, bars, mvols)
    _last_value_tag(
        ax_vol, len(bars) - 1, mvols[-1], _UP if last.close >= last.open else _DOWN
    )
    ax_vol.set_ylabel(f"거래량({mvol_unit}주)", fontsize=7)
    _comma_axis(ax_vol)
    # 정시마다 눈금 — 캔들이 몇 시쯤인지 아래 거래량과 나란히 읽을 수 있게 한다
    hour_marks = {
        i: bars[i].key[8:10]
        for i in range(len(bars))
        if bars[i].key[10:12] in ("00", "01", "02")
        and (i == 0 or bars[i].key[8:10] != bars[i - 1].key[8:10])
    }
    day_marks = {i: f"{bars[i].key[4:6]}/{bars[i].key[6:8]}" for i in [0, *day_starts]}
    marks = {**hour_marks, **day_marks}  # 날짜 경계는 날짜로 표기
    ticks = sorted(marks)
    _apply_xticks((ax, ax_vol), ticks, [marks[i] for i in ticks])
    _style(ax_vol, show_x=True)

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return str(path)
