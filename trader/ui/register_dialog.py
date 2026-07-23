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

from trader.state_machine import Params, Position, State  # noqa: F401
from trader.ui import bus

_HOLDING_STATES = [s for s in State if s not in (State.WAITING, State.CLOSED)]
_ICON = Path(__file__).resolve().parents[2] / "assets" / "three-line-trader.ico"


class RegisterDialog(tk.Toplevel):
    def __init__(
        self,
        master,
        on_submit: Callable[[bus.Register], None],
        funds: bus.Funds,  # 전역 설정 (매수 금액·익절률·비중의 출처)
        edit: tuple[str, str, Params, Position, str] | None = None,
        # (symbol, name, params, position, memo) — 편집 모드: 상태·수량까지 수정 가능
        on_lookup: Callable[[str], None] | None = None,  # 종목코드 → 종목명 조회 요청
        prefill: (
            tuple[str, str] | None
        ) = None,  # (code, name) — CSV 대기 종목의 3선 입력
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

        def entry_row(
            label: str, key: str, default: str = "", numeric: bool = False
        ) -> ttk.Entry:
            nonlocal row
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=2)
            self._vars[key] = tk.StringVar(value=default)
            e = ttk.Entry(
                form, textvariable=self._vars[key], width=22, justify="center"
            )
            e.grid(row=row, column=1, sticky="ew", pady=2)
            if numeric:
                self._make_numeric(e, self._vars[key])
            row += 1
            return e

        ttk.Label(form, text="종목코드").grid(row=row, column=0, sticky="w", pady=2)
        self._vars["symbol"] = tk.StringVar()
        symbol_box = ttk.Frame(form)
        symbol_box.grid(row=row, column=1, sticky="ew", pady=2)
        symbol_entry = ttk.Entry(
            symbol_box, textvariable=self._vars["symbol"], justify="center"
        )
        symbol_entry.pack(side="left", fill="x", expand=True)  # 남는 폭을 채움
        if on_lookup and edit is None:  # 조회 버튼의 오른쪽 = 아래 입력칸들의 오른쪽 선
            ttk.Button(
                symbol_box,
                text="조회",
                width=5,
                command=lambda: on_lookup(self._vars["symbol"].get().strip()),
            ).pack(side="right", padx=(4, 0))
        row += 1
        entry_row("종목명", "name")
        entry_row("1선 가격", "line1", numeric=True)
        entry_row("2선 가격", "line2", numeric=True)
        entry_row("3선 가격", "line3", numeric=True)
        entry_row("메모", "memo")

        if prefill and not self._edit_mode:
            code, name = prefill
            self._vars["symbol"].set(code)
            symbol_entry.configure(state="disabled")  # CSV 에서 온 코드는 고정
            self._vars["name"].set(name)

        # 상태 — 등록: 시작 상태 지정 / 편집: 상태·평단·수량까지 수정 가능
        # (외부에서 직접 매도한 경우 등 계좌와 프로그램 상태를 맞추는 용도)
        ttk.Label(form, text="상태").grid(row=row, column=0, sticky="w", pady=(10, 2))
        self._state = ttk.Combobox(
            form,
            values=[s.value for s in State],
            state="readonly",
            width=19,
            justify="center",
        )
        self._state.set(State.WAITING.value)
        self._state.grid(row=row, column=1, sticky="ew", pady=(10, 2))
        self._state.bind("<<ComboboxSelected>>", self._toggle_holding_fields)
        row += 1

        self._holding_entries = [
            entry_row("평단가", "avg_price", "0", numeric=True),
            entry_row("누적 매수량", "total_bought", "0", numeric=True),
            entry_row("잔량", "remaining", "0", numeric=True),
        ]
        for e in self._holding_entries:
            e.configure(state="disabled")

        if self._edit_mode:
            symbol, name, params, pos, memo = edit
            self._prev_position = pos
            self._vars["symbol"].set(symbol)
            symbol_entry.configure(state="disabled")
            self._vars["name"].set(name)
            self._vars["memo"].set(memo)
            for key, value in (
                ("line1", f"{params.line1:,.0f}"),
                ("line2", f"{params.line2:,.0f}"),
                ("line3", f"{params.line3:,.0f}"),
                ("avg_price", f"{pos.avg_price:,.0f}"),
                ("total_bought", f"{pos.total_bought:,}"),
                ("remaining", f"{pos.remaining:,}"),
            ):
                self._vars[key].set(value)
            self._state.set(pos.state.value)
            self._toggle_holding_fields()

        ttk.Button(
            form, text="저장" if self._edit_mode else "등록", command=self._submit
        ).grid(row=row, column=0, columnspan=2, pady=(12, 0), sticky="ew")

    @staticmethod
    def _make_numeric(entry: ttk.Entry, var: tk.StringVar) -> None:
        """숫자만 입력 허용 + 세 자리 콤마 자동 적용."""
        vcmd = (entry.register(lambda p: p == "" or p.replace(",", "").isdigit()), "%P")
        entry.configure(validate="key", validatecommand=vcmd)

        def reformat(_event=None):
            raw = var.get().replace(",", "")
            if raw.isdigit():
                var.set(f"{int(raw):,}")
                entry.icursor("end")

        entry.bind("<KeyRelease>", reformat)

    def set_name(self, symbol: str, name: str) -> None:
        """조회 결과 수신 — 요청한 종목코드와 일치할 때만 채운다."""
        if self.winfo_exists() and self._vars["symbol"].get().strip() == symbol:
            self._vars["name"].set(name)

    def _toggle_holding_fields(self, _event=None) -> None:
        editable = State(self._state.get()) is not State.WAITING  # 대기만 0 고정
        for entry in self._holding_entries:
            entry.configure(state="normal" if editable else "disabled")

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
            state = State(self._state.get())
            realized = self._prev_position.realized_pnl if self._edit_mode else 0.0
            if state is State.WAITING:
                position = Position()
            else:
                position = Position(
                    state=state,
                    avg_price=float(v["avg_price"] or 0),
                    total_bought=int(v["total_bought"] or 0),
                    remaining=int(v["remaining"] or 0),
                    realized_pnl=realized,
                )  # 편집이 손익 기록을 지우지 않게 보존
        except ValueError as e:
            messagebox.showerror("입력 오류", str(e), parent=self)
            return
        self._on_submit(
            bus.Register(
                v["symbol"],
                v["name"] or v["symbol"],
                params,
                position,
                edit=self._edit_mode,
                memo=self._vars["memo"].get().strip(),
            )
        )
        self.destroy()
