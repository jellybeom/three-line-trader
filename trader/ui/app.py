"""메인 윈도우 (FHD 최적화) — 화면 구성:

  [툴바]      감시 시작/중지 · 매매일지 · 손익 요약 · 상태
  [설정]      한 줄 5그룹 (상시 표시):
              투자 모드 | 매매일(요일) | 키움 연결(주문가능금액) | Discord(알림 수준)
              | 자금 배분 및 익절 전략(적용 버튼 포함)
  [모니터]    종목 테이블 (세로 대부분) — 행 내 ✎/✕, ＋추가 행, 열 정렬
  [로그]      우클릭 메뉴 (지우기 / CSV 내보내기)
  [상태 바]   WS 상태 · 마지막 틱 · 장 운영 · 모드/종목 수

역할은 화면 조립, 200ms 큐 폴링, 사용자 조작의 명령 큐 전달뿐이다.
키움/Discord 키의 출처는 config.toml 이며, 설정값(모드·자금·익절·알림 수준)은
settings 테이블에 저장되어 재시작 시 복원된다.
"""

from __future__ import annotations

import csv
import queue
import re
import sys
import tkinter as tk
import traceback
from datetime import datetime, time as dtime, timedelta
from tkinter import filedialog, font as tkfont, messagebox, ttk

from trader.state_machine import State
from trader.ui import bus

try:
    import warnings

    with (
        warnings.catch_warnings()
    ):  # tkcalendar 소스의 이스케이프 결함 경고 억제 (동작 무관)
        warnings.filterwarnings("ignore", category=SyntaxWarning)
        from tkcalendar import DateEntry  # 캘린더 드롭다운 (uv add tkcalendar)
except ImportError:
    DateEntry = None
from trader.ui.chart_view import ChartView
from trader.ui.journal_dialog import SearchEntry
from trader.ui.events_view import EventsView
from trader.ui import theme
from trader.ui.icons import apply_icon
from trader.ui.positions_view import PositionsView
from trader.ui.register_dialog import RegisterDialog


# 휴장은 달력의 '빨간 날' 관례를 따른다. 주황은 이 프로그램에서 '진행 중·주의'(연결 중,
# 3선 미입력)를 뜻해, 확정된 사실인 휴장에는 맞지 않는다.
def _market_color(market: str) -> str:
    """개장/휴장/확인 불가 색. 휴장은 달력의 '빨간 날' 관례를 따른다."""
    c = theme.palette()
    return {"휴장": c.profit, "확인 불가": c.muted}.get(market, "")


# 개장/휴장 줄의 폭을 정할 때 기준으로 삼는 가장 긴 문구. 휴장 사유는 holidays.csv 에서
# 오므로 앞으로 조금 길어질 수 있어 여유를 둔다.
_MARKET_SAMPLE = "(월) · 휴장 · 석가탄신일(대체휴일)＋"
_SEARCH_CHARS = 18  # 검색 입력칸 폭(글자 수) — 종목명은 대개 이보다 짧다
_IME_GAP_PX = 3  # 검색줄과 표 사이 여백 — 줄이 세로를 많이 먹지 않도록 최소로 둔다
_POLL_MS = 200
# 종목코드는 6자리이며 **숫자로만 이루어지지 않는다** — 신주인수권·스팩 등에는
# 영문자가 섞인다(실측 2026-08-05: 아로마티카 0015N0). 숫자만 허용하면 조용히 누락된다.
# 앞의 따옴표(영웅문의 '096770)와 시장구분 접두 A 는 걷어낸다.
_CODE_PATTERN = re.compile(r"^['\u2019]*A?([0-9][0-9A-Z]{5})$", re.IGNORECASE)
_NUMERIC_CELL = re.compile(r"^[\d,.+\-%\s]*$")


_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_tags(cell: str) -> str:
    """태그 셀 정규화 — 쉼표·공백·`#` 어느 조합으로 적어도 같은 결과가 되게 한다.

    "#KOSPI상승장, #테마주" / "테마주 상한가" / "#테마주,상한가" → "테마주,상한가"
    """
    if not cell:
        return ""
    parts = [
        t.strip().lstrip("#").strip()
        for t in cell.replace("#", ",#").replace(" ", ",").split(",")
    ]
    seen: list[str] = []
    for tag in parts:
        if tag and tag not in seen:
            seen.append(tag)
    return ",".join(seen)


def parse_watchlist_csv(path: str) -> list[tuple[str, str, str, tuple | None]]:
    """영웅문 관심종목 CSV → [(종목코드, 종목명, 메모, 3선가격 또는 None)].

    1순위: 헤더 행에 '종목코드'/'종목명' 열이 있으면 그 열을 그대로 사용 (영웅문 형식).
      - '메모' 열이 있으면 함께 읽는다.
      - '1선'/'2선'/'3선' 열을 사용자가 추가해 값을 채우면 (line1, line2, line3) 로 읽어
        불러오기 시 곧바로 정식 등록된다 (없거나 비면 None → 3선 미입력 대기).
    2순위: 헤더가 없으면 휴리스틱 — 행에서 6자리 코드를 찾고 주변의 첫 텍스트 셀을 종목명으로.
    """
    for enc in ("cp949", "utf-8-sig"):
        try:
            with open(path, newline="", encoding=enc) as f:
                rows = list(csv.reader(f))
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("CSV 인코딩을 해석할 수 없습니다 (cp949 / utf-8 지원)")

    def extract_code(cell: str) -> str | None:
        m = _CODE_PATTERN.match(cell.strip().strip('"'))
        return m.group(1).upper() if m else None  # 0015n0 → 0015N0 로 통일

    result: list[tuple[str, str, str, tuple | None]] = []
    seen: set[str] = set()

    def cell(row: list, idx: int | None) -> str:
        return row[idx].strip().strip('"') if idx is not None and len(row) > idx else ""

    def repair_tag_split(row: list) -> list:
        """따옴표 없이 쉼표로 이어 쓴 태그를 한 칸으로 되돌린다.

        CSV 는 쉼표가 열 구분자라 `#상승장,#테마주` 를 따옴표로 감싸지 않으면 두 칸으로
        쪼개지고 뒤따르는 열이 밀린다. 쪼개진 조각이 모두 `#` 로 시작할 때만 합쳐,
        메모에 쉼표가 들어간 경우를 잘못 건드리지 않는다.
        """
        extra = len(row) - len(header)
        if extra <= 0 or tag_idx is None:
            return row
        pieces = [p.strip() for p in row[tag_idx : tag_idx + 1 + extra]]
        rest = row[tag_idx + 1 + extra :]
        # `#` 로 시작하면 확실한 태그. 없더라도 뒤 칸이 제자리를 찾으면(기준봉이 날짜
        # 형식이면) 태그가 쪼개진 것으로 본다 — 메모의 쉼표를 잘못 합치지 않기 위한 조건.
        looks_tagged = all(p.startswith("#") for p in pieces if p)
        if not looks_tagged and rest:
            # 태그 뒤에 다른 열이 있으면, 그 열이 제자리를 찾을 때만 합친다
            # (메모의 쉼표를 태그로 잘못 합치지 않기 위한 조건).
            expect_date = base_idx is not None and base_idx > tag_idx
            candidate = rest[base_idx - tag_idx - 1] if expect_date else ""
            if not _DATE_PATTERN.match(candidate.strip()):
                return row
        # rest 가 비었다면 태그가 마지막 열 — 뒤로 밀릴 값이 없으니 그대로 합친다
        return row[:tag_idx] + [",".join(pieces)] + rest

    def parse_lines(row: list, idxs: tuple) -> tuple | None:
        try:
            values = tuple(float(cell(row, i).replace(",", "")) for i in idxs)
        except (ValueError, TypeError):
            return None
        return values if all(v > 0 for v in values) else None

    header = rows[0] if rows else []
    code_idx = next((i for i, c in enumerate(header) if "종목코드" in c), None)
    name_idx = next((i for i, c in enumerate(header) if "종목명" in c), None)
    memo_idx = next((i for i, c in enumerate(header) if "메모" in c), None)
    # 선정 태그·기준봉 날짜 (선택 열) — 없으면 빈 값으로 둔다
    tag_idx = next((i for i, c in enumerate(header) if "태그" in c), None)
    base_idx = next((i for i, c in enumerate(header) if "기준봉" in c), None)
    line_idxs = tuple(
        next((i for i, c in enumerate(header) if f"{n}선" in c), None)
        for n in (1, 2, 3)
    )
    has_lines = all(i is not None for i in line_idxs)

    if code_idx is not None:  # 영웅문 등 헤더 있는 형식
        for row in rows[1:]:
            code = extract_code(cell(row, code_idx))
            if not code or code in seen:
                continue
            seen.add(code)
            lines = parse_lines(row, line_idxs) if has_lines else None
            row = repair_tag_split(row)
            result.append(
                (
                    code,
                    cell(row, name_idx) or code,
                    cell(row, memo_idx),
                    lines,
                    _parse_tags(cell(row, tag_idx)),
                    cell(row, base_idx),
                )
            )
        return result

    for row in rows:  # 헤더 없는 형식: 휴리스틱
        code = name = None
        for i, c0 in enumerate(row):
            code = extract_code(c0)
            if not code:
                continue
            for j in list(range(i + 1, len(row))) + list(
                range(i)
            ):  # 코드 뒤 → 앞 순서로 탐색
                c = row[j].strip().strip('"')
                if len(c) >= 2 and not _NUMERIC_CELL.match(c):
                    name = c
                    break
            break
        if code and code not in seen:
            seen.add(code)
            result.append((code, name or code, "", None, "", ""))
    return result


