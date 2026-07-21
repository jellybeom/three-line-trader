"""메인 윈도우 (FHD 최적화) — 화면 구성:

  [툴바]      감시 시작/중지 · 손익 요약 · 상태
  [설정]      한 줄 5그룹 (상시 표시):
              투자 모드 | 매매일(요일) | 키움 연결(예수금) | Discord(알림 수준)
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
_CODE_PATTERN = re.compile(r"^['\u2019A]*(\d{6})$")  # 영웅문은 '096770 처럼 따옴표 접두
_NUMERIC_CELL = re.compile(r"^[\d,.+\-%\s]*$")


def parse_watchlist_csv(path: str) -> list[tuple[str, str]]:
    """영웅문 관심종목 CSV → [(종목코드, 종목명)].

    1순위: 헤더 행에 '종목코드'/'종목명' 열이 있으면 그 열을 그대로 사용 (영웅문 형식).
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
        return m.group(1) if m else None

    result: list[tuple[str, str]] = []
    seen: set[str] = set()

    header = rows[0] if rows else []
    code_idx = next((i for i, c in enumerate(header) if "종목코드" in c), None)
    name_idx = next((i for i, c in enumerate(header) if "종목명" in c), None)

    if code_idx is not None:  # 영웅문 등 헤더 있는 형식
        for row in rows[1:]:
            if len(row) <= code_idx:
                continue
            code = extract_code(row[code_idx])
            if not code or code in seen:
                continue
            name = (
                row[name_idx].strip().strip('"')
                if (name_idx is not None and len(row) > name_idx)
                else ""
            )
            seen.add(code)
            result.append((code, name or code))
        return result

    for row in rows:  # 헤더 없는 형식: 휴리스틱
        code = name = None
        for i, cell in enumerate(row):
            code = extract_code(cell)
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
            result.append((code, name or code))
    return result


_ASSETS = Path(__file__).resolve().parents[2] / "assets"


