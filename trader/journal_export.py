"""매매일지 마크다운 생성기 (매매일지 3단계 · 1단계).

DB 에 있는 것을 읽어 **문서로 뱉기만 한다.** 매매 프로그램의 판단이나 상태에 손대지
않으므로 언제 몇 번을 돌려도 안전하다.

왜 마크다운인가
---------------
PDF 를 원본으로 삼으면 검색이 안 되고, 고치면 통째로 다시 만들어야 하고, git 이 변경
이력을 남길 수 없고, 나중에 집계 스크립트를 붙일 수 없다. 마크다운은 폰·PC·Obsidian·
GitHub 어디서나 열리고, A4 PDF 는 필요할 때 여기서 렌더하면 된다(4단계). 반대 방향은
불가능하다 — 그래서 원본은 마크다운이다.

문서는 **순수 생성물**이다. 사람이 직접 고치지 않는다는 전제라 매번 통째로 덮어써도
안전하고, 원본은 늘 DB 다. 사람이 쓴 것(잘한 점·아쉬운 점)은 UI 나 Discord 로 DB 에
들어가고, 다음 생성 때 문서에 실린다.

담지 않는 것
------------
**예수금과 계좌 평가액은 싣지 않는다.** private repo 라도 남의 서버에 올라가는 문서다.
종목·손익액·손익률은 남긴다 — 그게 없으면 복기가 되지 않는다. 계좌 규모는 복기에
필요하지 않으므로 애초에 조회하지 않는다.

파일 구조
---------
    journal/
      2026-08/
        2026-08-21.md              ← 그날 인덱스
        2026-08-21/
          263800-데이타솔루션.md          ← 매매 1건 = 파일 1개 = 나중에 A4 1장
          263800-데이타솔루션-daily.png   ← 일봉 (복사해 온 것)
          263800-데이타솔루션-minute.png  ← 3분봉

매매 1건이 파일 1개라 검색·링크·개별 참조가 자연스럽고, 4단계에서 A4 한 장으로
그대로 떨어진다. 날짜는 **청산일**(사이클이 끝난 날) 기준이다 — 이월된 매매를 진입일에
두면 며칠 뒤에야 완성되는 문서가 되어 그날 폴더를 다시 열어야 한다.
"""

from __future__ import annotations

import filecmp
import re
import shutil
from pathlib import Path

from trader.journal import cycle_holding, cycle_timeline, transition_path
from trader.trading_calendar import TradingCalendar, format_days

_BANNED = re.compile(r'[\\/:*?"<>|]')  # 윈도우에서 파일명에 못 쓰는 글자

# (문서에 적을 이름, journal 테이블 컬럼, 파일명 접미사). 순서가 문서에 실리는 순서다 —
# **일봉 먼저**다. "3선을 어디에 그었나" 를 보고 나서 "그래서 어떻게 체결됐나" 를 본다.
_CHARTS = (("일봉", "daily_path", "daily"), ("3분봉", "minute_path", "minute"))


def net_pnl(entry: dict) -> float:
    """세후 실현손익. 화면·문서가 같은 값을 쓰도록 정의를 한 곳에 둔다."""
    return (entry.get("realized_pnl") or 0) - (entry.get("fees") or 0)


def safe_name(text: str) -> str:
    """파일명으로 쓸 수 있게 다듬는다.

    종목명에 슬래시나 물음표가 들어가는 일은 드물지만, 한 종목 때문에 그날 생성이
    통째로 실패하면 곤란하다. 공백은 밑줄로 바꿔 셸에서 다루기 쉽게 한다.
    """
    return _BANNED.sub("_", text).strip().replace(" ", "_") or "unknown"


def trade_slug(entry: dict) -> str:
    """`263800-데이타솔루션` — 파일명 겸 문서 사이 링크에 쓰는 식별자."""
    return f"{entry.get('symbol', '')}-{safe_name(entry.get('name', ''))}"


def result_label(entry: dict) -> str:
    """익절 / 손절 / 본전 / 보유 중 — 폴더를 정렬만 해도 결과가 모이도록."""
    if (entry.get("state") or "") != "종료":
        return "보유 중"
    net = net_pnl(entry)
    return "익절" if net > 0 else "손절" if net < 0 else "본전"


# ── 지표 계산 ───────────────────────────────────────────────────


