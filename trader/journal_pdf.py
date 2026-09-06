"""매매일지 A4 PDF — 매매일지 3단계-4.

마크다운이 원본이고 PDF 는 **필요할 때 뽑는 출력물**이다. 그래서 평소 생성에서는
만들지 않고 `--pdf` 를 줄 때만 만든다. 매번 만들면 느리고, git 에 올리면 저장소가
그림 무게로 계속 커진다(`.gitignore` 대상).

레이아웃 — 가로 A4 한 장
------------------------
    ┌──────────────────────────────────────────────┐
    │ 희림(037440) · 익절              2026-09-03  │  헤더
    ├───────────────┬──────────────┬───────────────┤
    │               │              │  매매 정보     │
    │     일봉       │    3분봉      │  ─────────    │
    │               │              │  체결         │
    ├───────────────┴──────────────┴───────────────┤
    │ 잘한 점 …                                     │  푸터
    │ 아쉬운 점 …                                   │
    └──────────────────────────────────────────────┘

가로로 정한 이유는 **차트 두 장이 3:4 세로형**이기 때문이다. 세로 A4 에 좌우로 놓으면
폭이 상한이 되어 각 88mm 에 묶이고 아래가 40mm 비는데, 가로로 두면 108mm 까지 커지면서
남는 여백이 사라진다(2026-09-06 시안 비교).

체결 표는 4열이다. 판정가는 3선으로 차트에 이미 그려져 있어 숫자로 또 적을 이유가
없고, 열을 줄인 만큼 오른쪽 기둥이 좁아져 차트가 커진다. 오차는 체결가 옆에 괄호로
붙인다 — "얼마에 샀나" 와 "얼마나 밀렸나" 는 함께 읽는 값이다.

여백
----
위·아래 여백과 헤더↔본문·본문↔푸터 간격을 모두 같게 맞춘다. 푸터 높이를 넉넉히
잡아 두면 그 안의 빈 공간이 '본문↔푸터 간격' 처럼 보여 어긋나므로, **코멘트 높이를
먼저 재서** 배치하고 남는 세로는 차트가 흡수한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

from trader.journal import compact_timeline, cycle_holding, transition_path
from trader.journal_export import net_pnl, result_label, slippage_rows, trade_slug

MARGIN = 11.0  # mm — 상하좌우 동일
BLOCK = 9.0  # 헤더·본문·푸터 사이 간격
GAP = 5.0  # 차트 사이
SIDE = 58.0  # 오른쪽 기둥 폭
LINE = 4.6  # 코멘트 한 줄 높이

# 한글 폰트 후보. 윈도우에는 맑은 고딕이 기본으로 있어 별도 설치가 필요 없다.
# Noto CJK 는 PostScript 윤곽이라 reportlab 이 읽지 못하므로 후보에 넣지 않는다.
FONT_CANDIDATES = (
    (r"C:\Windows\Fonts\malgun.ttf", r"C:\Windows\Fonts\malgunbd.ttf"),
    (
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    ),
    ("/Library/Fonts/AppleSDGothicNeo.ttc", "/Library/Fonts/AppleSDGothicNeo.ttc"),
)


class PdfError(RuntimeError):
    """PDF 생성 실패 — 라이브러리나 폰트가 없을 때."""


def _mm(v: float) -> float:
    from reportlab.lib.units import mm as unit

    return v * unit


def register_fonts(regular: str = "", bold: str = "") -> tuple[str, str]:
    """(본문, 굵게) 폰트 이름. 지정이 없으면 설치된 후보에서 찾는다."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    # 설정한 것을 먼저 보되, 없거나 읽지 못하면 설치된 후보로 물러난다 — 경로 하나가
    # 틀렸다고 PDF 를 아예 못 만들면 곤란하다.
    pairs = list(FONT_CANDIDATES)
    if regular:
        pairs.insert(0, (regular, bold or regular))
    for reg, bld in pairs:
        if not Path(reg).exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont("JK", reg))
            pdfmetrics.registerFont(TTFont("JKB", bld if Path(bld).exists() else reg))
            return "JK", "JKB"
        except Exception:  # noqa: BLE001 — 다음 후보로 넘어간다
            continue
    raise PdfError(
        "한글 폰트를 찾지 못했습니다. 윈도우에서는 맑은 고딕이 기본으로 있고, "
        "리눅스에서는 `apt install fonts-nanum` 으로 설치하거나 "
        "config.toml 의 [pdf] font 에 .ttf 경로를 적어 주세요."
    )