def _width_in_chars(widget, sample: str) -> int:
    """`sample` 이 잘리지 않는 width 값 (글자 수 단위).

    Tk 의 width 는 **'0' 문자 폭**을 단위로 센다. 한글은 그보다 1.5~2배 넓어, 글자 수를
    그대로 넣으면 폰트에 따라 뒷부분이 잘린다 — 리눅스에서는 아슬아슬하게 들어가고
    Windows 한글 폰트에서는 잘렸다(2026-08-17 실측). 폰트를 재서 환산한다.
    """
    try:
        metrics = tkfont.Font(font=widget.cget("font") or "TkDefaultFont")
        unit = metrics.measure("0") or 1
        return -(-metrics.measure(sample) // unit)  # 올림
    except tk.TclError:  # 폰트를 못 읽어도 창은 떠야 한다
        return len(sample) * 2


class App(tk.Tk):
    def __init__(self, b: bus.Bus):
        super().__init__()
        self._bus = b
        self._running = False
        self._mode_real = False
        self._funds: bus.Funds | None = None
        # symbol -> (name, params, position, memo)
        self._registry: dict[str, tuple[str, object, object, str]] = {}
        self._last_price: dict[str, float] = {}  # 평가손익 계산용
        self._staged: dict[str, str] = (
            {}
        )  # CSV 로 불러온 3선 미입력 종목 {코드: 종목명}
        self._backend_sim = False  # 상태 바 "(시뮬레이션)" 표기용
        self._last_tick: str = "--:--:--"
        self._current_date: str = datetime.now().strftime("%Y-%m-%d")

        self.title("three-line-trader")
        # 위젯을 만들기 **전에** 테마를 적용한다. 고전 tk 위젯(목록·코멘트·메뉴)은
        # 만들 때 색을 넣으므로, 뒤늦게 적용하면 그것들만 옛 색으로 남는다.
        self._theme_mode = theme.read_mode()
        theme.apply(self, self._theme_mode)
        self._set_icon()
        try:
            self.state("zoomed")  # Windows: 최대화 (FHD 전체화면)
        except tk.TclError:
            self.geometry("1600x900")

        self._build_toolbar()
        self._settings = ttk.Frame(self)  # 접이식 컨테이너 (연결 바 + 자금 바)
        self._settings.pack(fill="x", after=self._toolbar)
        self._build_settings(self._settings)
        self._build_main_area()
        self._build_status_bar()

        self.bind_all("<Button-1>", self._maybe_deselect, add="+")
        self.after(_POLL_MS, self._poll)
        self.after(1000, self._refresh_clock)

    # ── 화면 조립 ───────────────────────────────────────────────

    def _build_toolbar(self) -> None:
        self._toolbar = ttk.Frame(self, padding=(8, 5))
        self._toolbar.pack(fill="x")
        self._toggle_btn = ttk.Button(
            self._toolbar, text="감시 시작", command=self._toggle_run
        )
        self._toggle_btn.pack(side="left")
        # 매매일지는 로그 우클릭으로도 열 수 있지만, 하루에 한 번은 반드시 여는 화면이라
        # 툴바에도 둔다 (우클릭은 '그 종목의 일지', 이 버튼은 '전체 목록' 으로 역할이 다르다).
        ttk.Button(self._toolbar, text="매매일지", command=self._open_journal).pack(
            side="left", padx=(6, 0)
        )
        self._status = ttk.Label(
            self._toolbar, text="정지됨", foreground=theme.palette().muted
        )
        self._status.pack(side="right")
        pnl_box = ttk.Frame(self._toolbar)
        pnl_box.pack(side="right", padx=(0, 16))
        self._pnl_parts = {}
        for i, key in enumerate(("실현", "평가", "합계")):
            if i:
                ttk.Label(pnl_box, text=" · ").pack(side="left")
            lbl = ttk.Label(pnl_box, text=f"{key} -")
            lbl.pack(side="left")
            self._pnl_parts[key] = lbl
        self._mode_badge = ttk.Label(
            self._toolbar,
            text="모의투자",
            foreground=theme.palette().loss,
            font=("", 10, "bold"),
        )
        self._mode_badge.pack(side="right", padx=(0, 16))

    def _build_settings(self, parent: ttk.Frame) -> None:
        """설정 영역: 한 줄 5그룹. 그룹 내 컨텐츠는 상하 가운데 정렬,
        마지막 그룹이 남는 폭을 채워 오른쪽 여백을 없앤다."""
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=8, pady=(2, 4))
        muted = theme.palette().muted

        g_mode = ttk.LabelFrame(row, text="투자 모드", padding=(10, 2, 10, 6))
        g_mode.pack(side="left", fill="both", expand=True)
        box = ttk.Frame(g_mode)
        box.pack(expand=True)  # 상하 가운데 정렬
        self._mode_var = tk.StringVar(value="모의")
        self._mode_radios = []
        for text, pady in (("모의", (0, 2)), ("실전", 0)):
            rb = ttk.Radiobutton(
                box,
                text=text,
                value=text,
                variable=self._mode_var,
                command=self._on_mode_selected,
            )
            rb.pack(anchor="w", pady=pady)
            self._mode_radios.append(rb)

        g_date = ttk.LabelFrame(row, text="매매일", padding=(10, 2, 10, 6))
        g_date.pack(side="left", fill="both", expand=True, padx=(8, 0))
        box = ttk.Frame(g_date)
        box.pack(expand=True)
        self._date_var = tk.StringVar()
        line = ttk.Frame(box)
        line.pack(pady=(0, 3))
        self._date_prev = ttk.Button(
            line, text="◀", width=2, command=lambda: self._shift_date(-1)
        )
        self._date_prev.pack(side="left", padx=(0, 3))
        if DateEntry:  # 날짜 영역을 클릭해도 캘린더가 펼쳐지도록 바인딩
            self._date_picker = DateEntry(
                line,
                textvariable=self._date_var,
                date_pattern="yyyy-mm-dd",
                width=11,
                justify="center",
                state="readonly",
            )
            self._date_picker.pack(side="left")
            self._date_picker.bind(
                "<<DateEntrySelected>>", lambda _e: self._change_date()
            )
            self._date_picker.bind("<Button-1>", self._open_calendar)
        else:  # tkcalendar 미설치: 직접 입력 (Enter 로 이동)
            self._date_picker = None
            e = ttk.Entry(line, textvariable=self._date_var, width=12, justify="center")
            e.pack(side="left")
            e.bind("<Return>", lambda _e: self._change_date())
        self._date_next = ttk.Button(
            line, text="▶", width=2, command=lambda: self._shift_date(1)
        )
        self._date_next.pack(side="left", padx=(3, 0))
        # 폭을 고정한다. 날짜를 넘길 때마다 글자 길이가 달라지면 그룹 폭이 늘었다 줄었다
        # 하며 옆 그룹(키움·Discord·자금)이 밀린다.
        self._weekday = ttk.Label(box, text="-", anchor="center")
        self._weekday.configure(width=_width_in_chars(self._weekday, _MARKET_SAMPLE))
        self._weekday.pack(fill="x")

        g_kiwoom = ttk.LabelFrame(row, text="키움증권 API", padding=(10, 2, 10, 6))
        g_kiwoom.pack(side="left", fill="both", expand=True, padx=(8, 0))
        box = ttk.Frame(g_kiwoom)
        box.pack(expand=True)
        line = ttk.Frame(box)
        line.pack(fill="x", pady=(0, 3))
        self._kiwoom_connect_btn = ttk.Button(
            line, text="연결", width=6, command=self._connect_kiwoom
        )
        self._kiwoom_connect_btn.pack(side="left")
        self._kiwoom_status = ttk.Label(line, text="● 미연결", foreground=muted)
        self._kiwoom_status.pack(side="left", padx=(8, 0))
        line = ttk.Frame(box)
        line.pack(fill="x")
        ttk.Button(line, text="⟳", width=3, command=self._refresh_account).pack(
            side="right"
        )
        self._account = ttk.Label(line, text="주문가능 -")
        self._account.pack(side="right", padx=(0, 6))

        g_discord = ttk.LabelFrame(row, text="Discord", padding=(10, 2, 10, 6))
        g_discord.pack(side="left", fill="both", expand=True, padx=(8, 0))
        box = ttk.Frame(g_discord)
        box.pack(expand=True)
        line = ttk.Frame(box)
        line.pack(fill="x", pady=(0, 3))
        # 봇은 프로그램 시작과 함께 자동 연결되므로 버튼이 없다 (상태만 표시)
        self._discord_status = ttk.Label(
            line, text="● 연결 중...", foreground=theme.palette().warn
        )
        self._discord_status.pack(side="left")
        line = ttk.Frame(box)
        line.pack(fill="x")
        ttk.Label(line, text="알림", foreground=muted).pack(side="left")
        self._notify_combo = ttk.Combobox(
            line,
            values=["전체", "매매만 (시스템 제외)", "에러만", "끔"],
            state="readonly",
            width=15,
            justify="center",
        )
        self._notify_combo.set("전체")
        self._notify_combo.bind(
            "<<ComboboxSelected>>",
            lambda _e: self._bus.commands.put(
                bus.SetNotifyLevel(self._notify_combo.get())
            ),
        )
        self._notify_combo.pack(side="left", padx=(6, 0))

        g_strategy = ttk.LabelFrame(
            row, text="자금 배분 및 익절 전략", padding=(10, 2, 10, 6)
        )
        g_strategy.pack(side="left", fill="both", expand=True, padx=(8, 0))
        keys = (
            "total",
            "max",
            "buy1",
            "buy2",
            "rate1",
            "rate2",
            "rate3",
            "ratio1",
            "ratio2",
            "ratio3",
        )
        self._funds_vars = {k: tk.StringVar() for k in keys}
        box = ttk.Frame(g_strategy)
        box.pack(expand=True)

        grid = ttk.Frame(box)
        grid.pack(side="left")
        self._lock_widgets: list = []  # 감시 중 비활성화할 설정 위젯들
        for r, (label, key, width) in enumerate(
            [("총 운용금액", "total", 12), ("최대 종목", "max", 12)]
        ):
            ttk.Label(grid, text=label, foreground=muted).grid(
                row=r, column=0, sticky="e", padx=(0, 6)
            )
            e = ttk.Entry(
                grid, textvariable=self._funds_vars[key], width=width, justify="center"
            )
            e.grid(row=r, column=1, pady=1)
            if key == "total":
                self._make_money_entry(e, self._funds_vars[key])
            e.bind("<KeyRelease>", self._auto_fill_funds, add="+")
            self._lock_widgets.append(e)
        ttk.Label(grid, text="종목당", foreground=muted).grid(
            row=2, column=0, sticky="e", padx=(0, 6)
        )
        self._per_symbol = ttk.Label(grid, text="-", anchor="center")
        self._per_symbol.grid(row=2, column=1)
        ttk.Label(grid, text="매수 금액", foreground=muted).grid(row=0, column=4)
        for r, key in [(1, "buy1"), (2, "buy2")]:
            ttk.Label(grid, text=f"{r}차", foreground=muted).grid(
                row=r, column=3, sticky="e", padx=(16, 6)
            )
            e = ttk.Entry(
                grid, textvariable=self._funds_vars[key], width=11, justify="center"
            )
            e.grid(row=r, column=4, pady=1)
            self._make_money_entry(e, self._funds_vars[key])
            self._lock_widgets.append(e)

        ttk.Separator(box, orient="vertical").pack(
            side="left", fill="y", padx=12, pady=2
        )

        grid = ttk.Frame(box)
        grid.pack(side="left")
        for col, text in enumerate(["1차", "2차", "3차"], start=1):
            ttk.Label(grid, text=text, foreground=muted).grid(row=0, column=col)
        for r, (label, prefix) in enumerate(
            [("익절 %", "rate"), ("매도 비중 %", "ratio")], start=1
        ):
            ttk.Label(grid, text=label, foreground=muted).grid(
                row=r, column=0, sticky="e", padx=(0, 6)
            )
            for i in (1, 2, 3):
                e = ttk.Entry(
                    grid,
                    textvariable=self._funds_vars[f"{prefix}{i}"],
                    width=6,
                    justify="center",
                )
                e.grid(row=r, column=i, padx=2, pady=1)
                self._lock_widgets.append(e)

        self._apply_btn = ttk.Button(
            box, text="적용", width=6, command=self._apply_funds
        )
        self._apply_btn.pack(side="left", fill="y", padx=(12, 0), pady=2)

    def _build_main_area(self) -> None:
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=8, pady=(2, 0))
        monitor = ttk.Frame(paned)
        self._build_search_bar(monitor)
        self.positions = PositionsView(
            monitor,
            on_add=self._open_register,
            on_edit=self._open_edit,
            on_reset=self._reset,
            on_delete=self._delete,
            on_chart=self._open_chart,
            on_csv=self._import_csv,
            on_carry=self._carry_over,
            on_carry_position=self._carry_position,
            on_manual_sell=self._manual_sell,
        )
        self.positions.pack(fill="both", expand=True)
        self.events = EventsView(
            paned,
            on_daily_summary=lambda: self._bus.commands.put(bus.RequestDailySummary()),
            on_journal=self._open_journal,
        )
        paned.add(monitor, weight=5)
        paned.add(self.events, weight=2)
        # Ctrl+F 는 **이 창에만** 건다. bind_all 로 걸면 매매일지·등록 창에서 눌러도
        # 메인 검색줄이 떠, 그 창의 자체 검색을 가로챈다.
        self.bind("<Control-f>", self._open_search)
        self.bind("<Control-F>", self._open_search)

    def _build_search_bar(self, parent: ttk.Frame) -> None:
        """종목 검색줄 — 평소에는 숨어 있고 Ctrl+F 로만 나타난다.

        살아 있는 감시 화면을 가리는 기능이라 늘 띄워 두지 않는다. 대신 **필터가 걸려
        있으면 절대 사라지지 않는다** — 검색줄이 없는데 종목만 줄어 있으면 왜 안 보이는지
        알 수 없기 때문이다. 그래서 '검색줄을 닫는 것 = 필터를 푸는 것' 으로 묶었다.

        표 위에 얹히는 보조 도구라 **세로를 최대한 적게 쓴다**. 버튼 여백을 없애고
        아래 간격도 최소로 뒀다. 한글 조합 중에는 Windows 가 입력칸 아래에 자기 창을
        띄워 표 머리글에 잠깐 걸칠 수 있는데(OS 가 그리는 창이라 Tk 로는 못 막는다),
        머리글은 잠깐 가려도 잃는 정보가 없어 세로 공간을 택했다.
        """
        self._search_bar = ttk.Frame(parent)
        # 검색줄은 표 위에 얹히는 보조 도구라 세로로 얇을수록 좋다. 기본 버튼은 위아래
        # 여백(3px)이 붙어 입력칸보다 커지고, 그만큼 줄 전체가 두꺼워진다.
        ttk.Style().configure("Search.TButton", padding=(2, 0))
        ttk.Label(self._search_bar, text="종목 검색").pack(side="left", padx=(2, 6))
        self._search = SearchEntry(
            self._search_bar, "종목명 또는 종목코드", self._on_search
        )
        # 폭을 묶어 둔다. 창 너비만큼 늘리면 입력칸만 덩그러니 길어져 보기 나쁘다.
        self._search.entry.configure(width=_SEARCH_CHARS)
        self._search.pack(side="left")
        self._search_count = ttk.Label(
            self._search_bar, text="", foreground=theme.palette().muted, width=12
        )
        self._search_count.pack(side="left", padx=(10, 0))
        # 오른쪽 끝 ✕ 는 **검색줄 닫기**. 입력칸 옆의 '내용 지우기' 는 아이콘이라
        # 같은 ✕ 가 둘 있는 혼란이 없다.
        ttk.Button(
            self._search_bar,
            text="✕",
            width=3,
            style="Search.TButton",
            command=self._close_search,
        ).pack(side="right", padx=(6, 2))
        # Esc 는 두 단계다. 글자가 있으면 글자만 지우고(필터만 풀림), 비어 있으면 줄을 닫는다.
        # **검색칸에 포커스가 있을 때만** 반응한다 — 표에서 누른 Esc 로 필터가 풀리면 놀란다.
        self._search.entry.bind("<Escape>", self._on_search_escape)
        # Enter 는 조합 중인 한글을 확정시킨다. 확정되면 입력칸 내용이 바뀌어 필터가 걸린다.
        self._search.entry.bind("<Return>", lambda _e: (self._on_search(), "break")[1])

    def _open_search(self, _event=None) -> str:
        self._search_bar.pack(fill="x", pady=(0, _IME_GAP_PX), before=self.positions)
        self._search.take_focus()
        return "break"

    def _close_search(self, _event=None) -> str:
        """검색줄을 닫는다 = 필터도 함께 푼다."""
        self._search.clear()
        self._search_bar.pack_forget()
        return "break"

    def _on_search_escape(self, _event=None) -> str:
        if self._search.get():
            self._search.clear()  # 1단계: 글자만 지운다 (줄은 남는다)
        else:
            self._close_search()  # 2단계: 줄을 닫는다
        return "break"

    def _on_search(self) -> None:
        query = self._search.get()
        shown = self.positions.set_filter(query)
        total = self.positions.count()
        self._search_count.configure(text="" if not query else f"{shown}/{total}종목")

    def _build_status_bar(self) -> None:
        bar = ttk.Frame(self, padding=(8, 3))
        bar.pack(fill="x", side="bottom")
        self._ws_label = ttk.Label(
            bar, text="● WS 미연결", foreground=theme.palette().muted
        )
        self._ws_label.pack(side="left", padx=(0, 12))
        self._tick_label = ttk.Label(bar, text="마지막 틱 --:--:--")
        self._tick_label.pack(side="left", padx=(0, 12))
        self._market_label = ttk.Label(bar, text="")
        self._market_label.pack(side="left")
        self._summary = ttk.Label(bar, text="")
        self._summary.pack(side="right", padx=(0, 12))
        # 테마 선택 — 자주 바꾸는 항목이 아니라 툴바·설정 줄의 자리를 뺏지 않는다
        self._theme_var = tk.StringVar(value=self._theme_mode)
        picker = ttk.Combobox(
            bar,
            textvariable=self._theme_var,
            state="readonly",
            width=6,
            values=list(theme.MODES),
        )
        picker.pack(side="right", padx=(0, 12))  # 옆 '감시 N종목' 과 붙지 않게
        picker.bind("<<ComboboxSelected>>", self._on_theme_change)
        ttk.Label(bar, text="테마", foreground=theme.palette().muted).pack(
            side="right", padx=(0, 4)
        )

    def _on_theme_change(self, _event=None) -> None:
        """테마 설정 저장 — 실제 적용은 다시 켤 때다.

        이미 만들어진 고전 tk 위젯(목록·코멘트·메뉴)은 색이 자동으로 바뀌지 않는다.
        위젯 트리를 훑어 다시 칠할 수도 있지만 놓치는 곳이 생기기 쉬워, 재시작을
        안내하는 쪽이 확실하다.
        """
        mode = self._theme_var.get()
        if mode == self._theme_mode:
            return
        theme.write_mode(mode)
        self._theme_mode = mode
        messagebox.showinfo(
            "테마", f"'{mode}' 로 저장했습니다.\n다음에 프로그램을 켤 때 적용됩니다."
        )

    def _set_icon(self) -> None:
        """창 아이콘 — 모든 창이 같은 것을 쓴다 (trader.ui.icons 참고)."""
        apply_icon(self)

    # ── 사용자 조작 → 명령 큐 ───────────────────────────────────

    def _toggle_run(self) -> None:
        if not self._running and self._staged:
            messagebox.showwarning(
                "감시 시작 불가",
                f"3선 가격이 입력되지 않은 종목이 {len(self._staged)}개 있습니다.\n"
                "각 종목의 ✎ 를 눌러 가격을 입력하거나 ✕ 로 제외한 뒤 시작하세요.",
            )
            return
        self._bus.commands.put(bus.SetRunning(not self._running))

    def _open_register(self) -> None:
        if self._running:
            messagebox.showwarning(
                "변경 불가", "감시 중에는 변경할 수 없습니다. 먼저 중지하세요."
            )
            return
        if self._funds is None:
            messagebox.showwarning("안내", "전역 자금 설정이 로드되지 않았습니다.")
            return
        self._dialog = RegisterDialog(
            self,
            on_submit=self._submit_register,
            funds=self._funds,
            on_lookup=lambda s: (
                self._bus.commands.put(bus.LookupSymbol(s)) if s else None
            ),
        )

    def _open_edit(self, symbol: str | None) -> None:
        if not symbol or self._funds is None:
            return
        if self._running:
            messagebox.showwarning(
                "변경 불가", "감시 중에는 변경할 수 없습니다. 먼저 중지하세요."
            )
            return

        if symbol in self._staged:  # CSV 대기 종목: 3선 입력 → 정식 등록
            RegisterDialog(
                self,
                on_submit=self._submit_register,
                funds=self._funds,
                prefill=(symbol, self._staged[symbol]),
            )
            return
        if symbol not in self._registry:
            return
        name, params, pos, memo, tags, base_date = self._registry[symbol]
        RegisterDialog(
            self,
            on_submit=self._submit_register,
            funds=self._funds,
            edit=(symbol, name, params, pos, memo, tags, base_date),
        )

    def _import_csv(self) -> None:
        if self._running:
            messagebox.showwarning(
                "변경 불가", "감시 중에는 변경할 수 없습니다. 먼저 중지하세요."
            )
            return
        path = filedialog.askopenfilename(
            title="관심종목 CSV 선택",
            filetypes=[("CSV", "*.csv"), ("모든 파일", "*.*")],
        )
        if not path:
            return
        try:
            items = parse_watchlist_csv(path)
        except (OSError, ValueError) as e:
            messagebox.showerror("불러오기 실패", str(e))
            return
        registered = staged = 0
        rejected: list[str] = []  # 값이 잘못돼 등록하지 못한 종목 (사용자 입력 실수)
        added: list[dict] = []  # 등록 알림용 — 종목별 선정 근거를 함께 싣는다
        for code, name, memo, lines, tags, base_date in items:
            if code in self._registry or code in self._staged:
                continue
            if lines:  # CSV 에 1/2/3선이 채워져 있으면 곧바로 정식 등록
                from trader.state_machine import Params, Position

                try:
                    params = Params(
                        line1=lines[0],
                        line2=lines[1],
                        line3=lines[2],
                        buy1_amount=self._funds.buy1_amount,
                        buy2_amount=self._funds.buy2_amount,
                        tp_rates=self._funds.tp_rates,
                        tp_ratios=self._funds.tp_ratios,
                    )
                except ValueError as e:
                    rejected.append(
                        f"{name}({code}) 1선 {lines[0]:,.0f} / 2선 {lines[1]:,.0f} / "
                        f"3선 {lines[2]:,.0f} — {e}"
                    )
                    continue
                self._bus.commands.put(
                    bus.Register(
                        code,
                        name,
                        params,
                        Position(),
                        memo=memo,
                        tags=tags,
                        base_date=base_date,
                        quiet=True,
                    )
                )
                added.append(
                    {
                        "symbol": code,
                        "name": name,
                        "tags": tags,
                        "base_date": base_date,
                        "memo": memo,
                        "qty": int(params.buy1_amount // params.line1),
                    }
                )
                registered += 1
            else:
                self._staged[code] = name
                self.positions.upsert_staged(code, name, memo)
                staged += 1
        if not items:
            messagebox.showwarning("불러오기", "CSV 에서 종목코드를 찾지 못했습니다.")
            return
        skipped = len(items) - registered - staged - len(rejected)
        message = f"정식 등록 {registered}종목 · 3선 미입력 {staged}종목 (중복 {skipped}종목 제외)"
        if staged:
            message += (
                "\n미입력 종목은 ✎ 로 1·2·3선을 입력해야 감시를 시작할 수 있습니다."
            )

        # 팝업은 닫으면 사라진다 — 결과와 특히 '등록 실패' 는 로그·Discord 에도 남긴다
        # 알림은 한 번만 — 등록 결과 embed 안에 미입력·중복 건수까지 담는다
        if added or rejected or staged or skipped:
            self._bus.commands.put(
                bus.RegistrationNotice(tuple(added), tuple(rejected), staged, skipped)
            )

        if rejected:
            message += "\n\n등록하지 못한 종목 (값 확인 필요):\n· " + "\n· ".join(
                rejected
            )
            messagebox.showwarning("불러오기 완료 (일부 제외)", message)
            return
        messagebox.showinfo("불러오기 완료", message)

    def _reset(self, symbol: str | None) -> None:
        if symbol:
            self._bus.commands.put(bus.Reset(symbol))

    def _delete(self, symbol: str | None) -> None:
        # 확인창은 PositionsView 가 담당한다 (여기서 또 물으면 이중 확인)
        if not symbol:
            return
        if self._running:
            messagebox.showwarning(
                "변경 불가", "감시 중에는 변경할 수 없습니다. 먼저 중지하세요."
            )
            return

        if symbol in self._staged:  # 대기 종목은 코어에 없음 — UI 에서만 제거
            del self._staged[symbol]
            self.positions.remove(symbol)
            return
        self._bus.commands.put(bus.Delete(symbol))

    def _on_mode_selected(self, _event=None) -> None:
        want_real = self._mode_var.get() == "실전"
        if want_real == self._mode_real:
            return
        if self._running:
            messagebox.showwarning(
                "전환 불가", "감시 중에는 모드를 전환할 수 없습니다. 먼저 중지하세요."
            )
            self._mode_var.set("실전" if self._mode_real else "모의")
            return
        if want_real and not messagebox.askyesno(
            "실전투자 전환", "실전투자로 전환합니다.\n실제 주문이 나갑니다. 계속할까요?"
        ):
            self._mode_var.set("모의")
            return
        self._bus.commands.put(bus.SetMode(want_real))

    @staticmethod
    def _make_money_entry(entry: ttk.Entry, var: tk.StringVar) -> None:
        """숫자만 입력 허용 + 입력 중에도 세 자리 콤마 유지 (지웠다 다시 써도 적용)."""
        vcmd = (entry.register(lambda p: p == "" or p.replace(",", "").isdigit()), "%P")
        entry.configure(validate="key", validatecommand=vcmd)

        def reformat(_event=None):
            raw = var.get().replace(",", "")
            if raw.isdigit():
                var.set(f"{int(raw):,}")
                entry.icursor("end")

        entry.bind(
            "<KeyRelease>", reformat
        )  # 다른 KeyRelease 핸들러는 add="+" 로 뒤에 연결

    def _auto_fill_funds(self, _event=None) -> None:
        """총액/최대 종목 입력 시 종목당 배분 표시 및 1·2차 금액 절반씩 자동 채움."""
        try:
            total = float(self._funds_vars["total"].get().replace(",", "") or 0)
            max_n = int(self._funds_vars["max"].get() or 0)
            per = int(total // max_n) if max_n else 0  # 버림 — 배분 초과 원천 차단
        except ValueError:
            return
        half = per // 2  # 버림: 1차+2차 합이 항상 종목당 배분 이하
        self._per_symbol.configure(text=f"{per:,}")
        self._funds_vars["buy1"].set(f"{half:,}")
        self._funds_vars["buy2"].set(f"{half:,}")

    def _apply_funds(self) -> None:
        if self._running:
            messagebox.showwarning(
                "변경 불가", "감시 중에는 변경할 수 없습니다. 먼저 중지하세요."
            )
            return
        from trader.state_machine import Params  # 검증 규칙 재사용

        v = {
            k: var.get().replace(",", "").strip() for k, var in self._funds_vars.items()
        }
        try:
            total = float(v["total"])
            max_n = int(v["max"])
            buy1, buy2 = float(v["buy1"]), float(v["buy2"])
            rates = tuple(float(v[f"rate{i}"]) / 100 for i in (1, 2, 3))
            ratios = tuple(float(v[f"ratio{i}"]) / 100 for i in (1, 2, 3))
            if total <= 0 or max_n <= 0:
                raise ValueError("총 운용금액과 최대 종목 수는 0보다 커야 합니다")
            if buy1 + buy2 > total / max_n + 1e-9:
                raise ValueError(
                    f"1차+2차 금액이 종목당 배분({total / max_n:,.0f})을 초과합니다"
                )
            Params(
                line1=3,
                line2=2,
                line3=1,
                buy1_amount=max(buy1, 3),
                buy2_amount=max(buy2, 2),
                tp_rates=rates,
                tp_ratios=ratios,
            )  # 익절률·비중 규칙 검증
        except ValueError as e:
            messagebox.showerror("입력 오류", str(e))
            return
        self._bus.commands.put(bus.SetFunds(total, max_n, buy1, buy2, rates, ratios))

    def _shift_date(self, days: int) -> None:
        """매매일 하루 이동 (◀ 전일 / ▶ 다음일). 감시 중이면 _change_date 가 막는다."""
        new = datetime.strptime(self._current_date, "%Y-%m-%d") + timedelta(days=days)
        self._set_date_display(new.strftime("%Y-%m-%d"))
        self._change_date()

    def _open_calendar(self, _event):
        """날짜든 화살표든 클릭 한 번 = 캘린더 토글 한 번.
        기본 화살표 동작과 겹치면 이중 토글(열림→닫힘)이 되므로 'break' 로 차단한다."""
        self._date_picker.drop_down()
        return "break"

    def _change_date(self) -> None:
        d = self._date_var.get().strip()
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            messagebox.showerror(
                "입력 오류", "매매일은 YYYY-MM-DD 형식으로 입력하세요."
            )
            return
        if self._running:
            messagebox.showwarning(
                "전환 불가", "감시 중에는 매매일을 전환할 수 없습니다. 먼저 중지하세요."
            )
            self._set_date_display(self._current_date)  # 선택을 원래 날짜로 되돌림
            return
        self._bus.commands.put(bus.SetTradeDate(d))

    def _set_date_display(self, d: str, market: str = "", note: str = "") -> None:
        dt = datetime.strptime(d, "%Y-%m-%d")
        if self._date_picker:
            self._date_picker.set_date(dt)
        else:
            self._date_var.set(d)
        weekday = "월화수목금토일"[dt.weekday()]
        # 개장 여부는 코어가 판정해 보내준다(휴장일 목록 + 지수 일봉). 화면은 받은 대로
        # 보여주기만 한다 — 감시 게이트와 같은 근거를 써야 화면과 동작이 어긋나지 않는다.
        text = f"({weekday})"
        color = ""
        if market:
            # 사유에 이미 괄호가 있어(광복절(대체휴일)) 다시 감싸면 겹쳐 보인다
            text += f" · {market}" + (f" · {note}" if note else "")
            color = _market_color(market)
        self._weekday.configure(text=text, foreground=color)

    def _manual_sell(self, symbol: str) -> None:
        if symbol in self._staged or symbol not in self._registry:
            return
        name, _, pos, *_ = self._registry[symbol]
        if pos.remaining <= 0:
            messagebox.showinfo("청산 불가", "청산할 잔량이 없습니다.")
            return
        if messagebox.askyesno(
            "수동 전량 청산",
            f"{symbol}({name}) 잔량 {pos.remaining}주를 시장가로 전량 청산할까요?\n"
            "체결 후 상태는 '종료'가 됩니다.",
        ):
            self._bus.commands.put(bus.ManualSell(symbol))

    def _carry_position(self, symbol: str) -> None:
        """포지션만 이월 — 다음 매매일의 3선·메모는 그대로 두고 상태·평단·수량만 덮어쓴다."""
        self._bus.commands.put(bus.CarryPosition(symbol))

    def _carry_over(self, symbol: str) -> None:
        if symbol in self._staged:
            messagebox.showwarning("이월 불가", "3선 미입력 종목은 이월할 수 없습니다.")
            return
        self._bus.commands.put(bus.CarryOver(symbol))

    def _submit_register(self, cmd: bus.Register) -> None:
        """등록 창 제출 — 신규 등록이 기존 종목을 덮어쓰지 않게 여기서 한 번 더 막는다."""
        if cmd.position is not None and not cmd.edit and cmd.symbol in self._registry:
            messagebox.showwarning(
                "중복 종목",
                f"{cmd.symbol} 은 이미 등록되어 있습니다.\n수정하려면 편집(✎)을 사용하세요.",
            )
            return
        self._bus.commands.put(cmd)

    def _open_journal(self, select: tuple[str, str] | None = None) -> None:
        """매매일지 창 — 목록은 코어에서 받아 채운다 (JournalEntries 이벤트).

        처음에는 **기본 기간만** 요청한다. 전체를 들고 오면 기록이 쌓일수록 창이 뜨는 데
        오래 걸리고, 그 조회가 코어 스레드에서 돌아 매매 판정까지 밀린다(2026-08-12).
        """
        import datetime as dt

        from trader.ui.journal_dialog import period_range

        self._journal_select = select
        today = dt.date.today()  # 창의 기본값과 같은 기간 (이번 달)
        since, until = period_range(str(today.year), f"{today.month:02d}")
        self._bus.commands.put(bus.RequestJournal(since, until))

    def _request_journal_period(self, since: str, until: str) -> None:
        self._bus.commands.put(bus.RequestJournal(since, until))

    def _show_journal(self, entries: tuple, months: tuple = ()) -> None:
        """일지 창을 띄우거나, 이미 열려 있으면 새 기간의 목록으로 갈아끼운다."""
        from trader.ui.journal_dialog import JournalDialog

        dialog = getattr(self, "_journal_dialog", None)
        if dialog is not None and dialog.winfo_exists():
            dialog.set_entries([dict(e) for e in entries], months)
            dialog.lift()
            return
        if not entries:
            messagebox.showinfo("매매일지", "아직 기록할 매매가 없습니다.")
            return
        self._journal_dialog = JournalDialog(
            self,
            [dict(e) for e in entries],
            on_save=self._save_journal,
            select=getattr(self, "_journal_select", None),
            on_period=self._request_journal_period,
            months=months,
        )

    def _save_journal(self, trade_date: str, symbol: str, good: str, bad: str) -> None:
        self._bus.commands.put(bus.SaveJournal(trade_date, symbol, good, bad))

    def _open_chart(self, symbol: str) -> None:
        if symbol in self._staged:
            messagebox.showinfo("안내", "3선 미입력 종목은 차트를 만들 수 없습니다.")
            return
        self._bus.commands.put(
            bus.ChartRequest(symbol)
        )  # 완료되면 ChartReady 로 창이 뜬다

    def _show_chart(
        self, symbol: str, name: str, daily_path: str, minute_path: str
    ) -> None:
        """생성된 PNG 를 탭 2개짜리 창으로 표시 (휠 확대·드래그 이동)."""
        win = tk.Toplevel(self)
        win.title(f"{name}({symbol}) 복기 차트")
        apply_icon(win)
        win.geometry("980x900")
        notebook = ttk.Notebook(win)
        notebook.pack(fill="both", expand=True)

        for label, path in (("일봉", daily_path), ("3분봉", minute_path)):
            view = ChartView(notebook, path)
            notebook.add(view, text=label)

        bar = ttk.Frame(win)
        bar.pack(fill="x", pady=4)
        ttk.Label(
            bar,
            text="휠: 확대·축소 · 드래그: 이동 · 더블클릭: 원본 열기",
            foreground=theme.palette().muted,
        ).pack(side="left", padx=8)
        ttk.Button(
            bar,
            text="Discord로 보내기",
            command=lambda: (
                self._bus.commands.put(
                    bus.SendChartDiscord(symbol, (daily_path, minute_path))
                ),
                messagebox.showinfo("전송", "Discord 전송을 요청했습니다.", parent=win),
            ),
        ).pack(side="right", padx=8)

    def _connect_kiwoom(self) -> None:
        self._bus.commands.put(bus.ConnectKiwoom())
        self._kiwoom_status.configure(
            text="● 연결 중...", foreground=theme.palette().warn
        )

    def _refresh_account(self) -> None:
        self._bus.commands.put(bus.RefreshAccount())

    # ── 이벤트 큐 → 화면 갱신 ───────────────────────────────────

    def _poll(self) -> None:
        """이벤트 큐 → 화면. **어떤 일이 있어도 다음 폴링을 예약한다.**

        예전에는 queue.Empty 만 잡아서, 화면 갱신 중 예외가 하나만 나도 이 함수가
        그대로 빠져나가 다시 예약되지 않았다. 그러면 매매는 계속 도는데(코어는 별도
        스레드다) **화면만 그 시점에 멈춘다** — 체결도 로그도 손익도 갱신되지 않아
        사용자가 실제 상태를 모른 채 판단하게 된다. 돈이 걸린 화면에서 가장 위험한
        고장 방식이라, 개별 이벤트 처리 실패가 루프를 끊지 못하게 막는다
        (2026-08-13: PositionsView.set_blocked 누락으로 실제 발생).
        """
        try:
            while True:
                event = self._bus.events.get_nowait()
                try:
                    self._dispatch(event)
                except Exception:  # 이벤트 하나가 실패해도 나머지는 계속 처리한다
                    self._report_ui_error(event)
        except queue.Empty:
            pass
        except Exception:  # 큐 자체가 이상해도 폴링은 살아 있어야 한다
            self._report_ui_error(None)
        finally:
            self.after(_POLL_MS, self._poll)

    def _report_ui_error(self, event) -> None:
        """화면 갱신 실패를 조용히 넘기지 않고 눈에 보이게 남긴다.

        조용히 삼키면 '화면이 멈추지는 않지만 값이 틀린' 더 나쁜 상태가 된다.
        로그 창과 터미널 양쪽에 남겨 재현·수정이 가능하게 한다.
        """
        name = type(event).__name__ if event is not None else "이벤트 큐"
        detail = traceback.format_exc()
        print(f"[UI 오류] {name} 처리 실패\n{detail}", file=sys.stderr)
        try:
            self.events.append(
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "시스템",
                "",
                "오류",
                f"화면 갱신 실패({name}) — 매매는 계속됩니다. 로그를 확인하세요",
            )
        except Exception:  # 로그 위젯마저 실패하면 터미널 출력으로 끝낸다
            pass

    def _dispatch(self, ev) -> None:
        match ev:
            case bus.PositionUpdate(
                symbol=s,
                name=n,
                position=p,
                params=prm,
                memo=memo,
                tags=tags,
                base_date=base_date,
                day_open=day_open,
                base_days=base_days,
            ):
                self._staged.pop(s, None)  # 3선 입력 완료 → 대기 해제
                self._registry[s] = (n, prm, p, memo, tags, base_date)
                self.positions.set_day_open(s, day_open)
                self.positions.upsert(s, n, p, prm, memo, base_days)
                self._update_summary()
                self._update_pnl()
            case bus.Tick(symbol=s, price=p):
                self.positions.tick(s, p)
                self._last_price[s] = p
                self._last_tick = datetime.now().strftime("%H:%M:%S")
                self._tick_label.configure(text=f"마지막 틱 {self._last_tick}")
                self._update_pnl()
            case bus.LogLine(ts=ts, symbol=s, kind=k, text=t):
                name = self._registry[s][0] if s in self._registry else "-"
                self.events.append(ts, s, name, k, t)
            case bus.SymbolRemoved(symbol=s):
                self._registry.pop(s, None)
                self.positions.remove(s)
                self._update_summary()
            case bus.WatchStatus(running=r):
                self._running = r
                self._set_settings_locked(r)
                self._toggle_btn.configure(text="중지" if r else "감시 시작")
                self._status.configure(
                    text="감시 중" if r else "정지됨",
                    foreground=theme.palette().ok if r else theme.palette().muted,
                )
                self._ws_label.configure(
                    text=(
                        (
                            "● WS 수신 중"
                            + (" (시뮬레이션)" if self._backend_sim else "")
                        )
                        if r
                        else "● WS 미연결"
                    ),
                    foreground=theme.palette().ok if r else theme.palette().muted,
                )
            case bus.Funds() as f:
                self._funds = f
                self._funds_vars["total"].set(f"{f.total:,.0f}")
                self._funds_vars["max"].set(str(f.max_symbols))
                self._funds_vars["buy1"].set(f"{f.buy1_amount:,.0f}")
                self._funds_vars["buy2"].set(f"{f.buy2_amount:,.0f}")
                for i in (1, 2, 3):
                    self._funds_vars[f"rate{i}"].set(f"{f.tp_rates[i - 1] * 100:g}")
                    self._funds_vars[f"ratio{i}"].set(f"{f.tp_ratios[i - 1] * 100:g}")
                self._per_symbol.configure(text=f"{int(f.total // f.max_symbols):,}")
            case bus.TradeDate(date=d, market=market, market_note=note):
                self._current_date = d
                self._set_date_display(d, market, note)
                self.events.clear_view()
                self._staged.clear()
                self._registry.clear()
                self._last_price.clear()
                self.positions.clear()
                self._update_summary()
                self._update_pnl()
            case bus.NotifyLevel(level=lv):
                self._notify_combo.set(lv)
            case bus.Blocked(symbol=s, active=on, reason=why):
                self.positions.set_blocked(s, on, why)
            case bus.JournalEntries(entries=entries, months=months):
                self._show_journal(entries, months)
            case bus.ChartReady(symbol=s, name=n, daily_path=dp, minute_path=mp):
                self._show_chart(s, n, dp, mp)
            case bus.DiscordStatus(connected=ok, detail=detail):
                self._discord_status.configure(
                    text="● 연결됨" if ok else f"● 미연결 · {detail}",
                    foreground=theme.palette().ok if ok else theme.palette().muted,
                )
            case bus.SymbolInfo(symbol=s, name=n):
                if getattr(self, "_dialog", None) and self._dialog.winfo_exists():
                    self._dialog.set_name(s, n)
            case bus.KiwoomStatus(connected=ok, detail=detail):
                self._backend_sim = "시뮬레이션" in detail
                self._kiwoom_status.configure(
                    text=f"● 연결됨 · {detail}" if ok else f"● 미연결 · {detail}",
                    foreground=theme.palette().ok if ok else theme.palette().muted,
                )
            case bus.Account(deposit=d, account=acct):
                # 이 값은 '주문가능금액'(= 현금 + 당일 매도대금 재사용분)이다.
                # 영웅문 [예수금] 탭 숫자와 다르므로 이름을 정확히 적는다.
                prefix = f"{acct} · " if acct else ""
                self._account.configure(text=f"{prefix}주문가능 {d:,.0f}")
            case bus.Mode(real=real):
                self._mode_real = real
                self._mode_var.set("실전" if real else "모의")
                self._mode_badge.configure(
                    text="실전투자" if real else "모의투자",
                    foreground=theme.palette().profit if real else theme.palette().loss,
                )
                self._update_summary()

    def _update_summary(self) -> None:
        holding = sum(
            1
            for entry in self._registry.values()
            if entry[2].state not in (State.WAITING, State.CLOSED)
        )
        self._summary.configure(
            text=f"감시 {len(self._registry)}종목 · 보유 {holding}종목"
        )

    def _set_settings_locked(self, locked: bool) -> None:
        """감시 중에는 매매 조건에 영향을 주는 설정 위젯을 시각적으로도 잠근다.
        (주문가능금액 새로고침·알림 수준은 매매와 무관하므로 항상 허용)"""
        state = "disabled" if locked else "normal"
        widgets = (
            self._lock_widgets
            + self._mode_radios
            + [
                self._date_prev,
                self._date_next,
                self._apply_btn,
                self._kiwoom_connect_btn,
            ]
        )
        for w in widgets:
            w.configure(state=state)
        if self._date_picker:
            self._date_picker.configure(state="disabled" if locked else "readonly")

    def _maybe_deselect(self, event) -> None:
        """리스트 바깥(또는 리스트의 빈 영역) 클릭 시 행 선택 해제."""
        if isinstance(event.widget, tk.Menu):
            return  # 우클릭 메뉴 조작은 유지
        for view in (self.positions, self.events):
            if event.widget is view.tree:
                if not view.tree.identify_row(event.y):  # 트리 내부의 빈 영역
                    view.deselect()
                return  # 행 클릭은 해당 트리의 선택 동작에 맡김
        self.positions.deselect()
        self.events.deselect()

    def _update_pnl(self) -> None:
        realized = sum(entry[2].realized_pnl for entry in self._registry.values())
        unrealized = invested = 0.0
        for s, entry in self._registry.items():
            p = entry[2]
            if p.remaining and s in self._last_price:
                unrealized += (self._last_price[s] - p.avg_price) * p.remaining
            invested += p.avg_price * p.total_bought
        total = realized + unrealized
        rate = f" ({total / invested:+.2%})" if invested else ""
        values = {
            "실현": (realized, ""),
            "평가": (unrealized, ""),
            "합계": (total, rate),
        }
        for key, (
            value,
            suffix,
        ) in values.items():  # 항목별로 수익 빨강 / 손실 파랑 / 0 기본색
            c = theme.palette()
            color = c.profit if value > 0 else (c.loss if value < 0 else "")
            self._pnl_parts[key].configure(
                text=f"{key} {value:+,.0f}{suffix}", foreground=color
            )

    def _refresh_clock(self) -> None:
        now = datetime.now()
        if now.weekday() >= 5:
            phase = "휴장 (주말)"
        elif now.time() < dtime(9, 0):
            phase = "장전"
        elif now.time() <= dtime(15, 30):
            phase = "장중 (15:30 마감)"
        else:
            phase = "장 마감"
        self._market_label.configure(text=phase)
        self.after(1000, self._refresh_clock)
