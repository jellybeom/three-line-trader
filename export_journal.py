"""매매일지를 마크다운으로 내보낸다.

매매 프로그램과 **따로 도는 읽기 전용 도구**다. 장중에 돌려도 안전하고, 실패해도
매매에 영향이 없다. 나중에 15:35 요약 뒤 자동 실행으로 옮길 때도 이 함수를 그대로
부르면 된다(2단계).

    uv run python export_journal.py                # 최근 매매일 하나
    uv run python export_journal.py 2026-08-21     # 그날만
    uv run python export_journal.py --month 2026-08
    uv run python export_journal.py --all
    uv run python export_journal.py --mock         # 모의투자 기록

DB 는 **프로그램과 같은 규칙**으로 고른다 — `data/mode.txt` 를 읽어 실전이면
`trader-real.db`, 모의면 `trader-mock.db`. 모드가 DB 안에 있으면 '어느 DB 를 열지'
정하는 데 순환이 생기므로 모드만 DB 밖에 있다(core._mode_file 참고).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from trader.core import db_path_for, read_mode
from trader.journal_export import export_day
from trader.store import Store
from trader.trading_calendar import TradingCalendar, load_holidays


def _dates(store: Store, args) -> list[str]:
    if args.date:
        return [args.date]
    known = store.recent_trade_dates(limit=10_000)
    if args.all:
        return sorted(known)
    if args.month:
        return sorted(d for d in known if d.startswith(args.month))
    return known[:1]  # 기본은 가장 최근 매매일 하나


def main(argv: list[str] | None = None) -> int:
    """argv 를 받는 이유: sync_journal.py 가 이 함수를 그대로 부른다."""
    parser = argparse.ArgumentParser(description="매매일지 마크다운 생성")
    parser.add_argument("date", nargs="?", default="", help="매매일 (YYYY-MM-DD)")
    parser.add_argument("--month", default="", help="그 달 전체 (YYYY-MM)")
    parser.add_argument("--all", action="store_true", help="기록이 있는 모든 매매일")
    parser.add_argument("--out", default="journal", help="문서를 쓸 폴더")
    parser.add_argument("--data", default="data", help="DB 폴더")
    parser.add_argument("--db", default="", help="DB 파일을 직접 지정 (모드 무시)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--real", action="store_true", help="실전 DB")
    mode.add_argument("--mock", action="store_true", help="모의 DB")
    args = parser.parse_args(argv)

    if args.db:
        db = args.db
    else:
        real = args.real or (not args.mock and read_mode(args.data))
        db = db_path_for(real, args.data)
    if not Path(db).exists():
        print(f"DB 를 찾을 수 없습니다: {db}", file=sys.stderr)
        return 1
    print(f"DB: {db}")

    store = Store(db)
    try:
        calendar = TradingCalendar()
        calendar.set_holidays(load_holidays())
        if saved := store.get_setting("trading_days", ""):
            calendar.replace(saved.split(","))

        dates = _dates(store, args)
        if not dates:
            print("내보낼 매매일이 없습니다.")
            return 0
        total = 0
        for date in dates:
            written = export_day(store, date, args.out, calendar)
            if written:
                docs = [p for p in written if p.suffix == ".md"]
                print(f"{date}  문서 {len(docs)}개 · 파일 {len(written)}개")
                total += len(written)
        print(f"\n{args.out}/ 에 {total}개 파일을 썼습니다.")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
