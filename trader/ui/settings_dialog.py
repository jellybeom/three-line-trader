"""매매 설정 다이얼로그 — 자주 바꾸는 값만 모은다.

툴바에 흩어져 있던 자금·익절과, `config.toml` 에만 있던 거래비용, 그리고 알림 수준을
한자리에 모았다. 툴바는 **지금 상태**(실전인가·연결됐나·감시 중인가)만 남기고, 설정은
전부 다이얼로그로 뺀다 — 매일 보는 화면에 매일 안 바꾸는 값이 자리를 차지하고 있었다.

**환경 설정과 나눈 이유.** 투자 모드·API 키·스케줄은 몇 달에 한 번 건드리는 값이고,
여기 있는 것은 자금이 바뀔 때마다 손대는 값이다. 한 창에 두면 매일 여는 자리에서
토큰 칸을 실수로 건드릴 위험이 생긴다.

특히 **투자 모드는 여기 두지 않는다.** 모드를 바꾸면 DB 가 갈리는데(`_open_store`),
자금 설정은 그 DB 의 `settings` 테이블에 저장된다. 한 창에서 둘을 함께 저장하면
'자금 먼저냐 모드 먼저냐' 에 따라 값이 옛 DB 로 새는 순서 의존이 생긴다.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from trader.ui import theme
from trader.ui.icons import apply_icon

NOTIFY_LEVELS = ("전체", "매매만 (시스템 제외)", "끔")

# (키, 라벨, 폭). 자금은 DB 의 settings 에, 거래비용은 config.toml 에 저장된다 —
# 사용자에게는 한 창이지만 저장 경로가 다르다.
FUND_FIELDS = (
    ("total", "총 운용금액", 12),
    ("max", "최대 종목 수", 6),
    ("buy1", "1차 매수", 11),
    ("buy2", "2차 매수", 11),
)


def parse_funds(values: dict) -> tuple:
    """입력 → (총액, 최대 종목, 1차, 2차, 익절률, 비중). 어긋나면 ValueError.

    다이얼로그와 기존 툴바 경로가 **같은 함수**를 쓴다. 규칙이 두 벌이 되면 한쪽만
    고쳐져 조용히 갈라진다.
    """
    from trader.state_machine import Params  # 검증 규칙 재사용

    try:
        total = float(values["total"])
        max_n = int(values["max"])
        buy1, buy2 = float(values["buy1"]), float(values["buy2"])
        rates = tuple(float(values[f"rate{i}"]) / 100 for i in (1, 2, 3))
        ratios = tuple(float(values[f"ratio{i}"]) / 100 for i in (1, 2, 3))
    except (KeyError, ValueError) as err:
        raise ValueError(f"숫자를 확인하세요: {err}") from err
    if total <= 0 or max_n <= 0:
        raise ValueError("총 운용금액과 최대 종목 수는 0보다 커야 합니다")
    if buy1 + buy2 > total / max_n + 1e-9:
        raise ValueError(
            f"1차+2차 금액이 종목당 배분({total / max_n:,.0f})을 초과합니다"
        )
    Params(  # 익절률·비중 규칙 검증 (오름차순·합계 100%)
        line1=3,
        line2=2,
        line3=1,
        buy1_amount=max(buy1, 3),
        buy2_amount=max(buy2, 2),
        tp_rates=rates,
        tp_ratios=ratios,
    )
    return total, max_n, buy1, buy2, rates, ratios


def parse_fees(values: dict) -> tuple[float, float]:
    """입력 → (수수료율, 거래세율). 화면은 %, 저장은 소수다."""
    try:
        commission = float(values["commission"]) / 100
        tax = float(values["tax"]) / 100
    except (KeyError, ValueError) as err:
        raise ValueError(f"거래비용은 숫자로 입력하세요: {err}") from err
    if not 0 <= commission < 0.01 or not 0 <= tax < 0.01:
        raise ValueError("수수료·거래세는 0% 이상 1% 미만이어야 합니다")
    return commission, tax


class TradeSettingsDialog(tk.Toplevel):
    """자금·익절·거래비용·알림 수준.

    저장 버튼 하나로 모두 반영한다. 항목마다 따로 저장하면 어느 것이 반영됐는지
    알 수 없어, '저장했는데 안 바뀐다' 는 혼란이 생긴다.
    """

    def __init__(self, master, values: dict, on_save, running: bool = False):
        super().__init__(master)
        self.title("매매 설정")
        self.transient(master)
        self.resizable(False, False)
        # 테마는 부모(App)에서 물려받는다 — ttk 스타일은 인터프리터 단위라 다시 칠할
        # 필요가 없다. 아이콘만 붙인다.
        apply_icon(self)
        self._on_save = on_save
        self._running = running
        self._vars: dict[str, tk.StringVar] = {}

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        self._build_funds(body, values)
        self._build_takeprofit(body, values)
        self._build_fees(body, values)
        self._build_notify(body, values)
        self._build_buttons(body)

        if running:
            # 감시 중에는 진입 시점의 조건으로 끝까지 가야 한다. 도중에 3선이나 매수
            # 금액이 바뀌면 이미 들어간 종목의 판정 근거가 흔들린다.
            ttk.Label(
                body,
                text="감시 중에는 저장할 수 없습니다 — 먼저 중지하세요.",
                foreground=theme.palette().loss,
            ).pack(anchor="w", pady=(8, 0))

        self.bind("<Escape>", lambda _e: self.destroy())
        self.bind("<Return>", lambda _e: self._save())
        self.update_idletasks()
        self.grab_set()

    # ── 화면 조립 ───────────────────────────────────────────────

    def _section(self, parent, title: str) -> ttk.Frame:
        frame = ttk.LabelFrame(parent, text=title, padding=(10, 6))
        frame.pack(fill="x", pady=(0, 10))
        return frame

    def _entry(self, parent, key: str, value, width: int) -> ttk.Entry:
        var = tk.StringVar(value="" if value is None else str(value))
        self._vars[key] = var
        entry = ttk.Entry(parent, textvariable=var, width=width, justify="center")
        return entry

    def _build_funds(self, parent, values: dict) -> None:
        frame = self._section(parent, "자금")
        grid = ttk.Frame(frame)
        grid.pack()
        for col, (key, label, width) in enumerate(FUND_FIELDS):
            ttk.Label(grid, text=label).grid(row=0, column=col, padx=4, pady=(0, 2))
            self._entry(grid, key, values.get(key), width).grid(
                row=1, column=col, padx=4
            )
        self._per_symbol = ttk.Label(frame, text="", foreground=theme.palette().muted)
        self._per_symbol.pack(pady=(6, 0))
        for key in ("total", "max"):
            self._vars[key].trace_add("write", lambda *_a: self._update_per_symbol())
        self._update_per_symbol()

    def _build_takeprofit(self, parent, values: dict) -> None:
        frame = self._section(parent, "익절")
        grid = ttk.Frame(frame)
        grid.pack()
        for row, (prefix, label) in enumerate(
            (("rate", "익절 %"), ("ratio", "비중 %"))
        ):
            ttk.Label(grid, text=label).grid(row=row, column=0, padx=(0, 8), pady=2)
            for i in (1, 2, 3):
                self._entry(grid, f"{prefix}{i}", values.get(f"{prefix}{i}"), 6).grid(
                    row=row, column=i, padx=3, pady=2
                )
        ttk.Label(
            frame,
            text="3차는 남은 전량이 나갑니다. 소량이면 단계마다 1주씩.",
            foreground=theme.palette().muted,
        ).pack(pady=(6, 0))

    def _build_fees(self, parent, values: dict) -> None:
        frame = self._section(parent, "거래비용")
        grid = ttk.Frame(frame)
        grid.pack()
        for col, (key, label) in enumerate(
            (("commission", "수수료 %"), ("tax", "거래세 %"))
        ):
            ttk.Label(grid, text=label).grid(row=0, column=col, padx=6, pady=(0, 2))
            self._entry(grid, key, values.get(key), 10).grid(row=1, column=col, padx=6)
        ttk.Label(
            frame,
            text="모르면 높은 쪽으로. 낮게 잡으면 세후 손익이 실제보다 좋아 보입니다.",
            foreground=theme.palette().muted,
        ).pack(pady=(6, 0))

    def _build_notify(self, parent, values: dict) -> None:
        frame = self._section(parent, "Discord 알림")
        self._notify = ttk.Combobox(
            frame, values=list(NOTIFY_LEVELS), state="readonly", width=20
        )
        self._notify.set(values.get("notify_level") or NOTIFY_LEVELS[1])
        self._notify.pack()

    def _build_buttons(self, parent) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(4, 0))
        ttk.Button(bar, text="취소", command=self.destroy).pack(side="right")
        self._save_btn = ttk.Button(bar, text="저장", command=self._save)
        self._save_btn.pack(side="right", padx=(0, 6))
        if self._running:
            self._save_btn.state(["disabled"])

    # ── 계산·검증 ───────────────────────────────────────────────

    def _update_per_symbol(self) -> None:
        """종목당 배분액을 실시간으로 보여 준다 — 1·2차 금액을 정할 때의 상한이다."""
        try:
            total = float(self._vars["total"].get().replace(",", "") or 0)
            count = int(self._vars["max"].get() or 0)
            text = f"종목당 {total / count:,.0f}원" if total and count else ""
        except (ValueError, ZeroDivisionError):
            text = ""
        self._per_symbol.configure(text=text)

    def values(self) -> dict:
        out = {k: v.get().replace(",", "").strip() for k, v in self._vars.items()}
        out["notify_level"] = self._notify.get()
        return out

    def _save(self) -> None:
        if self._running:
            return
        try:
            self._on_save(self.values())
        except ValueError as err:
            messagebox.showwarning("설정 오류", str(err), parent=self)
            return
        self.destroy()
