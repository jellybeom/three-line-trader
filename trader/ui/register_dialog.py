"""종목 등록/편집 창 — 3선·매수 금액·익절 설정과 시작 상태를 입력받는다.

- 익절률·비중은 각각 3칸으로 분리 입력 (1·2·3차)
- 매수 금액은 전역 자금 설정의 1·2차 금액이 기본값으로 채워진다
- 편집 모드: 종목코드는 잠기고, 시작 상태 영역은 숨긴다.
  포지션은 코어의 현재 값을 그대로 유지한다 (Register.position=None)

검증은 이 파일이 직접 하지 않는다. Params / Position 생성자가
규칙의 단일 출처이므로, 여기서는 입력을 넘기고 ValueError 를
메시지박스로 보여줄 뿐이다.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from trader.state_machine import Params, Position, State
from trader.ui.bus import Register

_HOLDING_STATES = [s for s in State if s not in (State.WAITING, State.CLOSED)]


class RegisterDialog(tk.Toplevel):
    def __init__(
        self,
        master,
        on_submit: Callable[[Register], None],
        default_amounts: tuple[float, float] = (0, 0),
        edit: (
            tuple[str, str, Params] | None
        ) = None,  # (symbol, name, params) — 편집 모드
    ):
        super().__init__(master)
        self._edit_mode = edit is not None
        self.title("종목 편집" if self._edit_mode else "관심종목 등록")
        self.resizable(False, False)
        self.grab_set()  # 모달
        self._on_submit = on_submit

        form = ttk.Frame(self, padding=12)
        form.pack(fill="both", expand=True)
        self._vars: dict[str, tk.StringVar] = {}
        row = 0

        def entry_row(
            label: str, key: str, default: str = "", width: int = 22
        ) -> ttk.Entry:
            nonlocal row
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=2)
            self._vars[key] = tk.StringVar(value=default)
            e = ttk.Entry(form, textvariable=self._vars[key], width=width)
            e.grid(row=row, column=1, sticky="w", pady=2)
            row += 1
            return e

        def triple_row(
            label: str, keys: list[str], defaults: list[str], hint: str
        ) -> None:
            nonlocal row
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=2)
            box = ttk.Frame(form)
            box.grid(row=row, column=1, sticky="w", pady=2)
            for key, default in zip(keys, defaults):
                self._vars[key] = tk.StringVar(value=default)
                ttk.Entry(box, textvariable=self._vars[key], width=6).pack(
                    side="left", padx=(0, 4)
                )
            ttk.Label(box, text=hint, foreground="#9e9e9e").pack(side="left")
            row += 1

        symbol_entry = entry_row("종목코드", "symbol")
        entry_row("종목명", "name")
        entry_row("1선 가격", "line1")
        entry_row("2선 가격", "line2")
        entry_row("3선 가격", "line3")
        entry_row(
            "1차 매수 금액",
            "buy1_amount",
            f"{default_amounts[0]:.0f}" if default_amounts[0] else "",
        )
        entry_row(
            "2차 매수 금액",
            "buy2_amount",
            f"{default_amounts[1]:.0f}" if default_amounts[1] else "",
        )
        triple_row("익절률 %", ["rate1", "rate2", "rate3"], ["3", "5", "7"], "1·2·3차")
        triple_row(
            "익절 비중 %", ["ratio1", "ratio2", "ratio3"], ["40", "50", "10"], "합 100"
        )
        entry_row("본절 버퍼 %", "buffer", "0", width=6)

        if self._edit_mode:
            symbol, name, params = edit
            self._vars["symbol"].set(symbol)
            symbol_entry.configure(state="disabled")
            self._vars["name"].set(name)
            for key, value in (
                ("line1", f"{params.line1:g}"),
                ("line2", f"{params.line2:g}"),
                ("line3", f"{params.line3:g}"),
                ("buy1_amount", f"{params.buy1_amount:g}"),
                ("buy2_amount", f"{params.buy2_amount:g}"),
                ("buffer", f"{params.breakeven_buffer * 100:g}"),
            ):
                self._vars[key].set(value)
            for i, key in enumerate(["rate1", "rate2", "rate3"]):
                self._vars[key].set(f"{params.tp_rates[i] * 100:g}")
            for i, key in enumerate(["ratio1", "ratio2", "ratio3"]):
                self._vars[key].set(f"{params.tp_ratios[i] * 100:g}")
        else:
            # 시작 상태 — 기본은 대기, 오버나이트 보유분은 직접 지정
            ttk.Label(form, text="시작 상태").grid(
                row=row, column=0, sticky="w", pady=(10, 2)
            )
            self._state = ttk.Combobox(
                form, values=[s.value for s in State], state="readonly", width=19
            )
            self._state.set(State.WAITING.value)
            self._state.grid(row=row, column=1, sticky="w", pady=(10, 2))
            self._state.bind("<<ComboboxSelected>>", self._toggle_holding_fields)
            row += 1

            self._holding_entries = [
                entry_row("평단가", "avg_price", "0"),
                entry_row("누적 매수량", "total_bought", "0"),
                entry_row("잔량", "remaining", "0"),
            ]
            for e in self._holding_entries:
                e.configure(state="disabled")

        ttk.Button(
            form, text="저장" if self._edit_mode else "등록", command=self._submit
        ).grid(row=row, column=0, columnspan=2, pady=(12, 0), sticky="ew")

    def _toggle_holding_fields(self, _event=None) -> None:
        holding = State(self._state.get()) in _HOLDING_STATES
        for entry in self._holding_entries:
            entry.configure(state="normal" if holding else "disabled")

    def _submit(self) -> None:
        v = {k: var.get().strip().replace(",", "") for k, var in self._vars.items()}
        try:
            if not v["symbol"]:
                raise ValueError("종목코드를 입력하세요")
            params = Params(
                line1=float(v["line1"]),
                line2=float(v["line2"]),
                line3=float(v["line3"]),
                buy1_amount=float(v["buy1_amount"]),
                buy2_amount=float(v["buy2_amount"]),
                tp_rates=tuple(float(v[k]) / 100 for k in ("rate1", "rate2", "rate3")),
                tp_ratios=tuple(
                    float(v[k]) / 100 for k in ("ratio1", "ratio2", "ratio3")
                ),
                breakeven_buffer=float(v["buffer"]) / 100,
            )
            if self._edit_mode:
                position = None  # 현재 포지션 유지
            else:
                state = State(self._state.get())
                if state is State.WAITING:
                    position = Position()
                elif state is State.CLOSED:
                    position = Position(state=State.CLOSED)
                else:
                    position = Position(
                        state=state,
                        avg_price=float(v["avg_price"]),
                        total_bought=int(v["total_bought"]),
                        remaining=int(v["remaining"]),
                    )
        except ValueError as e:
            messagebox.showerror("입력 오류", str(e), parent=self)
            return
        self._on_submit(
            Register(v["symbol"], v["name"] or v["symbol"], params, position)
        )
        self.destroy()
