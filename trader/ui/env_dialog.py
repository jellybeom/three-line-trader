"""환경 설정 다이얼로그 — `config.toml` 의 값들.

거의 안 건드리는 것만 모았다. 투자 모드·API 키·Discord 채널·스케줄·폰트다. 자주 바꾸는
자금·익절은 `settings_dialog.py` 에 따로 있다 — 한 창에 두면 매일 여는 자리에서 토큰
칸을 실수로 건드릴 위험이 생긴다.

**모드를 여기 둔 이유.** 모드는 "어느 계좌·어느 DB 를 쓰나" 라는 환경이지 매매 설정이
아니다. 그리고 자금 설정과 같은 창에 두면 안 된다 — 모드를 바꾸면 DB 가 갈리는데
(`_open_store`) 자금은 그 DB 의 `settings` 테이블에 저장되므로, 한 창에서 함께 저장하면
'자금 먼저냐 모드 먼저냐' 에 따라 값이 옛 DB 로 새는 순서 의존이 생긴다.

**비밀키는 가린다.** 원격 접속 화면이 노출되거나 화면을 공유할 때 토큰이 그대로 보인다.
저장된 값은 뒤 네 자리만 보여 주고, 사용자가 새로 입력할 때만 실제 값을 받는다.
"""

from __future__ import annotations

import re
import tkinter as tk
from tkinter import messagebox, ttk

from trader.ui.icons import apply_icon

MASK = "•" * 8
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# 화면에 채울 값. (설정 경로, 라벨, 비밀 여부)
KIWOOM_FIELDS = (
    ("appkey", "앱키", True),
    ("secretkey", "시크릿", True),
    ("account", "계좌 표기", False),
)
DISCORD_FIELDS = (
    ("discord.bot_token", "봇 토큰", True),
    ("discord.channel_id", "실시간 채널", False),
    ("discord.journal_channel_id", "매매일지 채널", False),
    ("discord.log_channel_id", "매매로그 채널", False),
)
SCHEDULE_FIELDS = (
    ("schedule.start", "감시 시작"),
    ("schedule.stop", "감시 중지"),
    ("schedule.summary", "요약 발송"),
)
FONT_FIELDS = (
    ("chart.font", "차트"),
    ("pdf.font", "PDF"),
    ("pdf.font_bold", "PDF 굵게"),
)


def mask(value: str) -> str:
    """저장된 비밀값을 `••••••••4f2a` 로. 빈 값은 빈 채로 둔다."""
    text = (value or "").strip()
    if not text:
        return ""
    return MASK + text[-4:] if len(text) > 4 else MASK


def is_masked(value: str) -> bool:
    """화면 값이 '안 건드린 것' 인가. 그러면 저장할 때 건너뛴다."""
    return value.startswith(MASK)


def parse_users(text: str) -> list[str]:
    """쉼표·줄바꿈·공백 어느 것으로 나눠도 받는다."""
    return [t for t in re.split(r"[,\s]+", text or "") if t]


def check_times(values: dict) -> None:
    """스케줄 시각 검증. 어긋나면 ValueError.

    순서까지 본다 — 중지가 시작보다 이르면 그날 감시가 아예 안 돌고, 요약이 중지보다
    이르면 아직 끝나지 않은 매매로 요약을 만든다.
    """
    times = {}
    for key, label in SCHEDULE_FIELDS:
        text = (values.get(key) or "").strip()
        if not TIME_PATTERN.match(text):
            raise ValueError(f"{label} 시각은 HH:MM 형식으로 입력하세요 (예: 08:55)")
        times[key] = text
    if (
        not times["schedule.start"]
        < times["schedule.stop"]
        <= times["schedule.summary"]
    ):
        raise ValueError("시각은 감시 시작 < 감시 중지 ≤ 요약 발송 순서여야 합니다")


