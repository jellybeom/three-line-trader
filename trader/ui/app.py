"""메인 윈도우 (FHD 최적화) — 화면 구성:

  [상시 툴바]  접기토글 · 모드배지 · 감시 시작/일시정지 · 등록/편집/삭제 · 상태
  [연결 바]↕   모드 선택 · 키움 키/연결/만료 · Discord 토큰/연결 · 계좌 요약
  [자금 바]↕   총 운용금액 · 최대 종목 → 종목당 배분 · 1/2차 금액 · 적용
  [모니터]     종목 테이블 (세로 대부분)
  [로그]
  [상태 바]    WS 상태 · 마지막 틱 · 장 운영 · 모드/종목 수

연결·자금 바(↕)만 접힌다 — 장중에 쓰는 조작은 항상 보인다.
역할은 화면 조립, 200ms 큐 폴링, 사용자 조작의 명령 큐 전달뿐이다.
키움/Discord 연결 버튼은 watcher/broker 구현 전까지 안내만 표시한다.
"""

from __future__ import annotations

import queue
import tkinter as tk
from datetime import datetime, time as dtime
from pathlib import Path
from tkinter import messagebox, ttk

from trader.state_machine import State
from trader.ui import bus

try:
    from tkcalendar import DateEntry  # 캘린더 드롭다운 (uv add tkcalendar)
except ImportError:
    DateEntry = None
from trader.ui.events_view import EventsView
from trader.ui.positions_view import PositionsView
from trader.ui.register_dialog import RegisterDialog