def info_rows(entry: dict, cycle: list[dict], calendar=None) -> list[tuple[str, str]]:
    """오른쪽 기둥의 '매매 정보'. 값이 없는 항목은 넣지 않는다.

    당일 등락은 담지 않는다 — 여러 날에 걸친 매매에서는 청산일 하루치일 뿐이라
    그 매매의 성적으로 오해된다.
    """
    avg, total = entry.get("avg_price") or 0, entry.get("total_bought") or 0
    net = net_pnl(entry)
    rows: list[tuple[str, str]] = []
    if avg and total:
        rows.append(("평단 / 수량", f"{avg:,.0f}원 / {total}주"))
        rows.append(("실현손익 (세후)", f"{net:+,.0f}원 ({net / (avg * total):+.2%})"))
    else:
        rows.append(("실현손익 (세후)", f"{net:+,.0f}원"))
    high, low = entry.get("high_price") or 0, entry.get("low_price") or 0
    if avg and high:
        rows.append(
            ("최고 / 최저", f"{(high - avg) / avg:+.1%} / {(low - avg) / avg:+.1%}")
        )
    if held := cycle_holding(cycle, calendar):
        rows.append(("보유", held))
    if timeline := compact_timeline(cycle, entry.get("base_date") or "", calendar):
        rows.append(("시점", timeline))
    if path := transition_path(cycle):
        rows.append(("상태 경로", path))
    if tags := (entry.get("tags") or ""):
        rows.append(
            ("태그", " ".join(f"#{t.strip()}" for t in tags.split(",") if t.strip()))
        )
    if memo := (entry.get("memo") or ""):
        rows.append(("메모", memo))
    return rows


def fill_rows(cycle: list[dict]) -> list[tuple[str, str, str, str]]:
    """체결 표 4열 — (시각, 구분, 수량, 체결가(오차))."""
    out = []
    for row in cycle:
        if not row.get("side"):
            continue
        price = row.get("price") or 0
        gap = next((r["gap"] for r in slippage_rows([row])), None)
        text = f"{price:,.0f}" + (f" ({gap:+.2%})" if gap is not None else "")
        out.append(
            ((row.get("ts") or "")[11:16], row["side"], f"{row.get('qty', 0)}주", text)
        )
    return out


class _Sheet:
    """한 장을 그리는 도구. reportlab 캔버스를 감싸 mm 단위로 다룬다."""

    def __init__(self, canvas, font: str, bold: str):
        self.c = canvas
        self.font, self.bold = font, bold

    def text(self, x, y, s, size=8.2, bold=False, gray=0.15):
        self.c.setFillColorRGB(gray, gray, gray)
        self.c.setFont(self.bold if bold else self.font, size)
        self.c.drawString(_mm(x), _mm(y), s)

    def right(self, x, y, s, size=8.2, gray=0.35):
        self.c.setFillColorRGB(gray, gray, gray)
        self.c.setFont(self.font, size)
        self.c.drawRightString(_mm(x), _mm(y), s)

    def rule(self, x1, y, x2, gray=0.8):
        self.c.setStrokeColorRGB(gray, gray, gray)
        self.c.setLineWidth(0.4)
        self.c.line(_mm(x1), _mm(y), _mm(x2), _mm(y))

    def wrap(self, s: str, size: float, width_mm: float) -> list[str]:
        """폭에 맞춰 줄을 나눈다."""
        lines, cur = [], ""
        for token in s.split(" "):
            trial = f"{cur} {token}".strip()
            if self.c.stringWidth(trial, self.font, size) <= _mm(width_mm) or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = token
        if cur:
            lines.append(cur)
        return lines


def _comments_height(sheet: _Sheet, entry: dict, width: float, size: float) -> float:
    """코멘트 띠의 실제 높이(mm). **배치 전에 알아야** 위·아래 여백을 맞출 수 있다."""
    total = 0.0
    for key in ("good", "bad"):
        body = " ".join((entry.get(key) or "-").split("\n"))
        total += len(sheet.wrap(body, size, width - 20)) * LINE + 3
    return total - 3


def _draw_comments(
    sheet: _Sheet, entry: dict, x: float, y: float, width: float, size: float
):
    """y 는 첫 줄 기준선."""
    for label, key in (("잘한 점", "good"), ("아쉬운 점", "bad")):
        sheet.text(x, y, label, size=size, bold=True)
        body = " ".join((entry.get(key) or "-").split("\n"))
        lines = sheet.wrap(body, size, width - 20)
        for j, line in enumerate(lines):
            sheet.text(x + 20, y - j * LINE, line, size=size)
        y -= len(lines) * LINE + 3