class EnvSettingsDialog(tk.Toplevel):
    """투자 모드·API 키·Discord·스케줄·폰트."""

    def __init__(self, master, values: dict, on_save, on_mode, running: bool = False):
        super().__init__(master)
        self.title("환경 설정")
        self.transient(master)
        self.resizable(False, False)
        apply_icon(self)
        self._on_save, self._on_mode = on_save, on_mode
        self._running = running
        self._values = values
        self._vars: dict[str, tk.StringVar] = {}
        self._mode_real = bool(values.get("mode_real"))

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        self._build_mode(body)
        self._build_kiwoom(body, values)
        self._build_discord(body, values)
        self._build_schedule(body, values)
        self._build_fonts(body, values)
        ttk.Label(
            body,
            text="채널·스케줄·폰트는 재시작해야 반영됩니다.",
            foreground="#b26a00",
        ).pack(anchor="w", pady=(2, 8))
        self._build_buttons(body)

        self.bind("<Escape>", lambda _e: self.destroy())
        self.update_idletasks()
        self.grab_set()

    # ── 화면 조립 ───────────────────────────────────────────────

    def _section(self, parent, title: str) -> ttk.Frame:
        frame = ttk.LabelFrame(parent, text=title, padding=(10, 6))
        frame.pack(fill="x", pady=(0, 10))
        return frame

    def _row(self, parent, key: str, label: str, value, secret: bool, width=34) -> None:
        line = ttk.Frame(parent)
        line.pack(fill="x", pady=2)
        ttk.Label(line, text=label, width=12).pack(side="left")
        var = tk.StringVar(value=mask(value) if secret else (value or ""))
        self._vars[key] = var
        entry = ttk.Entry(line, textvariable=var, width=width)
        entry.pack(side="left", fill="x", expand=True)
        if secret:
            # **글자를 치기 시작할 때만** 지운다. `<FocusIn>` 으로 하면 탭을 눌러
            # 그 페이지가 보이기만 해도 포커스가 들어와 앱키가 사라진다
            # (2026-09-07 실측: 실전 키 ↔ 모의 키 탭 전환에서 내용이 날아갔다).
            entry.bind("<Key>", lambda e, v=var: self._clear_mask(e, v))

    @staticmethod
    def _clear_mask(event, var: tk.StringVar) -> str | None:
        """가려진 값에 처음 글자를 치면 통째로 지운다.

        뒤에 이어 쓰면 `••••4f2a새키` 가 저장되어 키가 망가진다. 이동·복사 키는
        내용을 바꾸려는 것이 아니므로 그냥 둔다.
        """
        if not is_masked(var.get()):
            return None
        # keysym 이 없는 이벤트도 온다(합성 이벤트·일부 IME). 없으면 일반 입력으로 본다.
        if getattr(event, "keysym", "") in (
            "Tab",
            "ISO_Left_Tab",
            "Shift_L",
            "Shift_R",
            "Control_L",
            "Control_R",
            "Alt_L",
            "Alt_R",
            "Left",
            "Right",
            "Up",
            "Down",
            "Home",
            "End",
            "Escape",
        ):
            return None
        var.set("")
        # keysym 이 없는 이벤트도 온다(합성 이벤트·일부 IME). 없으면 일반 입력으로 본다.
        if getattr(event, "keysym", "") in ("BackSpace", "Delete"):
            return "break"  # 지우려던 것이므로 여기서 끝낸다
        return None

    def _build_mode(self, parent) -> None:
        frame = self._section(parent, "투자 모드")
        self._mode_var = tk.StringVar(value="실전" if self._mode_real else "모의")
        line = ttk.Frame(frame)
        line.pack()
        for text in ("모의", "실전"):
            ttk.Radiobutton(
                line,
                text=f"{text}투자",
                value=text,
                variable=self._mode_var,
                command=self._switch_mode,
            ).pack(side="left", padx=8)
        if self._running:
            for child in line.winfo_children():
                child.state(["disabled"])

    def _build_kiwoom(self, parent, values: dict) -> None:
        frame = self._section(parent, "키움 API")
        tabs = ttk.Notebook(frame)
        tabs.pack(fill="x")
        for scope, title in (("real", "실전 키"), ("mock", "모의 키")):
            page = ttk.Frame(tabs, padding=(6, 6))
            tabs.add(page, text=title)
            for name, label, secret in KIWOOM_FIELDS:
                key = f"kiwoom.{scope}.{name}"
                self._row(page, key, label, values.get(key), secret)
        tabs.select(0 if self._mode_real else 1)  # 지금 모드 쪽을 먼저 보여 준다

    def _build_discord(self, parent, values: dict) -> None:
        frame = self._section(parent, "Discord")
        for key, label, secret in DISCORD_FIELDS:
            self._row(frame, key, label, values.get(key), secret)
        line = ttk.Frame(frame)
        line.pack(fill="x", pady=2)
        ttk.Label(line, text="허용 사용자", width=12).pack(side="left")
        users = values.get("discord.allowed_users") or []
        var = tk.StringVar(value=", ".join(str(u) for u in users))
        self._vars["discord.allowed_users"] = var
        ttk.Entry(line, textvariable=var, width=34).pack(
            side="left", fill="x", expand=True
        )

    def _build_schedule(self, parent, values: dict) -> None:
        frame = self._section(parent, "시작·스케줄")
        self._auto_connect = tk.BooleanVar(
            value=bool(values.get("startup.auto_connect"))
        )
        ttk.Checkbutton(
            frame,
            text="실행하면 키움·Discord 자동 연결",
            variable=self._auto_connect,
        ).pack(anchor="w")
        self._schedule_on = tk.BooleanVar(value=bool(values.get("schedule.enabled")))
        ttk.Checkbutton(
            frame, text="자동 스케줄 사용", variable=self._schedule_on
        ).pack(anchor="w", pady=(2, 4))
        grid = ttk.Frame(frame)
        grid.pack()
        for col, (key, label) in enumerate(SCHEDULE_FIELDS):
            ttk.Label(grid, text=label).grid(row=0, column=col, padx=6, pady=(0, 2))
            var = tk.StringVar(value=values.get(key) or "")
            self._vars[key] = var
            ttk.Entry(grid, textvariable=var, width=8, justify="center").grid(
                row=1, column=col, padx=6
            )

    def _build_fonts(self, parent, values: dict) -> None:
        frame = self._section(parent, "폰트")
        for key, label in FONT_FIELDS:
            self._row(frame, key, label, values.get(key), False, width=30)
        ttk.Label(
            frame,
            text="비우면 맑은 고딕을 찾고, 없으면 나눔고딕으로 물러납니다.",
            foreground="#7f858c",
        ).pack(anchor="w", pady=(4, 0))

    def _build_buttons(self, parent) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x")
        ttk.Button(bar, text="취소", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="저장", command=self._save).pack(side="right", padx=(0, 6))

    # ── 저장 ────────────────────────────────────────────────────

    def _switch_mode(self) -> None:
        """모드는 **저장 버튼과 무관하게 즉시** 처리한다.

        DB 교체·연결 해제라는 부수효과가 있어서, 다른 값과 함께 저장하면 순서에 따라
        결과가 달라진다. 확인창은 호출부(App)가 띄운다.
        """
        want_real = self._mode_var.get() == "실전"
        if want_real == self._mode_real:
            return
        if not self._on_mode(want_real):
            self._mode_var.set("실전" if self._mode_real else "모의")
            return
        self._mode_real = want_real

    def updates(self) -> dict:
        """`config.toml` 에 쓸 값만 추린다. **가려진 채 그대로인 비밀값은 뺀다.**"""
        out: dict = {}
        secrets = {k for k, _l, s in DISCORD_FIELDS if s} | {
            f"kiwoom.{scope}.{name}"
            for scope in ("real", "mock")
            for name, _l, s in KIWOOM_FIELDS
            if s
        }
        for key, var in self._vars.items():
            text = var.get().strip()
            if key in secrets and is_masked(text):
                continue  # 안 건드렸다 — 덮어쓰면 뒤 네 자리만 남는다
            if key == "discord.allowed_users":
                out[key] = parse_users(text)
            else:
                out[key] = text
        out["startup.auto_connect"] = bool(self._auto_connect.get())
        out["schedule.enabled"] = bool(self._schedule_on.get())
        return out

    def _save(self) -> None:
        updates = self.updates()
        try:
            check_times(updates)
            self._on_save(updates)
        except ValueError as err:
            messagebox.showwarning("설정 오류", str(err), parent=self)
            return
        self.destroy()