_POLL_MS = 200
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
        self._build_connection_bar(self._settings)
        self._build_funds_bar(self._settings)
        self._build_main_area()
        self._build_status_bar()

        self.bind_all("<Button-1>", self._maybe_deselect, add="+")
        self.after(_POLL_MS, self._poll)
        self.after(1000, self._refresh_clock)

    # ── 화면 조립 ───────────────────────────────────────────────

    def _build_toolbar(self) -> None:
        self._toolbar = ttk.Frame(self, padding=(8, 5))
        self._toolbar.pack(fill="x")
        self._fold_btn = ttk.Button(
            self._toolbar, text="▲ 설정", width=7, command=self._toggle_fold
        )
        self._fold_btn.pack(side="left")
        ttk.Label(self._toolbar, text="매매일").pack(side="left", padx=(10, 0))
        self._date_var = tk.StringVar()
        if DateEntry:  # 클릭 시 캘린더가 펼쳐지고, 날짜 선택 즉시 이동
            self._date_picker = DateEntry(
                self._toolbar,
                textvariable=self._date_var,
                date_pattern="yyyy-mm-dd",
                width=11,
                justify="center",
                state="readonly",
            )
            self._date_picker.pack(side="left", padx=(4, 12))
            self._date_picker.bind(
                "<<DateEntrySelected>>", lambda _e: self._change_date()
            )
        else:  # tkcalendar 미설치: 직접 입력 + 이동 버튼
            self._date_picker = None
            date_entry = ttk.Entry(
                self._toolbar, textvariable=self._date_var, width=11, justify="center"
            )
            date_entry.pack(side="left", padx=(4, 2))
            date_entry.bind("<Return>", lambda _e: self._change_date())
            ttk.Button(self._toolbar, text="이동", command=self._change_date).pack(
                side="left", padx=(0, 12)
            )
        self._toggle_btn = ttk.Button(
            self._toolbar, text="감시 시작", command=self._toggle_run
        )
        self._toggle_btn.pack(side="left", padx=(6, 0))
        self._status = ttk.Label(self._toolbar, text="정지됨", foreground="#9e9e9e")
        self._status.pack(side="right")
        self._pnl_label = ttk.Label(self._toolbar, text="실현 - · 평가 - · 합계 -")
        self._pnl_label.pack(side="right", padx=(0, 16))
        self._mode_badge = ttk.Label(
            self._toolbar, text="모의투자", foreground="#1565c0", font=("", 10, "bold")
        )
        self._mode_badge.pack(side="right", padx=(0, 16))

    def _build_connection_bar(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent, padding=(8, 3))
        bar.pack(fill="x")
        ttk.Label(bar, text="모드").pack(side="left")
        self._mode_combo = ttk.Combobox(
            bar, values=["모의투자", "실전투자"], state="readonly", width=8
        )
        self._mode_combo.set("모의투자")
        self._mode_combo.bind("<<ComboboxSelected>>", self._on_mode_selected)
        self._mode_combo.pack(side="left", padx=(4, 12))

        ttk.Label(bar, text="키움").pack(side="left")
        ttk.Button(bar, text="연결", command=self._connect_kiwoom).pack(
            side="left", padx=(4, 0)
        )
        self._kiwoom_status = ttk.Label(
            bar, text="● 미연결 (키: config.toml)", foreground="#9e9e9e"
        )
        self._kiwoom_status.pack(side="left", padx=(6, 14))

        ttk.Label(bar, text="Discord").pack(side="left")
        ttk.Button(bar, text="연결", command=self._connect_discord).pack(
            side="left", padx=(4, 0)
        )
        self._discord_status = ttk.Label(bar, text="● 미연결", foreground="#9e9e9e")
        self._discord_status.pack(side="left", padx=(6, 0))

        self._account = ttk.Label(bar, text="예수금 -")
        self._account.pack(side="right")
        ttk.Button(bar, text="⟳", width=3, command=self._refresh_account).pack(
            side="right", padx=(0, 4)
        )

    def _build_funds_bar(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent, padding=(8, 3))
        bar.pack(fill="x")
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

        def entry(label: str, key: str, width: int) -> None:
            ttk.Label(bar, text=label).pack(side="left")
            e = ttk.Entry(
                bar, textvariable=self._funds_vars[key], width=width, justify="right"
            )
            e.pack(side="left", padx=(4, 10))
            if key in ("total", "max"):  # 변경 시 종목당 배분·1/2차 금액 자동 채움
                e.bind("<KeyRelease>", self._auto_fill_funds)

        entry("총 운용금액", "total", 12)
        entry("최대 종목", "max", 4)
        self._per_symbol = ttk.Label(bar, text="→ 종목당 -")
        self._per_symbol.pack(side="left", padx=(0, 10))
        entry("1차", "buy1", 10)
        entry("2차", "buy2", 10)

        def triple(label: str, prefix: str) -> None:
            ttk.Label(bar, text=label).pack(side="left")
            for i in (1, 2, 3):
                ttk.Entry(
                    bar,
                    textvariable=self._funds_vars[f"{prefix}{i}"],
                    width=4,
                    justify="right",
                ).pack(side="left", padx=(3, 0))
            ttk.Label(bar, text=" ").pack(side="left")

        triple("익절%", "rate")
        triple("비중%", "ratio")
        ttk.Button(bar, text="적용", command=self._apply_funds).pack(side="left")

    def _build_main_area(self) -> None:
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=8, pady=(2, 0))
        self.positions = PositionsView(
            paned,
            on_add=self._open_register,
            on_edit=self._open_edit,
            on_reset=self._reset,
            on_delete=self._delete,
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

    def _toggle_fold(self) -> None:
        if self._settings.winfo_manager():
            self._settings.pack_forget()
            self._fold_btn.configure(text="▼ 설정")
        else:
            self._settings.pack(fill="x", after=self._toolbar)
            self._fold_btn.configure(text="▲ 설정")

    def _toggle_run(self) -> None:
        self._bus.commands.put(bus.SetRunning(not self._running))

    def _open_register(self) -> None:
        if self._funds is None:
            messagebox.showwarning("안내", "전역 자금 설정이 로드되지 않았습니다.")
            return
        RegisterDialog(self, on_submit=self._bus.commands.put, funds=self._funds)

    def _open_edit(self, symbol: str | None) -> None:
        if not symbol or symbol not in self._registry or self._funds is None:
            return
        name, params, _pos = self._registry[symbol]
        RegisterDialog(
            self,
            on_submit=self._bus.commands.put,
            funds=self._funds,
            edit=(symbol, name, params),
        )

    def _reset(self, symbol: str | None) -> None:
        if symbol:
            self._bus.commands.put(bus.Reset(symbol))

    def _delete(self, symbol: str | None) -> None:
        # 확인창은 PositionsView 가 담당한다 (여기서 또 물으면 이중 확인)
        if symbol:
            self._bus.commands.put(bus.Delete(symbol))

    def _on_mode_selected(self, _event=None) -> None:
        want_real = self._mode_combo.get() == "실전투자"
        if want_real == self._mode_real:
            return
        if self._running:
            messagebox.showwarning(
                "전환 불가",
                "감시 중에는 모드를 전환할 수 없습니다. 먼저 일시정지하세요.",
            )
            self._mode_combo.set("실전투자" if self._mode_real else "모의투자")
            return
        if want_real and not messagebox.askyesno(
            "실전투자 전환", "실전투자로 전환합니다.\n실제 주문이 나갑니다. 계속할까요?"
        ):
            self._mode_combo.set("모의투자")
            return
        self._bus.commands.put(bus.SetMode(want_real))

    def _auto_fill_funds(self, _event=None) -> None:
        """총액/최대 종목 입력 시 종목당 배분 표시 및 1·2차 금액 절반씩 자동 채움."""
        try:
            total = float(self._funds_vars["total"].get().replace(",", "") or 0)
            max_n = int(self._funds_vars["max"].get() or 0)
            per = total / max_n if max_n else 0
        except ValueError:
            return
        self._per_symbol.configure(text=f"→ 종목당 {per:,.0f}")
        self._funds_vars["buy1"].set(f"{per / 2:,.0f}")
        self._funds_vars["buy2"].set(f"{per / 2:,.0f}")

    def _apply_funds(self) -> None:
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
        if self._date_picker:
            self._date_picker.set_date(datetime.strptime(d, "%Y-%m-%d"))
        else:
            self._date_var.set(d)

    def _connect_kiwoom(self) -> None:
        self._bus.commands.put(bus.ConnectKiwoom())
        self._kiwoom_status.configure(text="● 연결 중...", foreground="#f9a825")

    def _refresh_account(self) -> None:
        self._bus.commands.put(bus.RefreshAccount())

    def _connect_discord(self) -> None:
        messagebox.showinfo("안내", "Discord 연결은 notifier 구현 후 동작합니다.")

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
                self.events.append(ts, s, k, t)
            case bus.SymbolRemoved(symbol=s):
                self._registry.pop(s, None)
                self.positions.remove(s)
                self._update_summary()
            case bus.WatchStatus(running=r):
                self._running = r
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
                self._per_symbol.configure(
                    text=f"→ 종목당 {f.total / f.max_symbols:,.0f}"
                )
            case bus.TradeDate(date=d):
                self._current_date = d
                self._set_date_display(d)
                self._registry.clear()
                self._last_price.clear()
                self.positions.clear()
                self._update_summary()
                self._update_pnl()
            case bus.KiwoomStatus(connected=ok, detail=detail):
                self._kiwoom_status.configure(
                    text=f"● 연결됨 · {detail}" if ok else f"● 미연결 · {detail}",
                    foreground="#2e7d32" if ok else "#9e9e9e",
                )
            case bus.Account(deposit=d):
                self._account.configure(text=f"예수금 {d:,.0f}")
            case bus.Mode(real=real):
                self._mode_real = real
                self._mode_combo.set("실전투자" if real else "모의투자")
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
        mode = "실전투자" if self._mode_real else "모의투자"
        self._summary.configure(
            text=f"{mode} · 감시 {len(self._registry)}종목 · 보유 {holding}종목"
        )

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
