"""실전 진입점 — 코어 스레드(asyncio) 기동 후 Tkinter 앱 실행. 조립만 담당한다.

    uv run main.py

연습 모드는 uv run simulate.py (가짜 틱, 즉시 체결 가정).
키움 연결에는 config.toml 이 필요하다 (config.toml.example 참고).
"""

from __future__ import annotations

import asyncio
import threading

from trader.ui import bus


def main() -> None:
    b = bus.Bus()

    def run_core() -> None:
        # Store(sqlite)는 반드시 사용할 스레드 안에서 생성한다 — Core.run() 내부에서 생성됨
        from trader.core import Core

        asyncio.run(Core(b).run())

    threading.Thread(target=run_core, daemon=True).start()

    from trader.ui.app import App

    App(b).mainloop()


if __name__ == "__main__":
    main()