def metrics(entry: dict, cycle: list[dict]) -> list[tuple[str, str]]:
    """(라벨, 값) 목록 — 문서 상단 표에 그대로 들어간다.

    값이 없는 항목은 아예 빼서 `-` 로 채운 빈 줄이 생기지 않게 한다. 이월 종목이라
    당일 등락이 없거나, 기준봉을 안 적은 종목이 흔하다.
    """
    avg = entry.get("avg_price") or 0
    total = entry.get("total_bought") or 0
    net = net_pnl(entry)
    rows: list[tuple[str, str]] = [("결과", result_label(entry))]
    if avg and total:
        rows.append(("평단 / 수량", f"{avg:,.0f}원 · {total}주"))
        rows.append(("투입", f"{avg * total:,.0f}원"))
        rows.append(("세후 손익", f"{net:+,.0f}원 ({net / (avg * total):+.2%})"))
    else:
        rows.append(("세후 손익", f"{net:+,.0f}원"))

    high, low = entry.get("high_price") or 0, entry.get("low_price") or 0
    if avg and high:
        # MFE/MAE. "얼마까지 갔는데 얼마 먹었나" 가 익절 비중 조정의 근거고,
        # "얼마나 밀렸다 왔나" 가 3선 간격 조정의 근거다.
        rows.append(
            ("최고 / 최저", f"{(high - avg) / avg:+.1%} / {(low - avg) / avg:+.1%}")
        )
    opened, closed = entry.get("day_open") or 0, entry.get("day_close") or 0
    if opened and closed:
        rows.append(("당일 등락", f"{(closed - opened) / opened:+.2%}"))
    if held := cycle_holding(cycle):
        rows.append(("보유기간", held))
    return rows


def line_table(entry: dict) -> list[tuple[str, str]]:
    """3선 값. 사람이 판단한 유일한 지점이라 따로 떼어 눈에 띄게 둔다."""
    rows = []
    for label, key in (("1선", "line1"), ("2선", "line2"), ("3선", "line3")):
        if value := entry.get(key) or 0:
            rows.append((label, f"{value:,.0f}원"))
    return rows


def slippage_rows(fills: list[dict]) -> list[dict]:
    """판정가 대비 체결가 오차.

    시장가 주문이라 판정한 값과 실제 체결가가 늘 어긋난다. 2026-08-21 실측으로 매수는
    평균 +0.50%, 익절 매도는 −1.01% 였다 — 왕복 1.5% 면 1차 익절 3% 의 절반이다.
    3선 설정만큼 성적에 영향을 주는 값인데 여태 로그에만 있었다.
    """
    rows = []
    for f in fills:
        trigger, price = f.get("trigger_price") or 0, f.get("price") or 0
        if not (trigger and price):
            continue
        rows.append({**f, "gap": (price - trigger) / trigger})
    return rows


# ── 마크다운 조립 ───────────────────────────────────────────────


def _table(rows: list[tuple[str, str]]) -> str:
    if not rows:
        return ""
    body = "\n".join(f"| {label} | {value} |" for label, value in rows)
    return f"| | |\n|---|---|\n{body}\n"


def render_trade(
    entry: dict,
    cycle: list[dict],
    calendar: TradingCalendar | None = None,
    charts: dict[str, str] | None = None,
) -> str:
    """매매 1건 문서. A4 한 장에 들어가도록 항목을 절제한다.

    charts 는 `{"일봉": 파일명, "3분봉": 파일명}`. **둘 다 싣는다** — 일봉은 3선을 어디에
    그었는지(선정 근거)를, 3분봉은 그날 어떻게 체결됐는지(실행)를 보여준다. 복기의 질문이
    서로 달라 한쪽만으로는 답이 나오지 않는다.

    차트는 파일명만 상대 경로로 걸어 둔다 — 문서와 같은 폴더에 복사되므로 GitHub·
    Obsidian·PDF 어디서 열어도 그림이 따라온다.
    """
    name = entry.get("name", "")
    symbol = entry.get("symbol", "")
    date = entry.get("trade_date", "")
    parts = [f"# {name}({symbol}) · {date}", ""]

    if path := transition_path(cycle):
        parts += [f"**{path}**", ""]
    if timeline := cycle_timeline(cycle, entry.get("base_date") or "", calendar):
        parts += [timeline, ""]

    parts += [_table(metrics(entry, cycle))]

    if lines := line_table(entry):
        parts += ["## 3선", "", _table(lines)]
        if base := (entry.get("base_date") or ""):
            note = f"기준봉 {base}"
            if calendar is not None and date:
                if days := format_days(calendar.days_between(base, date)):
                    note += f" ({days})"
            parts += [note, ""]
    if tags := (entry.get("tags") or ""):
        parts += [" ".join(f"`#{t}`" for t in tags.split(",") if t), ""]
    if memo := (entry.get("memo") or ""):
        parts += [f"> {memo}", ""]

    if shown := [(label, f) for label, f in (charts or {}).items() if f]:
        parts += ["## 차트", ""]
        for label, filename in shown:
            parts += [f"**{label}**", "", f"![{name} {label}]({filename})", ""]

    if slips := slippage_rows(
        [r for r in cycle if r.get("side") and r.get("trigger_price")]
    ):
        parts += [
            "## 체결",
            "",
            "| 시각 | 구분 | 수량 | 판정가 | 체결가 | 오차 |",
            "|---|---|---|---|---|---|",
        ]
        for r in slips:
            parts.append(
                f"| {(r.get('ts') or '')[11:16]} | {r.get('side', '')}"
                f" | {r.get('qty', 0)}주 | {r.get('trigger_price', 0):,.0f}"
                f" | {r.get('price', 0):,.0f} | {r['gap']:+.2%} |"
            )
        parts.append("")

    parts += ["## 잘한 점", "", (entry.get("good") or "").strip() or "_(미작성)_", ""]
    parts += ["## 아쉬운 점", "", (entry.get("bad") or "").strip() or "_(미작성)_", ""]
    return "\n".join(parts).rstrip() + "\n"


