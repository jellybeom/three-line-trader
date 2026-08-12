"""매매일지 창 — 매매 결과와 차트를 보면서 코멘트를 남긴다.

작성 시점을 강제하지 않는다(종료 직후·장중·마감 후·주말 어느 때나). 그래서 창은
**최근 매매 목록**을 왼쪽에 두고, 고른 매매의 지표와 차트를 오른쪽에 보여준 뒤
아래에서 코멘트를 쓰는 구조다. 미작성 건은 목록에서 바로 구분된다.

매매 데이터(손익·MFE/MAE·태그)는 이미 DB 에 있으므로 여기서는 **읽어서 보여주고
사람이 쓴 것만 저장**한다.
"""

from __future__ import annotations

import datetime as dt
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

from trader.journal import transition_path  # 재수출 — 창에서 경로 문자열을 쓴다
from trader.ui.chart_view import ChartView

__all__ = [
    "JournalDialog",
    "entry_label",
    "filter_entries",
    "net_pnl",
    "summarize",
    "transition_path",
]

_ICON = Path(__file__).resolve().parents[2] / "assets" / "three-line-trader.ico"

_TEXT_LINES = 4  # 잘한 점 · 아쉬운 점 입력칸 줄 수 (둘은 항상 같아야 한다)
_IME_GAP = 10  # 한글 조합 창이 아래 위젯을 덮지 않도록 검색칸 밑에 두는 여백 (px)
_BUTTON_MIN_PX = 56  # '월별'·'전체' 두 글자가 잘리지 않는 최소 폭

# 기간 선택 — '월별' 과 '전체' 둘 중 하나. 분기·주간처럼 잘게 나누면 고르는 데 손이
# 더 가고, 특정 날짜를 찾을 땐 종목명 검색이 더 빠르다.
PERIOD_MONTH = "월별"
PERIOD_ALL = "전체"
MONTHS = tuple(f"{m:02d}" for m in range(1, 13))
FIRST_YEAR = 2026  # 이 프로그램으로 매매를 시작한 해 — 그 이전 기록은 있을 수 없다


