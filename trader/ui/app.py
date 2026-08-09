"""메인 윈도우 (FHD 최적화) — 화면 구성:

  [툴바]      감시 시작/중지 · 손익 요약 · 상태
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
import os
import queue
import re
import tkinter as tk
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

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
from trader.ui.events_view import EventsView
from trader.ui.positions_view import PositionsView
from trader.ui.register_dialog import RegisterDialog

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
        if not looks_tagged:
            expect_date = base_idx is not None and base_idx > tag_idx
            candidate = rest[base_idx - tag_idx - 1] if expect_date and rest else ""
            if not _DATE_PATTERN.match(candidate.strip()):
                return row  # 태그가 아닌 값이 섞여 있으면 손대지 않는다
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


_ASSETS = Path(__file__).resolve().parents[2] / "assets"


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
        self._status = ttk.Label(self._toolbar, text="정지됨", foreground="#9e9e9e")
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
            self._toolbar, text="모의투자", foreground="#1565c0", font=("", 10, "bold")
        )
        self._mode_badge.pack(side="right", padx=(0, 16))

    def _build_settings(self, parent: ttk.Frame) -> None:
        """설정 영역: 한 줄 5그룹. 그룹 내 컨텐츠는 상하 가운데 정렬,
        마지막 그룹이 남는 폭을 채워 오른쪽 여백을 없앤다."""
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=8, pady=(2, 4))
        muted = "#9e9e9e"

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
        self._weekday = ttk.Label(box, text="-", anchor="center")
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
            line, text="● 연결 중...", foreground="#f9a825"
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
        self.positions = PositionsView(
            paned,
            on_add=self._open_register,
            on_edit=self._open_edit,
            on_reset=self._reset,
            on_delete=self._delete,
            on_chart=self._open_chart,
            on_csv=self._import_csv,
            on_carry=self._carry_over,
            on_manual_sell=self._manual_sell,
        )
        self.events = EventsView(
            paned,
            on_daily_summary=lambda: self._bus.commands.put(bus.RequestDailySummary()),
        )
        paned.add(self.positions, weight=5)
        paned.add(self.events, weight=2)

    def _build_status_bar(self) -> None:
        bar = ttk.Frame(self, padding=(8, 3))
        bar.pack(fill="x", side="bottom")
        self._ws_label = ttk.Label(bar, text="● WS 미연결", foreground="#9e9e9e")
        self._ws_label.pack(side="left", padx=(0, 12))
        self._tick_label = ttk.Label(bar, text="마지막 틱 --:--:--")
        self._tick_label.pack(side="left", padx=(0, 12))
        self._market_label = ttk.Label(bar, text="")
        self._market_label.pack(side="left")
        self._summary = ttk.Label(bar, text="")
        self._summary.pack(side="right")

    def _set_icon(self) -> None:
        """윈도우 아이콘: Windows 는 .ico, 그 외 플랫폼은 .png 로 적용."""
        try:
            self.iconbitmap(_ASSETS / "three-line-trader.ico")
        except tk.TclError:
            png = _ASSETS / "three-line-trader-512.png"
            if png.exists():
                self._icon_image = tk.PhotoImage(file=png)  # GC 방지로 참조 유지
                self.iconphoto(True, self._icon_image)

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

    def _set_date_display(self, d: str) -> None:
        dt = datetime.strptime(d, "%Y-%m-%d")
        if self._date_picker:
            self._date_picker.set_date(dt)
        else:
            self._date_var.set(d)
        weekday = "월화수목금토일"[dt.weekday()]
        self._weekday.configure(
            text=f"({weekday})", foreground="#f9a825" if dt.weekday() >= 5 else ""
        )  # 주말이면 주황 경고

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
        """생성된 PNG 를 탭 2개짜리 창으로 표시. 큰 이미지는 화면에 맞게 축소한다."""
        win = tk.Toplevel(self)
        win.title(f"{name}({symbol}) 복기 차트")
        notebook = ttk.Notebook(win)
        notebook.pack(fill="both", expand=True)
        win._photos = []  # PhotoImage 참조 유지 (없으면 즉시 사라짐)

        max_h = self.winfo_screenheight() - 160
        for label, path in (("일봉", daily_path), ("3분봉", minute_path)):
            frame = ttk.Frame(notebook)
            notebook.add(frame, text=label)
            try:
                photo = tk.PhotoImage(file=path)
            except tk.TclError as e:
                ttk.Label(frame, text=f"이미지를 열 수 없습니다: {e}").pack(
                    padx=20, pady=20
                )
                continue
            factor = -(-photo.height() // max_h)  # 올림 나눗셈
            if factor > 1:
                photo = photo.subsample(factor, factor)
            win._photos.append(photo)
            label_widget = ttk.Label(frame, image=photo, cursor="hand2")
            label_widget.pack()
            label_widget.bind(  # 더블클릭 → 기본 이미지 뷰어로 원본 열기 (Windows)
                "<Double-Button-1>",
                lambda _e, p=path: (
                    os.startfile(p) if hasattr(os, "startfile") else None
                ),
            )

        bar = ttk.Frame(win)
        bar.pack(fill="x", pady=4)
        ttk.Label(bar, text="더블클릭: 원본 크기로 열기", foreground="#9e9e9e").pack(
            side="left", padx=8
        )
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
        self._kiwoom_status.configure(text="● 연결 중...", foreground="#f9a825")

    def _refresh_account(self) -> None:
        self._bus.commands.put(bus.RefreshAccount())

    # ── 이벤트 큐 → 화면 갱신 ───────────────────────────────────

    def _poll(self) -> None:
        try:
            while True:
                self._dispatch(self._bus.events.get_nowait())
        except queue.Empty:
            pass
        self.after(_POLL_MS, self._poll)

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
            ):
                self._staged.pop(s, None)  # 3선 입력 완료 → 대기 해제
                self._registry[s] = (n, prm, p, memo, tags, base_date)
                self.positions.upsert(s, n, p, prm, memo)
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
                    foreground="#2e7d32" if r else "#9e9e9e",
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
                    foreground="#2e7d32" if r else "#9e9e9e",
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
            case bus.TradeDate(date=d):
                self._current_date = d
                self._set_date_display(d)
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
            case bus.ChartReady(symbol=s, name=n, daily_path=dp, minute_path=mp):
                self._show_chart(s, n, dp, mp)
            case bus.DiscordStatus(connected=ok, detail=detail):
                self._discord_status.configure(
                    text="● 연결됨" if ok else f"● 미연결 · {detail}",
                    foreground="#2e7d32" if ok else "#9e9e9e",
                )
            case bus.SymbolInfo(symbol=s, name=n):
                if getattr(self, "_dialog", None) and self._dialog.winfo_exists():
                    self._dialog.set_name(s, n)
            case bus.KiwoomStatus(connected=ok, detail=detail):
                self._backend_sim = "시뮬레이션" in detail
                self._kiwoom_status.configure(
                    text=f"● 연결됨 · {detail}" if ok else f"● 미연결 · {detail}",
                    foreground="#2e7d32" if ok else "#9e9e9e",
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
                    foreground="#c62828" if real else "#1565c0",
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
            color = "#c62828" if value > 0 else ("#1565c0" if value < 0 else "")
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
