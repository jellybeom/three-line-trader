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

import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_UP = "#d32f2f"  # 양봉
_DOWN = "#1565c0"  # 음봉
_MAGENTA = "#ff00ff"  # 1·2·3선
_VALUE_FILL = "#7986cb"  # 거래대금 채움
_MA_COLORS = {5: "#000000", 10: "#1565c0", 20: "#d32f2f", 60: "#2e7d32", 120: "#ef6c00"}
# 계단 지표: **그날 장중 누적 최저가** 대비 %. 시장이 바닥에서 얼마나 올라왔는지를 본다.
# 익절 트리거(평단 대비 3/5/7%)와는 기준점이 다르다 — 숫자가 같아도 의미가 다르고,
# 평단이 당일 저가와 같을 때만 두 선이 겹친다.
# 10% 이상 선은 캔들에서 멀리 떨어져 y축을 위로 늘리고 캔들을 아래로 눌러 제외했다.
_STEP_PCTS = (0.03, 0.05, 0.07)

# 체결 화살표 배치 — 모든 값은 **포인트(pt)** 단위다.
# 데이터 좌표로 띄우면 y축 배율에 따라 간격이 들쭉날쭉해지고(가격이 비싼 종목일수록
# 붙어 보인다) 여백 계산도 불가능하다. 축 크기와 무관한 pt 오프셋으로 띄운다.
_MARKER_MAX_PT = 9.0  # 화살표 최대 크기 (일봉처럼 봉이 성길 때)
_MARKER_MIN_PT = 5.0  # 최소 크기 — 이보다 작으면 모양을 알아볼 수 없다
_MARKER_GAP_PT = 4.0  # 캔들 끝(고가/저가)에서 화살표 가장자리까지
_MARKER_SPACE_PT = 3.0  # 쌓인 화살표 사이 간격


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


def _bar_index(bars: list[Bar], ts: str, daily: bool) -> int | None:
    """체결 시각(YYYYMMDDHHMMSS)이 속한 봉의 위치. 표시 구간 밖이면 None."""
    if daily:
        day = ts[:8]
        return next((i for i, b in enumerate(bars) if b.key[:8] == day), None)
    for i, b in enumerate(bars):  # 봉 시작 시각 <= 체결 < 다음 봉 시작
        if b.key <= ts and (i + 1 == len(bars) or ts < bars[i + 1].key):
            return i
    return None


def marker_slots(
    bars: list[Bar], fills: list[Fill], daily: bool, group: int = 1
) -> list[tuple[int, str, int]]:
    """(봉 index, 매수/매도, 층수) 목록 — 같은 방향이 겹치면 0,1,2… 로 쌓는다.

    층수는 **발생 순서**다. 0층이 캔들에 가장 가깝고 바깥으로 갈수록 나중 체결이라,
    차수를 글자로 적지 않아도 위치만으로 1차·2차를 읽을 수 있다.

    group 은 '화살표 하나가 가로로 덮는 봉 수' 다. 3분봉처럼 봉이 촘촘하면 이웃 봉의
    체결이라도 화살표끼리 가로로 겹쳐 개수를 셀 수 없으므로(3건이 2건처럼 보인다),
    그 범위 안이면 같은 칸으로 보고 세로로 쌓는다. 1이면 같은 봉만 쌓는다.
    """
    slots: list[tuple[int, str, int]] = []
    placed: dict[str, list[tuple[int, int]]] = {}  # 방향별 (봉 index, 층수)
    for f in sorted(fills, key=lambda x: x.ts):
        ts = f.ts.replace("-", "").replace(":", "").replace(" ", "")  # YYYYMMDDHHMMSS
        idx = _bar_index(bars, ts, daily)
        if idx is None:
            continue
        near = [lv for i, lv in placed.get(f.side, []) if abs(i - idx) < group]
        level = max(near) + 1 if near else 0
        placed.setdefault(f.side, []).append((idx, level))
        slots.append((idx, f.side, level))
    return slots


def _pt_per_bar(ax) -> float:
    """봉 하나가 차지하는 가로 폭 (pt)."""
    lo, hi = ax.get_xlim()
    width_pt = ax.get_position().width * ax.figure.get_figwidth() * 72.0
    return width_pt / max(hi - lo, 1.0)


def marker_size(ax) -> float:
    """봉 간격에 맞춘 화살표 크기.

    3분봉은 하루 130봉이라 고정 크기로 그리면 화살표가 **옆 캔들 위로** 삐져나온다.
    다만 무한정 줄이면 모양을 알아볼 수 없으므로 최소 크기에서 멈추고, 남는 겹침은
    기준점을 국소 극값으로 잡아(_marker_anchor) 해결한다.
    """
    return max(_MARKER_MIN_PT, min(_MARKER_MAX_PT, _pt_per_bar(ax)))