def render_day_index(date: str, entries: list[dict]) -> str:
    """그날 인덱스. 목록에서 각 매매 문서로 들어간다.

    합계는 **손익만** 적는다 — 예수금·계좌 평가액은 문서에 싣지 않는다.
    """
    parts = [f"# {date}", ""]
    if not entries:
        return "\n".join(parts + ["매매 없음", ""])

    closed = [e for e in entries if (e.get("state") or "") == "종료"]
    wins = sum(1 for e in closed if net_pnl(e) > 0)
    losses = sum(1 for e in closed if net_pnl(e) < 0)
    summary = [f"{len(entries)}건"]
    if wins or losses:
        summary.append(f"익절 {wins} · 손절 {losses}")
        summary.append(f"승률 {wins / (wins + losses):.1%}")
    if holding := len(entries) - len(closed):
        summary.append(f"보유 중 {holding}")
    summary.append(f"세후 {sum(net_pnl(e) for e in entries):+,.0f}원")
    parts += [" · ".join(summary), ""]

    parts += ["| 종목 | 결과 | 세후 손익 | 보유 | 작성 |", "|---|---|---|---|---|"]
    for e in sorted(entries, key=net_pnl, reverse=True):
        written = "✔" if (e.get("good") or e.get("bad")) else ""
        parts.append(
            f"| [{e.get('name', '')}({e.get('symbol', '')})]"
            f"({date}/{trade_slug(e)}.md) | {result_label(e)}"
            f" | {net_pnl(e):+,.0f}원 | {e.get('holding', '')} | {written} |"
        )
    return "\n".join(parts) + "\n"


# ── 파일로 쓰기 ─────────────────────────────────────────────────


def export_day(
    store,
    date: str,
    root: Path | str = "journal",
    calendar: TradingCalendar | None = None,
) -> list[Path]:
    """그날의 매매일지를 파일로 쓰고, 만든 파일 목록을 돌려준다.

    같은 날을 다시 돌리면 통째로 덮어쓴다 — 문서는 순수 생성물이라 그래도 안전하고,
    오히려 그래야 나중에 DB 에 코멘트를 채운 뒤 다시 돌려 반영할 수 있다.

    차트는 **일봉과 3분봉 둘 다** 문서 옆으로 **복사**한다. 심볼릭 링크나 상대 경로
    참조로 두면 git 에 올렸을 때 폰에서 그림이 깨진다. 한쪽이 없거나 원본이 지워졌으면
    있는 것만 싣는다 — 예전 매매의 차트를 정리했다고 생성이 멈추면 안 된다.
    """
    root = Path(root)
    entries = store.journal_entries(since=date, until=date)
    day_dir = root / date[:7] / date
    if not entries:
        return []

    cycles = store.cycles_for([(e["symbol"], e.get("trade_date", "")) for e in entries])
    day_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for entry in entries:
        cycle = cycles.get((entry["symbol"], entry.get("trade_date", "")), [])
        entry["holding"] = cycle_holding(cycle, calendar)
        charts: dict[str, str] = {}
        for label, key, suffix in _CHARTS:
            source = Path(entry.get(key) or "")
            if not (entry.get(key) and source.exists()):
                continue
            filename = f"{trade_slug(entry)}-{suffix}{source.suffix}"
            target = day_dir / filename
            # 내용이 같으면 건드리지 않는다. `--all` 로 1년치를 다시 돌리면 수천 장을
            # 헛되이 복사하게 되고, 파일 시각만 바뀌어도 백업 도구가 전부 바뀐 것으로
            # 본다. (git 은 내용 주소 방식이라 같은 그림은 어차피 저장소를 늘리지 않는다.)
            if not (target.exists() and filecmp.cmp(source, target, shallow=False)):
                shutil.copyfile(source, target)
            charts[label] = filename
            written.append(target)
        doc = day_dir / f"{trade_slug(entry)}.md"
        _write_if_changed(doc, render_trade(entry, cycle, calendar, charts))
        written.append(doc)

    index = root / date[:7] / f"{date}.md"
    _write_if_changed(index, render_day_index(date, entries))
    written.append(index)
    return written


def _write_if_changed(path: Path, text: str) -> None:
    """내용이 같으면 건드리지 않는다.

    `--all` 로 1년치를 다시 돌리는 것이 기본 사용법이라, 바뀐 것이 없는 날까지 파일
    시각을 갱신하면 백업 도구가 전부 바뀐 것으로 본다. (git 은 내용 주소 방식이라
    어차피 커밋이 생기지 않지만, 여기서 막아 두면 다른 동기화 수단에서도 안전하다.)
    """
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    path.write_text(text, encoding="utf-8")