class App(tk.Tk):
    def __init__(self, b: bus.Bus):
        super().__init__()
        self._bus = b
        self._running = False
        self._mode_real = False
        self._funds: bus.Funds | None = None
        self._registry: dict[str, tuple[str, object, object]] = (
            {}
        )  # symbol -> (name, params, position)
        self._last_price: dict[str, float] = {}  # 평가손익 계산용
        self._staged: dict[str, str] = (
            {}
        )  # CSV 로 불러온 3선 미입력 종목 {코드: 종목명}
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
        self._pnl_label = ttk.Label(self._toolbar, text="실현 - · 평가 - · 합계 -")
        self._pnl_label.pack(side="right", padx=(0, 16))
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
        self._account = ttk.Label(line, text="예수금 -")
        self._account.pack(side="right", padx=(0, 6))

        g_discord = ttk.LabelFrame(row, text="Discord", padding=(10, 2, 10, 6))
        g_discord.pack(side="left", fill="both", expand=True, padx=(8, 0))
        box = ttk.Frame(g_discord)
        box.pack(expand=True)
        line = ttk.Frame(box)
        line.pack(fill="x", pady=(0, 3))
        ttk.Button(line, text="연결", width=6, command=self._connect_discord).pack(
            side="left"
        )
        self._discord_status = ttk.Label(line, text="● 미연결", foreground=muted)
        self._discord_status.pack(side="left", padx=(8, 0))
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
        )
        self.events = EventsView(paned)
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
        name, params, _pos = self._registry[symbol]
        RegisterDialog(
            self,
            on_submit=self._bus.commands.put,
            funds=self._funds,
            edit=(symbol, name, params),
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
        added = 0
        for code, name in items:
            if code in self._registry or code in self._staged:
                continue
            self._staged[code] = name
            self.positions.upsert_staged(code, name)
            added += 1
        if not items:
            messagebox.showwarning("불러오기", "CSV 에서 종목코드를 찾지 못했습니다.")
            return
        messagebox.showinfo(
            "불러오기 완료",
            f"{added}종목을 불러왔습니다 (중복 {len(items) - added}종목 제외).\n"
            "각 종목의 ✎ 를 눌러 1·2·3선 가격을 입력해야 감시를 시작할 수 있습니다.",
        )

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

    def _carry_over(self, symbol: str) -> None:
        if symbol in self._staged:
            messagebox.showwarning("이월 불가", "3선 미입력 종목은 이월할 수 없습니다.")
            return
        self._bus.commands.put(bus.CarryOver(symbol))

    def _submit_register(self, cmd: bus.Register) -> None:
        """등록 창 제출 — 신규 등록이 기존 종목을 덮어쓰지 않게 여기서 한 번 더 막는다."""
        if cmd.position is not None and cmd.symbol in self._registry:
            messagebox.showwarning(
                "중복 종목",
                f"{cmd.symbol} 은 이미 등록되어 있습니다.\n수정하려면 편집(✎)을 사용하세요.",
            )
            return
        self._bus.commands.put(cmd)

    def _open_chart(self, symbol: str) -> None:
        messagebox.showinfo("안내", f"{symbol} 차트 보기는 추후 구현 예정입니다.")

    def _connect_kiwoom(self) -> None:
        self._bus.commands.put(bus.ConnectKiwoom())
        self._kiwoom_status.configure(text="● 연결 중...", foreground="#f9a825")

    def _refresh_account(self) -> None:
        self._bus.commands.put(bus.RefreshAccount())

    def _connect_discord(self) -> None:
        self._bus.commands.put(bus.ConnectDiscord())
        self._discord_status.configure(text="● 연결 중...", foreground="#f9a825")

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
            case bus.PositionUpdate(symbol=s, name=n, position=p, params=prm):
                self._staged.pop(s, None)  # 3선 입력 완료 → 대기 해제
                self._registry[s] = (n, prm, p)
                self.positions.upsert(s, n, p, prm)
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
                    text="● WS 수신 중 (시뮬레이션)" if r else "● WS 미연결",
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
                self._staged.clear()
                self._registry.clear()
                self._last_price.clear()
                self.positions.clear()
                self._update_summary()
                self._update_pnl()
            case bus.NotifyLevel(level=lv):
                self._notify_combo.set(lv)
            case bus.DiscordStatus(connected=ok, detail=detail):
                self._discord_status.configure(
                    text="● 연결됨" if ok else f"● 미연결 · {detail}",
                    foreground="#2e7d32" if ok else "#9e9e9e",
                )
            case bus.SymbolInfo(symbol=s, name=n):
                if getattr(self, "_dialog", None) and self._dialog.winfo_exists():
                    self._dialog.set_name(s, n)
            case bus.KiwoomStatus(connected=ok, detail=detail):
                self._kiwoom_status.configure(
                    text=f"● 연결됨 · {detail}" if ok else f"● 미연결 · {detail}",
                    foreground="#2e7d32" if ok else "#9e9e9e",
                )
            case bus.Account(deposit=d):
                self._account.configure(text=f"예수금 {d:,.0f}")
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
            for _, _, p in self._registry.values()
            if p.state not in (State.WAITING, State.CLOSED)
        )
        self._summary.configure(
            text=f"감시 {len(self._registry)}종목 · 보유 {holding}종목"
        )

    def _set_settings_locked(self, locked: bool) -> None:
        """감시 중에는 매매 조건에 영향을 주는 설정 위젯을 시각적으로도 잠근다.
        (예수금 새로고침·알림 수준·Discord 연결은 매매와 무관하므로 항상 허용)"""
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
        realized = sum(p.realized_pnl for _, _, p in self._registry.values())
        unrealized = invested = 0.0
        for s, (_, _, p) in self._registry.items():
            if p.remaining and s in self._last_price:
                unrealized += (self._last_price[s] - p.avg_price) * p.remaining
            invested += p.avg_price * p.total_bought
        total = realized + unrealized
        rate = f" ({total / invested:+.2%})" if invested else ""
        color = "#c62828" if total > 0 else ("#1565c0" if total < 0 else "#9e9e9e")
        self._pnl_label.configure(
            text=f"실현 {realized:+,.0f} · 평가 {unrealized:+,.0f} · 합계 {total:+,.0f}{rate}",
            foreground=color,
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
