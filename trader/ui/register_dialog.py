"""종목 등록 별도 창 — 3선·수량·익절 설정과 시작 상태를 입력받는다.

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
    def __init__(self, master, on_submit: Callable[[Register], None]):
        super().__init__(master)
        self.title("관심종목 등록")
        self.resizable(False, False)
        self.grab_set()  # 모달
        self._on_submit = on_submit

        form = ttk.Frame(self, padding=12)
        form.pack(fill="both", expand=True)
        self._vars: dict[str, tk.StringVar] = {}

        rows = [
            ("종목코드", "symbol", ""),
            ("종목명", "name", ""),
            ("1선 가격", "line1", ""),
            ("2선 가격", "line2", ""),
            ("3선 가격", "line3", ""),
            ("1차 매수 수량", "buy1_qty", ""),
            ("2차 매수 수량", "buy2_qty", ""),
            ("익절률 % (1,2,3차)", "tp_rates", "3, 5, 7"),
            ("익절 비중 % (1,2,3차)", "tp_ratios", "40, 50, 10"),
            ("본절 버퍼 %", "buffer", "0"),
        ]
        for i, (label, key, default) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=i, column=0, sticky="w", pady=2)
            self._vars[key] = tk.StringVar(value=default)
            ttk.Entry(form, textvariable=self._vars[key], width=24).grid(
                row=i, column=1, pady=2
            )

        # 시작 상태 — 기본은 대기, 오버나이트 보유분은 직접 지정
        row = len(rows)
        ttk.Label(form, text="시작 상태").grid(
            row=row, column=0, sticky="w", pady=(10, 2)
        )
        self._state = ttk.Combobox(
            form, values=[s.value for s in State], state="readonly", width=21
        )
        self._state.set(State.WAITING.value)
        self._state.grid(row=row, column=1, pady=(10, 2))
        self._state.bind("<<ComboboxSelected>>", self._toggle_holding_fields)

        self._holding_entries: list[ttk.Entry] = []
        for j, (label, key) in enumerate(
            [
                ("평단가", "avg_price"),
                ("누적 매수량", "total_bought"),
                ("잔량", "remaining"),
            ]
        ):
            ttk.Label(form, text=label).grid(
                row=row + 1 + j, column=0, sticky="w", pady=2
            )
            self._vars[key] = tk.StringVar(value="0")
            entry = ttk.Entry(
                form, textvariable=self._vars[key], width=24, state="disabled"
            )
            entry.grid(row=row + 1 + j, column=1, pady=2)
            self._holding_entries.append(entry)

        ttk.Button(form, text="등록", command=self._submit).grid(
            row=row + 4, column=0, columnspan=2, pady=(12, 0), sticky="ew"
        )

    def _toggle_holding_fields(self, _event=None) -> None:
        holding = State(self._state.get()) in _HOLDING_STATES
        for entry in self._holding_entries:
            entry.configure(state="normal" if holding else "disabled")

    def _submit(self) -> None:
        v = {k: var.get().strip() for k, var in self._vars.items()}
        try:
            if not v["symbol"]:
                raise ValueError("종목코드를 입력하세요")
            rates = tuple(float(x) / 100 for x in v["tp_rates"].split(","))
            ratios = tuple(float(x) / 100 for x in v["tp_ratios"].split(","))
            if len(rates) != 3 or len(ratios) != 3:
                raise ValueError("익절률/비중은 쉼표로 구분한 3개 값이어야 함")
            params = Params(
                line1=float(v["line1"]),
                line2=float(v["line2"]),
                line3=float(v["line3"]),
                buy1_qty=int(v["buy1_qty"]),
                buy2_qty=int(v["buy2_qty"]),
                tp_rates=rates,
                tp_ratios=ratios,
                breakeven_buffer=float(v["buffer"]) / 100,
            )
            state = State(self._state.get())
            position = (
                Position()
                if state is State.WAITING
                else Position(
                    state=state,
                    avg_price=float(v["avg_price"]),
                    total_bought=int(v["total_bought"]),
                    remaining=int(v["remaining"]),
                )
            )
        except ValueError as e:
            messagebox.showerror("입력 오류", str(e), parent=self)
            return
        self._on_submit(
            Register(v["symbol"], v["name"] or v["symbol"], params, position)
        )
        self.destroy()