def _draw_side(sheet: _Sheet, entry: dict, cycle: list, x: float, y: float, calendar):
    """오른쪽 기둥 — 매매 정보 + 체결."""
    size, line_h, label_w = 8.2, 6.4, 21.0
    sheet.text(x, y, "매매 정보", size=size + 0.5, bold=True)
    sheet.rule(x, y - 2.4, x + SIDE)
    y -= 7.5
    for label, value in info_rows(entry, cycle, calendar):
        sheet.text(x, y, label, size=size, gray=0.45)
        for j, line in enumerate(sheet.wrap(value, size, SIDE - label_w)):
            sheet.text(x + label_w, y - j * line_h, line, size=size)
        y -= len(sheet.wrap(value, size, SIDE - label_w)) * line_h

    fills = fill_rows(cycle)
    if not fills:
        return
    y -= BLOCK - 3
    size = 8.0 if len(fills) <= 12 else 7.0  # 많으면 글자를 줄여 다 담는다
    sheet.text(x, y, "체결", size=size + 0.5, bold=True)
    sheet.rule(x, y - 2.4, x + SIDE)
    y -= 7.5
    stops = (0.0, 0.26, 0.42, 0.58)
    for head, st in zip(("시각", "구분", "수량", "체결가 (오차)"), stops):
        sheet.text(x + SIDE * st, y, head, size=size - 0.5, gray=0.5)
    y -= 5.4
    for row in fills:
        for value, st in zip(row, stops):
            sheet.text(x + SIDE * st, y, value, size=size)
        y -= 5.6


def render_trade_pdf(
    path: Path, entry: dict, cycle: list[dict], charts: dict[str, str], calendar=None
) -> Path:
    """매매 한 건을 A4 가로 한 장으로."""
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.pdfgen import canvas as rl_canvas
    except ImportError as err:
        raise PdfError("reportlab 이 필요합니다 — `uv sync` 로 설치하세요.") from err

    font, bold = register_fonts(*_font_paths())
    page = landscape(A4)
    c = rl_canvas.Canvas(str(path), pagesize=page)
    sheet = _Sheet(c, font, bold)
    pw, ph = page[0] / _mm(1), page[1] / _mm(1)
    x, w = MARGIN, pw - 2 * MARGIN

    name, symbol = entry.get("name", ""), entry.get("symbol", "")
    hy = ph - MARGIN - 5
    sheet.text(x, hy, f"{name}({symbol})", size=13, bold=True, gray=0.1)
    head_w = c.stringWidth(f"{name}({symbol})", bold, 13) / _mm(1)
    sheet.text(x + head_w + 2, hy, f"· {result_label(entry)}", size=9.5, gray=0.35)
    sheet.right(x + w, hy, entry.get("trade_date", ""), size=9.5)
    sheet.rule(x, hy - 4.5, x + w, gray=0.7)
    top = hy - 4.5 - BLOCK

    foot_h = _comments_height(sheet, entry, w, 9.0)
    cw = (w - SIDE - GAP * 2) / 2
    ch = top - BLOCK - foot_h - MARGIN  # 남는 세로를 차트가 흡수한다

    for i, label in enumerate(("일봉", "3분봉")):
        if src := charts.get(label):
            c.drawImage(src, _mm(x + i * (cw + GAP)), _mm(top - ch), _mm(cw), _mm(ch))
    _draw_side(sheet, entry, cycle, x + 2 * (cw + GAP), top, calendar)

    # 푸터: 마지막 줄 기준선이 아래 여백 위에 오도록 한 줄 + 하강부만큼 보정한다.
    _draw_comments(sheet, entry, x, MARGIN + foot_h - LINE + 1, w, 9.0)
    c.showPage()
    c.save()
    return path


def _font_paths() -> tuple[str, str]:
    """config.toml 의 [pdf] font / font_bold. 없으면 빈 문자열."""
    import tomllib

    cfg = Path("config.toml")
    if not cfg.exists():
        return "", ""
    try:
        section = tomllib.loads(cfg.read_text(encoding="utf-8")).get("pdf", {})
    except (tomllib.TOMLDecodeError, OSError):
        return "", ""
    return str(section.get("font", "")), str(section.get("font_bold", ""))