def _anchor_half(ax, size: float) -> int:
    """화살표가 좌우 몇 봉을 덮는지 — 기준점을 찾을 범위.

    **반드시 올림이어야 한다.** 내림하면 화살표 폭이 1.2봉일 때 half=1 이 되어,
    실제로는 옆 봉을 덮는데 기준점 계산에서는 그 봉을 빼먹는다. 하락 추세에서
    옆 봉의 저가가 더 낮으면 화살표가 그 캔들 위에 그대로 얹힌다(2026-08-11 실측).
    """
    per_bar = _pt_per_bar(ax)
    if per_bar <= 0:
        return 0
    return min(4, math.ceil(size / per_bar / 2))


def _marker_anchor(bars: list[Bar], idx: int, buy: bool, half: int) -> float:
    """화살표를 띄울 기준 가격 — 자기 봉이 아니라 **좌우 half 봉의 국소 극값**.

    봉 간격이 화살표 폭보다 좁으면 자기 봉의 고가만 기준 삼아도 옆 캔들 꼬리를 덮는다.
    이웃까지 포함한 극값을 쓰면 어떤 캔들도 가리지 않는다. half=0 이면 자기 봉 기준.
    """
    window = bars[max(0, idx - half) : min(len(bars), idx + half + 1)]
    return min(b.low for b in window) if buy else max(b.high for b in window)


def _marker_offset(size: float, level: int) -> float:
    """기준 가격에서 level 층 화살표 **중심**까지의 거리 (pt)."""
    return _MARKER_GAP_PT + size / 2 + (size + _MARKER_SPACE_PT) * level


def _expand_ylim(ax, needs: list[tuple[float, bool, float]]) -> None:
    """화살표가 잘리지 않도록 y 범위를 넓힌다.

    needs 는 (기준가, 매수여부, 기준가에서 화살표 끝까지 필요한 pt) 목록이다.
    여백은 pt 고정인데 범위를 넓히면 'pt 당 가격' 도 같이 커지므로 한 번의 덧셈으로는
    답이 안 나온다. hi' ≥ a + R·S'/H 를 만족하는 S' 를 몇 번 반복해 수렴시킨다
    (단조 증가라 3회면 충분하다).

    필요 없는 쪽은 넓히지 않는다 — 화살표가 이미 차트 안쪽이면 괜히 캔들을 누를 이유가 없다.
    """
    if not needs:
        return
    height_pt = ax.get_position().height * ax.figure.get_figheight() * 72.0
    lo, hi = ax.get_ylim()
    if height_pt <= 0 or hi <= lo:
        return
    span, new_lo, new_hi = hi - lo, lo, hi
    for _ in range(3):
        new_hi = max([hi] + [a + r * span / height_pt for a, b, r in needs if not b])
        new_lo = min([lo] + [a - r * span / height_pt for a, b, r in needs if b])
        grown = new_hi - new_lo
        if grown > (hi - lo) * 2.5:  # 과도한 확장 방지 — 캔들이 뭉개진다
            break
        span = grown
    ax.set_ylim(new_lo, new_hi)


def _fill_markers(ax, bars: list[Bar], fills: list[Fill], daily: bool) -> None:
    """매수 ▲ 빨강(캔들 아래) / 매도 ▼ 파랑(캔들 위).

    예전에는 **실제 체결 가격**에 찍었는데(2026-07-27), 매수 1회 + 익절 3회가 같은 날
    나면 화살표가 캔들을 통째로 덮어 봉이 보이지 않았다(2026-08-11 피드백). 지금은
    캔들의 고가·저가 바깥으로 빼고, 같은 봉에 같은 방향이 여럿이면 위아래로 쌓는다.
    체결 가격은 잃지만 3선·계단 지표로 가격대를 읽을 수 있고, 몇 번째 매수·매도인지는
    쌓인 순서(안쪽이 먼저)로 알 수 있다.
    """
    from matplotlib.transforms import offset_copy

    size = marker_size(ax)
    half = _anchor_half(ax, size)
    slots = marker_slots(bars, fills, daily, group=max(1, half * 2 + 1))
    if not slots:
        return
    placed = [
        (
            idx,
            side,
            _marker_anchor(bars, idx, side == "매수", half),
            _marker_offset(size, level),
        )
        for idx, side, level in slots
    ]
    _expand_ylim(ax, [(a, s == "매수", gap + size / 2) for _, s, a, gap in placed])
    for idx, side, anchor, gap in placed:
        buy = side == "매수"
        marker, color = ("^", _UP) if buy else ("v", _DOWN)
        trans = offset_copy(
            ax.transData, fig=ax.figure, y=(-gap if buy else gap), units="points"
        )
        ax.plot(
            [idx],
            [anchor],
            marker=marker,
            linestyle="none",
            transform=trans,
            markersize=size,
            markerfacecolor="white",  # 속을 비워 캔들·격자와 톤을 맞춘다
            markeredgecolor=color,
            markeredgewidth=1.5 if size >= 7 else 1.0,
            zorder=7,
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
