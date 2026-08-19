"""실전 진입점 — 중복 실행을 막고, 코어 스레드(asyncio) 기동 후 Tkinter 앱을 실행한다.

    uv run main.py

연습 모드는 uv run simulate.py (가짜 틱, 즉시 체결 가정).
키움 연결에는 config.toml 이 필요하다 (config.toml.example 참고).
"""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
from datetime import datetime
import tkinter as tk
from tkinter import messagebox

from trader.ui import bus

_LOCK_PORT = 47321  # 중복 실행 감지용 로컬 포트 (통신하지 않고 점유만 한다)


def acquire_single_instance() -> socket.socket | None:
    """이미 실행 중이면 None. 성공하면 소켓을 돌려주고 프로세스 수명 동안 점유한다.

    파일 잠금 대신 포트를 쓰는 이유: 프로그램이 비정상 종료돼도 OS 가 포트를 회수하므로
    잠금 찌꺼기가 남아 다음 실행을 막는 일이 없다. 두 인스턴스가 동시에 돌면 같은 조건에
    각각 주문을 내 이중 매수가 되므로, **주문이 가능한 코어를 띄우기 전에** 확인한다.
    """
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", _LOCK_PORT))
    except OSError:
        lock.close()
        return None
    lock.listen(1)
    return lock


def main() -> None:
    lock = acquire_single_instance()
    if lock is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "이미 실행 중",
            "three-line-trader 가 이미 실행 중입니다.\n"
            "두 개가 동시에 돌면 같은 조건에 주문이 두 번 나갈 수 있어 종료합니다.",
        )
        root.destroy()
        sys.exit(1)

    b = bus.Bus()

    def run_core() -> None:
        """코어 스레드. **죽더라도 조용히 죽지 않게** 한다.

        데몬 스레드라 여기서 예외가 나면 프로세스는 살아 있고 창도 멀쩡한데 매매만
        완전히 멈춘다. 콘솔 없이 start.bat 으로 띄우면 아무도 알아채지 못한다.
        그래서 마지막에 화면 로그로 알린다 — 이벤트 큐는 스레드와 무관하게 동작한다.
        """
        # Store(sqlite)는 반드시 사용할 스레드 안에서 생성한다 — Core.run() 내부에서 생성됨
        import traceback

        from trader.core import Core

        try:
            asyncio.run(Core(b).run())
        except BaseException:  # noqa: BLE001 — 무엇이든 사용자에게 알려야 한다
            detail = traceback.format_exc()
            print(f"[코어 중단]\n{detail}", file=sys.stderr)
            b.events.put(
                bus.LogLine(
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "시스템",
                    "에러",
                    "코어가 멈췄습니다 — 매매가 진행되지 않습니다. "
                    "프로그램을 다시 시작하세요 (자세한 내용은 로그 참고)",
                )
            )
            raise

    threading.Thread(target=run_core, daemon=True).start()

    from trader.ui.app import App

    try:
        App(b).mainloop()
    finally:
        lock.close()


if __name__ == "__main__":
    main()
