"""종목 등록/편집 창 — 종목코드·종목명·3선 가격과 시작 상태만 입력받는다.

매수 금액·익절률·비중은 메인 화면의 전역 설정을 따른다 (종목별 입력 없음).
편집 모드: 종목코드는 잠기고, 시작 상태 영역은 숨긴다.
포지션은 코어의 현재 값을 그대로 유지한다 (Register.position=None).

검증은 Params / Position 생성자가 규칙의 단일 출처이므로,
여기서는 입력을 넘기고 ValueError 를 메시지박스로 보여줄 뿐이다.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

from trader.state_machine import Params, Position, State
from trader.ui import bus

_HOLDING_STATES = [s for s in State if s not in (State.WAITING, State.CLOSED)]
_ICON = Path(__file__).resolve().parents[2] / "assets" / "three-line-trader.ico"


class RegisterDialog(tk.Toplevel):
    def __init__(
        self,
        master,
        on_submit: Callable[[bus.Register], None],
        funds: bus.Funds,  # 전역 설정 (매수 금액·익절률·비중의 출처)
        edit: (
            tuple[str, str, Params] | None
        ) = None,  # (symbol, name, params) — 편집 모드
    ):
        super().__init__(master)
        self._edit_mode = edit is not None
        self.title("종목 편집" if self._edit_mode else "종목 추가")
        try:
            self.iconbitmap(_ICON)  # 메인 창과 아이콘 통일
        except tk.TclError:
            pass
        self.resizable(False, False)
        self.grab_set()  # 모달
        self._on_submit = on_submit
        self._funds = funds

        form = ttk.Frame(self, padding=12)
        form.pack(fill="both", expand=True)
        self._vars: dict[str, tk.StringVar] = {}
        row = 0

        def entry_row(label: str, key: str, default: str = "") -> ttk.Entry:
            nonlocal row
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=2)
            self._vars[key] = tk.StringVar(value=default)
            e = ttk.Entry(form, textvariable=self._vars[key], width=22)
            e.grid(row=row, column=1, sticky="w", pady=2)
            row += 1
            return e

        symbol_entry = entry_row("종목코드", "symbol")
        entry_row("종목명", "name")
        entry_row("1선 가격", "line1")
        entry_row("2선 가격", "line2")
        entry_row("3선 가격", "line3")

        ttk.Label(
            form,
            foreground="#9e9e9e",
            justify="left",
            text=(
                f"매수 금액·익절 설정은 전역 설정을 따릅니다\n"
                f"1차 {funds.buy1_amount:,.0f} · 2차 {funds.buy2_amount:,.0f} · "
                f"익절 {'/'.join(f'{r:.0%}' for r in funds.tp_rates)}"
            ),
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 2))
        row += 1

        if self._edit_mode:
            symbol, name, params = edit
            self._vars["symbol"].set(symbol)
            symbol_entry.configure(state="disabled")
            self._vars["name"].set(name)
            for key, value in (
                ("line1", f"{params.line1:g}"),
                ("line2", f"{params.line2:g}"),
                ("line3", f"{params.line3:g}"),
            ):
                self._vars[key].set(value)
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
                buy1_amount=self._funds.buy1_amount,
                buy2_amount=self._funds.buy2_amount,
                tp_rates=self._funds.tp_rates,
                tp_ratios=self._funds.tp_ratios,
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
            bus.Register(v["symbol"], v["name"] or v["symbol"], params, position)
        )
        self.destroy()