def period_range(year: str, month: str) -> tuple[str, str]:
    """(연, 월) → (since, until). 둘 중 하나라도 비면 제한 없음(= 전체).

    달의 마지막 날은 다음 달 1일에서 하루를 빼 구한다 — 월별 일수와 윤년을 따로
    다루지 않아도 된다.
    """
    if not (year and month):
        return "", ""
    try:
        first = dt.date(int(year), int(month), 1)
    except ValueError:
        return "", ""
    nxt = dt.date(first.year + first.month // 12, first.month % 12 + 1, 1)
    return first.isoformat(), (nxt - dt.timedelta(days=1)).isoformat()


def year_choices(months: tuple, today: dt.date | None = None) -> list[str]:
    """고를 수 있는 연도 — 시작한 해부터 올해까지 **오름차순** (월 목록과 같은 방향).

    빈 해도 목록에 남긴다. 중간이 비었다고 건너뛰면 목록이 들쭉날쭉해 오히려 찾기 어렵다.
    """
    today = today or dt.date.today()
    latest = max([today.year, FIRST_YEAR] + [int(m[:4]) for m in months if len(m) >= 4])
    return [str(y) for y in range(FIRST_YEAR, latest + 1)]


def _bind_optional(widget, sequence: str, handler) -> None:
    """플랫폼에 없는 키 이름이면 조용히 건너뛴다.

    <ISO_Left_Tab> 은 X11 에만 있는 Shift+Tab 키심이다. Windows 에서 그대로 bind 하면
    TclError("bad event type or keysym") 로 창 생성 자체가 실패한다(2026-08-12).
    """
    try:
        widget.bind(sequence, handler)
    except tk.TclError:
        pass


def _focus_to(target):
    """Tab 처리기 — 지정한 위젯으로 보낸다.

    "break" 를 돌려줘야 위젯의 기본 동작이 막힌다. 특히 tk.Text 는 Tab 을 **글자로**
    받아넣고, Listbox·Notebook 도 저마다 다르게 반응한다. 기본 순서는 위젯을 만든
    차례를 따르는데 화면 배치 순서와 달라(저장 버튼이 코멘트보다 먼저 생긴다), 눈에
    보이는 대로 움직이도록 순서를 직접 정한다.
    """

    def handler(_event=None):
        target.focus_set()
        return "break"

    return handler


class SearchEntry(ttk.Frame):
    """안내 문구가 있는 검색 입력칸 + 지우기(✕) 버튼.

    안내 문구를 **Entry 위에 Label 을 얹는 방식으로 만들지 않는다.** 얹으면 Windows 11
    테마가 입력칸 아래에 그리는 파란 밑줄을 가려버린다(2026-08-12). 대신 고전적인
    방식대로 **Entry 안에 흐린 글자를 직접 넣고** 포커스가 오면 지운다. 겹치는 위젯이
    없으니 테마가 무엇이든 안전하다.

    지우기 버튼도 입력칸 **바깥** 오른쪽에 둔다 — 같은 이유이고, 글자가 버튼에
    가려지는 일도 없다.
    """

    def __init__(self, master, placeholder: str, on_change: Callable[[], None]):
        super().__init__(master)
        self._placeholder = placeholder
        self._on_change = on_change
        self._showing = False  # 지금 보이는 글자가 안내 문구인가

        # ✕ 를 **먼저** 오른쪽에 붙인 뒤 입력칸이 남는 자리를 채우게 한다.
        # 순서를 바꾸면(입력칸 expand → 버튼) 버튼이 들어갈 자리가 남지 않아
        # 폭 0 으로 찌그러져 화면에 보이지 않는다(2026-08-12).
        self._clear = ttk.Button(
            self, text="✕", width=2, command=self.clear, takefocus=False
        )
        self._clear.pack(side="right", padx=(4, 0))
        self.entry = ttk.Entry(self)
        self.entry.pack(side="left", fill="x", expand=True)
        # 글자가 **들어가기 전에** 안내 문구를 치운다. FocusIn 에만 기대면, 포커스
        # 이벤트 없이 입력이 들어올 때 안내 문구 앞에 글자가 붙어버린다
        # ("로봇" + "종목명 또는 종목코드"). 키를 누르는 순간 지우면 순서가 보장된다.
        self.entry.bind("<KeyPress>", self._on_key_press)
        self.entry.bind("<KeyRelease>", self._on_key)
        self.entry.bind("<FocusIn>", self._on_focus_in)
        self.entry.bind("<FocusOut>", self._on_focus_out)
        self.entry.bind("<Escape>", lambda _e: self.clear())
        self._show_placeholder()

    def get(self) -> str:
        """실제 검색어. 안내 문구가 보이는 중이면 빈 문자열."""
        return "" if self._showing else self.entry.get()

    def clear(self) -> None:
        self.entry.delete(0, "end")
        self._sync_button()
        self._on_change()
        if self.focus_get() is not self.entry:
            self._show_placeholder()

    def _show_placeholder(self) -> None:
        if self.entry.get():
            return
        self._showing = True
        self.entry.insert(0, self._placeholder)
        self.entry.configure(foreground="#9e9e9e")
        self._sync_button()

    def _hide_placeholder(self) -> None:
        if not self._showing:
            return
        self._showing = False
        self.entry.delete(0, "end")
        self.entry.configure(foreground="")

    def _on_focus_in(self, _event=None) -> None:
        self._hide_placeholder()

    def _on_focus_out(self, _event=None) -> None:
        self._show_placeholder()

    def _on_key_press(self, _event=None) -> None:
        self._hide_placeholder()

    def _on_key(self, _event=None) -> None:
        if self._showing and self.entry.get() == self._placeholder:
            return  # 방향키 등 — 글자는 안 들어왔으므로 안내 문구 그대로 둔다
        self._showing = False  # 글자를 쳤으면 더 이상 안내 문구가 아니다
        self.entry.configure(foreground="")
        self._sync_button()
        self._on_change()

    def _sync_button(self) -> None:
        """지울 것이 없으면 ✕ 를 눌리지 않게 한다.

        숨기지 않고 흐리게만 둔다 — 숨겼다 보였다 하면 입력칸 폭이 그때마다 달라져
        글자가 밀린다. 자리는 늘 잡아두고 상태만 바꾼다.
        """
        self._clear.state(["!disabled"] if self.get() else ["disabled"])


def net_pnl(entry: dict) -> float:
    """세후 실현손익 — 목록 표시와 익절/손절 판정이 같은 값을 쓰도록 한 곳에 둔다."""
    return (entry.get("realized_pnl") or 0) - (entry.get("fees") or 0)


def filter_entries(
    entries: list[dict], query: str = "", result: str = "전체"
) -> list[dict]:
    """검색어와 익절/손절 조건으로 목록을 걸러낸다 (순수 함수 — UI 없이 시험 가능).

    검색어는 **종목명·종목코드 어느 쪽이든 일부만** 맞으면 된다. 코드를 외우고 있지
    않아도 되고, 반대로 이름이 헷갈릴 때는 코드로 찾을 수 있다. 대소문자는 무시한다.

    익절/손절은 **세후 손익 부호**로 나눈다. 수수료·세금까지 빼고도 남았는지가
    실제로 번 것인지의 기준이라, 매도가 평단보다 높았는지보다 이쪽이 정확하다.
    본전(0원)은 익절도 손절도 아니므로 '전체' 에서만 보인다.
    """
    rows = entries
    if query := query.strip().lower():
        rows = [
            e
            for e in rows
            if query in (e.get("name") or "").lower()
            or query in (e.get("symbol") or "").lower()
        ]
    if result == "익절":
        rows = [e for e in rows if net_pnl(e) > 0]
    elif result == "손절":
        rows = [e for e in rows if net_pnl(e) < 0]
    return rows


def summarize(entry: dict) -> list[tuple[str, str]]:
    """일지 상단에 보여줄 매매 요약 (라벨, 값) 목록."""
    avg = entry.get("avg_price") or 0
    total = entry.get("total_bought") or 0
    net = net_pnl(entry)
    rows = [
        ("매매일", entry.get("trade_date", "")),
        ("종목", f"{entry.get('name', '')}({entry.get('symbol', '')})"),
        ("상태", entry.get("state", "")),
        ("평단 / 수량", f"{avg:,.0f} · {total}주" if avg else "-"),
        (
            "실현손익(세후)",
            f"{net:+,.0f}원"
            + (f" ({net / (avg * total):+.2%})" if avg and total else ""),
        ),
    ]
    high, low = entry.get("high_price") or 0, entry.get("low_price") or 0
    if avg and high:
        rows.append(
            ("최고 / 최저", f"{(high - avg) / avg:+.1%} / {(low - avg) / avg:+.1%}")
        )
    opened, closed = entry.get("day_open") or 0, entry.get("day_close") or 0
    if opened and closed:
        rows.append(("당일 등락", f"{(closed - opened) / opened:+.2%}"))
    if path := (entry.get("path") or ""):
        rows.append(("상태 경로", path))
    if timeline := (entry.get("timeline") or ""):
        rows.append(("시점", timeline))
    if tags := (entry.get("tags") or ""):
        rows.append(("태그", " ".join(f"#{t}" for t in tags.split(",") if t)))
    if base := (entry.get("base_date") or ""):
        rows.append(("기준봉", base))
    if memo := (entry.get("memo") or ""):
        rows.append(("메모", memo))
    return rows


def summarize_stats(entries: list[dict], total: int | None = None) -> str:
    """목록 아래에 보여줄 성적 요약.

    단순히 몇 건인지보다 **이겼는지 졌는지**가 먼저 궁금하다. 승률과 세후 합계까지
    한 줄에 담아, 기간을 바꿔가며 성적을 비교할 수 있게 한다.

    승률은 **익절 ÷ (익절 + 손절)** 이다. 세후 정확히 0원인 매매는 이긴 것도 진 것도
    아니라 분모에서 뺀다 — 넣으면 승률이 실제보다 낮아 보인다. 보유 중이라 아직 손익이
    확정되지 않은 건도 마찬가지로 빠진다.

    total 을 주면 "12/30건" 처럼 걸러진 상태임을 함께 보여준다.
    """
    if not entries:
        return "0건" if total in (None, 0) else f"0/{total}건"
    closed = [e for e in entries if (e.get("state") or "") == "종료"]
    wins = sum(1 for e in closed if net_pnl(e) > 0)
    losses = sum(1 for e in closed if net_pnl(e) < 0)
    count = (
        f"{len(entries)}건"
        if total in (None, len(entries))
        else f"{len(entries)}/{total}건"
    )
    parts = [count]
    if wins or losses:
        parts.append(f"익절 {wins} · 손절 {losses}")
        parts.append(f"승률 {wins / (wins + losses):.1%}")
    if holding := len(entries) - len(closed):
        parts.append(f"보유 중 {holding}")
    parts.append(f"세후 {sum(net_pnl(e) for e in entries):+,.0f}원")
    return " · ".join(parts)


def entry_label(entry: dict) -> str:
    """목록 한 줄 — 작성 여부가 한눈에 보이도록 앞에 표시를 둔다."""
    net = net_pnl(entry)
    icon = "💰" if net > 0 else ("🛑" if net < 0 else "⚪")
    written = "✍" if (entry.get("good") or entry.get("bad")) else "　"
    return f"{written} {icon} {entry.get('trade_date', '')} {entry.get('name', '')} {net:+,.0f}"


class JournalDialog(tk.Toplevel):
    """일지 작성 창. 저장은 on_save(trade_date, symbol, good, bad) 로 위임한다."""

    def __init__(
        self,
        master,
        entries: list[dict],
        on_save: Callable[[str, str, str, str], None],
        select: tuple[str, str] | None = None,
        on_period: Callable[[str, str], None] | None = None,
        months: tuple = (),
    ):
        super().__init__(master)
        self.title("매매일지")
        self.geometry("1180x760")
        if _ICON.exists():
            try:
                self.iconbitmap(str(_ICON))
            except tk.TclError:
                pass

        self._entries = entries
        self._visible = entries  # 검색·필터를 통과해 지금 목록에 보이는 것
        self._on_save = on_save
        self._on_period_change = on_period
        self._current: dict | None = None
        self._views: dict[str, ChartView] = {}

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=8, pady=8)

        left = ttk.Frame(pane)
        ttk.Label(left, text="매매 목록 (✍ = 작성됨)").pack(anchor="w", pady=(0, 4))

        # 검색 — 종목명·코드 어느 쪽으로도 찾을 수 있다. 입력할 때마다 즉시 걸러진다
        # (목록이 수백 건이어도 문자열 비교뿐이라 체감 지연이 없다).
        self._search = SearchEntry(left, "종목명 또는 종목코드", self._fill_list)
        # 아래 여백을 넉넉히 둔다. 한글을 조합하는 동안 Windows 가 입력칸 바로 아래에
        # **자기 조합 창을 띄우는데**(OS 가 그리는 창이라 Tk 로는 못 막는다), 간격이
        # 좁으면 그 창이 다음 줄 위젯의 테두리를 덮는다(2026-08-12).
        self._search.pack(fill="x", pady=(0, _IME_GAP))

        # 기간 — 월별 조회와 전체 조회 중 하나. 한 줄에 [월별] [연] [월] [전체] 로 둔다.
        # 고르면 DB 를 다시 읽는다(전체를 들고 와서 화면에서 거르면 기록이 쌓일수록 느려진다).
        today = dt.date.today()
        self._period_mode = tk.StringVar(value=PERIOD_MONTH)
        self._year = tk.StringVar(value=str(today.year))
        self._month = tk.StringVar(value=f"{today.month:02d}")

        period_row = ttk.Frame(left)
        period_row.pack(fill="x", pady=(0, 4))
        # grid 로 폭을 나눠 갖게 한다. pack 으로는 오른쪽에 빈 자리가 남는다.
        # 3번 칸을 비워 '월별 묶음' 과 '전체' 사이를 벌린다.
        # minsize 가 없으면 창을 좁혔을 때 버튼 글자가 '월별' → '월' 로 잘린다.
        for column, weight, minsize in (
            (0, 2, _BUTTON_MIN_PX),
            (1, 4, 56),
            (2, 3, 44),
            (3, 1, 8),
            (4, 2, _BUTTON_MIN_PX),
        ):
            period_row.columnconfigure(column, weight=weight, minsize=minsize)
        self._month_button = ttk.Radiobutton(
            period_row,
            text=PERIOD_MONTH,
            value=PERIOD_MONTH,
            variable=self._period_mode,
            command=self._on_period,
            style="Filter.Toolbutton",
        )
        self._month_button.grid(row=0, column=0, sticky="ew")
        self._year_box = ttk.Combobox(
            period_row,
            textvariable=self._year,
            state="readonly",
            justify="center",  # 월 콤보와 같은 정렬 (숫자가 가운데 오면 읽기 편하다)
            values=year_choices(months),
        )
        self._year_box.grid(row=0, column=1, sticky="ew", padx=(4, 2))
        self._month_box = ttk.Combobox(
            period_row,
            textvariable=self._month,
            state="readonly",
            justify="center",
            values=list(MONTHS),
        )
        self._month_box.grid(row=0, column=2, sticky="ew")
        self._all_button = ttk.Radiobutton(
            period_row,
            text=PERIOD_ALL,
            value=PERIOD_ALL,
            variable=self._period_mode,
            command=self._on_period,
            style="Filter.Toolbutton",
        )
        self._all_button.grid(row=0, column=4, sticky="ew")
        for box in (self._year_box, self._month_box):
            # 연·월을 고르면 자동으로 월별 조회로 바뀐다 (모드를 따로 누를 필요가 없다)
            box.bind("<<ComboboxSelected>>", self._on_month_pick)

        self._result = tk.StringVar(value="전체")
        # Toolbutton 은 글자를 왼쪽에 붙여 놓는다. 버튼 셋이 폭을 나눠 가지므로
        # 그대로 두면 글자가 제각각 왼쪽에 몰려 보인다 — anchor 를 가운데로 바꾼다.
        ttk.Style().configure("Filter.Toolbutton", anchor="center")
        buttons = ttk.Frame(left)
        buttons.pack(fill="x", pady=(0, 4))
        self._filters = []
        for text in ("전체", "익절", "손절"):
            button = ttk.Radiobutton(
                buttons,
                text=text,
                value=text,
                variable=self._result,
                command=self._fill_list,
                style="Filter.Toolbutton",  # 라디오점 대신 눌린 버튼 모양으로 보인다
            )
            button.pack(side="left", expand=True, fill="x")
            self._filters.append(button)

        self._list = tk.Listbox(left, width=34, exportselection=False)
        self._list.pack(fill="both", expand=True)
        self._list.bind("<<ListboxSelect>>", self._on_select)
        # 성적 요약 — 목록 폭에 맞춰 줄바꿈한다 (칸을 좁히면 두 줄이 된다)
        self._count = ttk.Label(left, text="", foreground="#616161", justify="left")
        self._count.pack(anchor="w", fill="x", pady=(4, 0))
        left.bind(
            "<Configure>",
            lambda e: self._count.configure(wraplength=max(e.width - 8, 100)),
        )
        pane.add(left, weight=1)

        right = ttk.Frame(pane)
        pane.add(right, weight=3)

        self._summary = ttk.Frame(right)
        self._summary.pack(side="top", fill="x", pady=(0, 6))

        # **입력칸과 저장 버튼을 아래쪽에 먼저 배치한다.** pack 은 순서대로 자리를 나눠주는데,
        # 차트(expand=True)를 먼저 넣으면 창보다 내용이 커질 때 뒤에 오는 위젯이 화면 밖으로
        # 밀려난다 — 실제로 요약 줄이 늘어나자 저장 버튼이 통째로 사라졌다(2026-08-11).
        # side="bottom" 으로 먼저 잡아두면 차트는 '남는 만큼' 만 쓰므로 절대 밀리지 않는다.
        bar = ttk.Frame(right)
        bar.pack(side="bottom", fill="x", pady=(6, 0))
        self._status = ttk.Label(bar, text="", foreground="#9e9e9e")
        self._status.pack(side="left")
        self._save_button = ttk.Button(bar, text="저장", command=self._save)
        self._save_button.pack(side="right")

        form = ttk.Frame(right)
        form.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Label(form, text="잘한 점").grid(row=0, column=0, sticky="nw", pady=(0, 4))
        self._good = tk.Text(form, height=_TEXT_LINES, wrap="word")
        self._good.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(0, 4))
        ttk.Label(form, text="아쉬운 점").grid(
            row=1, column=0, sticky="nw", pady=(0, 4)
        )
        self._bad = tk.Text(form, height=_TEXT_LINES, wrap="word")
        self._bad.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(0, 4))
        form.columnconfigure(1, weight=1)
        # uniform 을 같게 주면 두 칸의 높이가 항상 같아진다 (한쪽만 눌리지 않는다)
        for row in (0, 1):
            form.rowconfigure(row, weight=1, uniform="comment")

        self._charts = ttk.Notebook(right)
        self._charts.pack(side="top", fill="both", expand=True)

        self._setup_focus_order()
        self._fill_list()
        self.update_idletasks()  # 차트 축소 배율을 실제 배치 크기로 계산하기 위해
        if select:
            self._select_symbol(*select)
        elif entries:
            self._list.selection_set(0)
            self._show(entries[0])

    # ── 목록 ───────────────────────────────────────────────────

    def _on_month_pick(self, _event=None) -> None:
        """연·월을 고르면 월별 조회로 전환한다."""
        self._period_mode.set(PERIOD_MONTH)
        self._on_period()

    def _on_period(self, _event=None) -> None:
        """기간이 바뀌면 DB 를 다시 읽어달라고 요청한다 (결과는 set_entries 로 온다)."""
        monthly = self._period_mode.get() == PERIOD_MONTH
        state = "readonly" if monthly else "disabled"
        for box in (self._year_box, self._month_box):
            box.configure(state=state)  # 전체 조회 중에는 연·월이 무의미하다
            box.selection_clear()
        if self._on_period_change is None:
            return
        if monthly:
            self._on_period_change(*period_range(self._year.get(), self._month.get()))
        else:
            self._on_period_change("", "")

    def set_entries(self, entries: list[dict], months: tuple = ()) -> None:
        """새 기간의 목록으로 갈아끼운다. 검색어·손익 필터는 그대로 둔다."""
        self._entries = entries
        if months:  # 기록이 있는 해를 연도 목록에 반영한다
            self._year_box.configure(values=year_choices(months))
        self._fill_list()
        if self._visible:
            self._list.selection_set(0)
            self._show(self._visible[0])
        else:
            self._current = None

    def _setup_focus_order(self) -> None:
        """Tab 이 화면에 보이는 순서대로 돌게 한다.

        검색 → 필터 → 목록 → 차트 → 잘한 점 → 아쉬운 점 → 저장 → (다시 검색).
        마지막에서 처음으로 돌아오게 해 어디서 시작해도 모든 칸에 닿을 수 있다.
        """
        self.focus_chain = [
            self._search.entry,
            self._month_button,
            self._year_box,
            self._month_box,
            self._all_button,
            *self._filters,
            self._list,
            self._charts,
            self._good,
            self._bad,
            self._save_button,
        ]
        chain = self.focus_chain
        for i, widget in enumerate(chain):
            widget.configure(takefocus=True)
            widget.bind("<Tab>", _focus_to(chain[(i + 1) % len(chain)]))
            widget.bind("<Shift-Tab>", _focus_to(chain[i - 1]))
            _bind_optional(widget, "<ISO_Left_Tab>", _focus_to(chain[i - 1]))

    def _fill_list(self) -> None:
        """검색·필터를 적용해 목록을 다시 그린다.

        고른 항목이 필터에 걸려 사라지면 오른쪽 상세는 그대로 둔다 — 검색어를 지우는
        중에 화면이 깜빡이거나, 쓰던 코멘트가 날아가는 것을 막기 위해서다.
        """
        self._visible = filter_entries(
            self._entries, self._search.get(), self._result.get()
        )
        self._list.delete(0, "end")
        for entry in self._visible:
            self._list.insert("end", entry_label(entry))
        self._count.configure(text=summarize_stats(self._visible, len(self._entries)))
        if self._current in self._visible:  # 보고 있던 항목이 남아 있으면 선택 유지
            index = self._visible.index(self._current)
            self._list.selection_set(index)
            self._list.see(index)

    def _select_symbol(self, trade_date: str, symbol: str) -> None:
        for i, entry in enumerate(self._visible):
            if entry["trade_date"] == trade_date and entry["symbol"] == symbol:
                self._list.selection_clear(0, "end")
                self._list.selection_set(i)
                self._list.see(i)
                self._show(entry)
                return

    def _on_select(self, _event=None) -> None:
        sel = self._list.curselection()
        if sel and sel[0] < len(self._visible):
            self._show(self._visible[sel[0]])

    # ── 표시 ───────────────────────────────────────────────────

    def _show(self, entry: dict) -> None:
        self._current = entry
        for child in self._summary.winfo_children():
            child.destroy()
        for row, (label, value) in enumerate(summarize(entry)):
            ttk.Label(self._summary, text=label, foreground="#9e9e9e").grid(
                row=row // 2, column=(row % 2) * 2, sticky="w", padx=(0, 6)
            )
            ttk.Label(self._summary, text=value).grid(
                row=row // 2, column=(row % 2) * 2 + 1, sticky="w", padx=(0, 24)
            )

        # 탭은 창을 만들 때 한 번만 두고 그림만 갈아끼운다. 매번 새로 만들면 확대 상태가
        # 초기화되고, 위젯이 쌓여 메모리도 샌다.
        for title, key in (("일봉", "daily_path"), ("3분봉", "minute_path")):
            view = self._views.get(title)
            if view is None:
                view = ChartView(self._charts)
                self._charts.add(view, text=title)
                self._views[title] = view
            view.show(entry.get(key))

        self._good.delete("1.0", "end")
        self._good.insert("1.0", entry.get("good") or "")
        self._bad.delete("1.0", "end")
        self._bad.insert("1.0", entry.get("bad") or "")
        self._status.configure(
            text="휠: 확대·축소 · 드래그: 이동 · 더블클릭: 원본 열기"
        )

    # ── 저장 ───────────────────────────────────────────────────

    def _save(self) -> None:
        if self._current is None:
            return
        good = self._good.get("1.0", "end").strip()
        bad = self._bad.get("1.0", "end").strip()
        if not (good or bad):
            messagebox.showinfo(
                "저장", "잘한 점이나 아쉬운 점을 입력해주세요.", parent=self
            )
            return
        self._on_save(self._current["trade_date"], self._current["symbol"], good, bad)
        self._current["good"], self._current["bad"] = good, bad
        if self._current in self._visible:  # ✍ 표시를 갱신한다
            index = self._visible.index(self._current)
            self._list.delete(index)
            self._list.insert(index, entry_label(self._current))
            self._list.selection_set(index)
        self._status.configure(text="저장했습니다.", foreground="#2e7d32")
