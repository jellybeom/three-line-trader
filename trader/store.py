"""SQLite 영속화 — 종목 설정·포지션 저장/복원, 이벤트 이력 기록.

설계 원칙:
- 모든 변경은 즉시 커밋된다. 프로그램이 언제 죽어도 마지막 확정 상태가 남는다.
- 포지션 갱신과 이벤트 기록은 한 트랜잭션으로 묶인다 (둘 중 하나만 남는 일 없음).
- events 는 append-only. 수정·삭제하지 않으며 월간 통계의 원천이 된다.
- 복원 시 Position 생성자의 정합성 검증이 그대로 작동한다 —
  DB 가 손상됐다면 조용히 이상한 값으로 매매하는 대신 시작 시점에 실패한다.

주의: 종목코드는 반드시 TEXT ("005930"). INTEGER 로 다루면 앞자리 0 이 사라진다.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from trader.state_machine import Decision, Params, Position, State

_SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    trade_date TEXT NOT NULL,           -- 매매일 (YYYY-MM-DD). 날짜별 관심종목 리스트
    symbol   TEXT NOT NULL,             -- 종목코드 (예: '005930')
    name     TEXT NOT NULL DEFAULT '',  -- 종목명 (표시용)
    line1    REAL NOT NULL,
    line2    REAL NOT NULL,
    line3    REAL NOT NULL,
    buy1_amount REAL NOT NULL,
    buy2_amount REAL NOT NULL,
    tp_rate1  REAL NOT NULL, tp_rate2  REAL NOT NULL, tp_rate3  REAL NOT NULL,
    tp_ratio1 REAL NOT NULL, tp_ratio2 REAL NOT NULL, tp_ratio3 REAL NOT NULL,
    memo TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',      -- 종목 선정 근거 (쉼표 구분, 예: 'KOSPI상승장,테마주')
    base_date TEXT NOT NULL DEFAULT '', -- 기준봉 날짜 (YYYY-MM-DD) — 선정의 기준이 된 급등일
    updated_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, symbol)
);

CREATE TABLE IF NOT EXISTS positions (
    trade_date   TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    state        TEXT NOT NULL,
    avg_price    REAL NOT NULL,
    total_bought INTEGER NOT NULL,
    remaining    INTEGER NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    fees         REAL NOT NULL DEFAULT 0,   -- 누적 거래비용 (수수료 + 매도 거래세)
    high_price   REAL NOT NULL DEFAULT 0,   -- 보유 중 최고가 (MFE)
    low_price    REAL NOT NULL DEFAULT 0,   -- 보유 중 최저가 (MAE)
    day_low      REAL NOT NULL DEFAULT 0,   -- 감시 중 당일 최저가 (진입 전 포함, 근접도 분석)
    day_open     REAL NOT NULL DEFAULT 0,   -- 감시 중 첫 체결가 (당일 등락률 기준)
    day_close    REAL NOT NULL DEFAULT 0,   -- 감시 중 마지막 체결가
    pending      INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (trade_date, symbol),
    FOREIGN KEY (trade_date, symbol) REFERENCES symbols(trade_date, symbol) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS events (       -- append-only 이력
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    trade_date TEXT NOT NULL DEFAULT '',   -- 어느 매매일 리스트에서 발생했는지
    symbol     TEXT NOT NULL,
    kind       TEXT NOT NULL,              -- 등록 / 전이 / 리셋 / 삭제 / 에러 ...
    from_state TEXT,
    to_state   TEXT,
    side       TEXT,                       -- 매수 / 매도 / NULL(주문 없는 전이)
    qty        INTEGER,
    price      REAL,                       -- 실제 체결가
    trigger_price REAL,                    -- 판정을 유발한 체결가 (슬리피지 분석용)
    reason     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_symbol_ts ON events(symbol, ts);

-- 매매일지: 코멘트와 복기 차트 경로. 매매 데이터(손익·MFE/MAE·태그)는 이미
-- symbols·positions·events 에 있으므로 여기에는 **사람이 쓴 것과 파일 경로만** 둔다.
-- 글자 수 제한은 두지 않는다 (TEXT 는 사실상 무제한이고, 차트 PNG 한 장이 텍스트
-- 수십 건보다 크다).
CREATE TABLE IF NOT EXISTS journal (
    trade_date TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    good       TEXT NOT NULL DEFAULT '',   -- 잘한 점
    bad        TEXT NOT NULL DEFAULT '',   -- 아쉬운 점
    daily_path  TEXT NOT NULL DEFAULT '',  -- 보관된 일봉 차트 경로
    minute_path TEXT NOT NULL DEFAULT '',  -- 보관된 3분봉 차트 경로
    updated_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, symbol)
);

CREATE TABLE IF NOT EXISTS settings (   -- 전역 설정 (자금 배분, 투자 모드 등)
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT '접수',  -- 접수 / 체결 / 거부 / 취소
    fill_price      REAL,
    fill_qty        INTEGER,
    broker_order_no TEXT,
    updated_at      TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_SCHEMA_VERSION = 11  # 스키마 변경 시 1 증가.

# 버전별 자동 이관 (컬럼 추가처럼 기존 데이터를 보존할 수 있는 변경만 여기 등록한다).
# 여기 없는 버전 차이는 데이터 구조가 바뀐 것이므로 종전대로 명확한 에러로 안내한다.
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    8: ("ALTER TABLE events ADD COLUMN trigger_price REAL",),  # 슬리피지 기록용
    9: ("ALTER TABLE positions ADD COLUMN day_low REAL",),  # 1선 근접도 분석용
    10: (  # 매매일지: 종목 선정 근거(태그·기준봉)와 벤치마크(시가·종가)
        "ALTER TABLE symbols ADD COLUMN tags TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE symbols ADD COLUMN base_date TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE positions ADD COLUMN day_open REAL",
        "ALTER TABLE positions ADD COLUMN day_close REAL",
    ),
    11: (  # 매매일지 2단계: 코멘트와 차트 보관 경로
        """CREATE TABLE IF NOT EXISTS journal (
            trade_date TEXT NOT NULL,
            symbol     TEXT NOT NULL,
            good       TEXT NOT NULL DEFAULT '',
            bad        TEXT NOT NULL DEFAULT '',
            daily_path  TEXT NOT NULL DEFAULT '',
            minute_path TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, symbol)
        )""",
    ),
}


class Store:
    """SQLite 저장소. 매매 코어 스레드가 단독으로 소유한다."""

    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")  # 쓰기 도중 죽어도 DB 무결성 보장
        self._conn.execute("PRAGMA foreign_keys=ON")

        has_tables = (
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='symbols'"
            ).fetchone()
            is not None
        )
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if has_tables and version != _SCHEMA_VERSION:
            if not self._migrate(version):
                self._conn.close()
                raise RuntimeError(
                    f"DB 스키마 버전 불일치: 파일 v{version}, 프로그램 v{_SCHEMA_VERSION}. "
                    f"개발 단계에서는 '{path}' 파일을 삭제하고 다시 실행하세요."
                )

        self._conn.executescript(_SCHEMA)
        self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        self._conn.commit()

    def _migrate(self, version: int) -> bool:
        """구버전 DB 를 최신으로 올린다. 매매 이력을 지우지 않는 것이 목적이다.

        중간 버전이 하나라도 이관 목록에 없으면 False 를 돌려 호출부가 안내하게 한다
        (데이터 구조가 바뀐 변경을 억지로 이어붙이면 더 위험하다).
        """
        if not 0 < version < _SCHEMA_VERSION:
            return False
        steps = []
        for target in range(version + 1, _SCHEMA_VERSION + 1):
            if target not in _MIGRATIONS:
                return False
            steps.extend(_MIGRATIONS[target])
        for sql in steps:
            try:
                self._conn.execute(sql)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):  # 이미 적용된 경우는 정상
                    return False
        self._conn.commit()
        return True

    def close(self) -> None:
        self._conn.close()

    # ── 저녁 등록 워크플로우 ─────────────────────────────────────

    def register_symbol(
        self,
        trade_date: str,
        symbol: str,
        name: str,
        params: Params,
        position: Position = Position(),
        memo: str = "",
        tags: str = "",
        base_date: str = "",
    ) -> None:
        """관심종목 등록/갱신. 기존 설정과 포지션을 통째로 대체한다.

        신규 종목은 기본값(대기)으로, 오버나이트 보유분은 전일 마감 상태의
        Position 을 직접 넘겨 시작 상태를 지정한다. 기존 포지션을 덮어쓰는
        작업이므로 이전 상태를 이벤트에 남겨 감사 가능하게 한다.
        """
        prev = self._load_position(trade_date, symbol)
        with self._conn:
            self._conn.execute(
                """INSERT INTO symbols
                   (trade_date, symbol, name, line1, line2, line3, buy1_amount, buy2_amount,
                    tp_rate1, tp_rate2, tp_rate3, tp_ratio1, tp_ratio2, tp_ratio3,
                    memo, tags, base_date, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(trade_date, symbol) DO UPDATE SET
                    name=excluded.name, line1=excluded.line1, line2=excluded.line2,
                    line3=excluded.line3, buy1_amount=excluded.buy1_amount,
                    buy2_amount=excluded.buy2_amount,
                    tp_rate1=excluded.tp_rate1, tp_rate2=excluded.tp_rate2,
                    tp_rate3=excluded.tp_rate3, tp_ratio1=excluded.tp_ratio1,
                    tp_ratio2=excluded.tp_ratio2, tp_ratio3=excluded.tp_ratio3,
                    memo=excluded.memo, tags=excluded.tags,
                    base_date=excluded.base_date,
                    updated_at=excluded.updated_at""",
                (
                    trade_date,
                    symbol,
                    name,
                    params.line1,
                    params.line2,
                    params.line3,
                    params.buy1_amount,
                    params.buy2_amount,
                    *params.tp_rates,
                    *params.tp_ratios,
                    memo,
                    tags,
                    base_date,
                    _now(),
                ),
            )
            self._write_position(trade_date, symbol, position)
            self._insert_event(
                trade_date,
                symbol,
                kind="등록",
                from_state=prev.state.value if prev else None,
                to_state=position.state.value,
                reason=f"{name} 등록 (시작 상태: {position.state.value})",
            )

    def delete_symbol(self, trade_date: str, symbol: str) -> None:
        """관심종목 제외. 포지션은 CASCADE 로 함께 삭제, events 이력은 남는다."""
        with self._conn:
            self._conn.execute(
                "DELETE FROM symbols WHERE trade_date=? AND symbol=?",
                (trade_date, symbol),
            )
            # to_state 를 채워 '감사 행' 으로 표시한다 — recent_events 가 제외하므로
            # 화면 로그 줄과 중복되지 않는다 (실측: 삭제 1회에 로그 2줄)
            self._insert_event(
                trade_date, symbol, kind="삭제", reason="관심종목 제외", to_state="삭제"
            )

    # ── 복원 ────────────────────────────────────────────────────

    def load_all(
        self, trade_date: str
    ) -> dict[str, tuple[str, Params, Position, str, str, str]]:
        """해당 매매일의 전 종목 복원: {종목코드: (종목명, 설정, 포지션, 메모, 태그, 기준봉일)}.

        Position 생성자 검증을 통과하지 못하는 행이 있으면 즉시 실패한다.
        """
        result: dict[str, tuple[str, Params, Position, str, str, str]] = {}
        rows = self._conn.execute(
            """SELECT s.*, p.state, p.avg_price, p.total_bought, p.remaining,
                      p.realized_pnl, p.fees, p.high_price, p.low_price, p.day_low,
                      p.day_open, p.day_close, p.pending
               FROM symbols s JOIN positions p USING(trade_date, symbol)
               WHERE s.trade_date=?""",
            (trade_date,),
        ).fetchall()
        for r in rows:
            try:
                params = Params(
                    line1=r["line1"],
                    line2=r["line2"],
                    line3=r["line3"],
                    buy1_amount=r["buy1_amount"],
                    buy2_amount=r["buy2_amount"],
                    tp_rates=(r["tp_rate1"], r["tp_rate2"], r["tp_rate3"]),
                    tp_ratios=(r["tp_ratio1"], r["tp_ratio2"], r["tp_ratio3"]),
                )
                position = Position(
                    state=State(r["state"]),
                    avg_price=r["avg_price"],
                    total_bought=r["total_bought"],
                    remaining=r["remaining"],
                    pending=bool(r["pending"]),
                    realized_pnl=r["realized_pnl"],
                    fees=r["fees"],
                    high_price=r["high_price"],
                    low_price=r["low_price"],
                    day_low=r["day_low"] or 0.0,
                    day_open=r["day_open"] or 0.0,
                    day_close=r["day_close"] or 0.0,
                )
            except ValueError as e:
                raise ValueError(
                    f"복원 실패 — 종목 {r['symbol']} 데이터 이상: {e}"
                ) from e
            result[r["symbol"]] = (
                r["name"],
                params,
                position,
                r["memo"],
                r["tags"] or "",
                r["base_date"] or "",
            )
        return result

    # ── 상태 변경 기록 ──────────────────────────────────────────

    def list_dates(self) -> list[str]:
        """관심종목이 등록된 매매일 목록 (최신순). 날짜 선택 UI 용."""
        rows = self._conn.execute(
            "SELECT DISTINCT trade_date FROM symbols ORDER BY trade_date DESC"
        ).fetchall()
        return [r["trade_date"] for r in rows]

    # ── 상태 변경 기록 ──────────────────────────────────────────

    def save_transition(
        self,
        trade_date: str,
        symbol: str,
        from_state: State,
        position: Position,
        decision: Decision,
        price: float | None,
        trigger_price: float | None = None,
    ) -> None:
        """전이 확정 직후 호출. 포지션 갱신 + 이벤트 기록을 한 트랜잭션으로.

        trigger_price 는 판정을 유발한 체결가다. 실제 체결가(price)와의 차이가
        시장가 주문의 슬리피지이며, 나중에 집계해 주문 방식을 재검토할 근거가 된다.
        """
        with self._conn:
            self._write_position(trade_date, symbol, position)
            self._insert_event(
                trade_date,
                symbol,
                kind="전이",
                from_state=from_state.value,
                to_state=decision.to_state.value,
                side=decision.side.value if decision.side else None,
                qty=decision.qty or None,
                price=price,
                reason=decision.reason,
                trigger_price=trigger_price,
            )

    def save_position(self, trade_date: str, symbol: str, position: Position) -> None:
        """전이 없는 포지션 갱신 (예: 주문 전송 직후 pending 표시)."""
        with self._conn:
            self._write_position(trade_date, symbol, position)

    def admin_reset(self, trade_date: str, symbol: str, position: Position) -> Position:
        """관리자 개입: 종료 → 대기. 규칙 검증은 state_machine.reset 이 담당."""
        from trader.state_machine import reset  # 순환 아님: 규칙의 단일 출처 유지

        new_pos = reset(position)
        with self._conn:
            self._write_position(trade_date, symbol, new_pos)
            self._insert_event(
                trade_date,
                symbol,
                kind="리셋",
                from_state=position.state.value,
                to_state=new_pos.state.value,
                reason="관리자 수동 초기화 (종료 → 대기)",
            )
        return new_pos

    def log(self, trade_date: str, symbol: str, kind: str, reason: str) -> None:
        """전이 외 일반 이벤트 기록 (에러, 재연결, 잔고 불일치 경고 등)."""
        with self._conn:
            self._insert_event(trade_date, symbol, kind=kind, reason=reason)

    def daily_report(self, trade_date: str) -> tuple[list[dict], list[dict]]:
        """일일 요약용 원자료 — (종목 스냅샷 목록, 체결 목록).

        체결은 `events` 의 전이 행 중 주문이 동반된 것(side 있음)만 모은다.
        """
        symbol_rows = [
            dict(r)
            for r in self._conn.execute(
                """SELECT s.symbol, s.name, s.memo, s.line1, s.tags, s.base_date,
                      p.state, p.avg_price, p.total_bought, p.remaining,
                      p.realized_pnl, p.fees, p.high_price, p.low_price,
                      p.day_low, p.day_open, p.day_close
               FROM symbols s JOIN positions p USING(trade_date, symbol)
               WHERE s.trade_date=? ORDER BY s.symbol""",
                (trade_date,),
            ).fetchall()
        ]
        fills = [
            dict(r)
            for r in self._conn.execute(
                """SELECT ts, symbol, side, qty, price, trigger_price, reason FROM events
               WHERE trade_date=? AND kind='전이' AND side IS NOT NULL
               ORDER BY id""",
                (trade_date,),
            ).fetchall()
        ]
        return symbol_rows, fills

    # ── 매매일지 ────────────────────────────────────────────────

    def save_journal(
        self,
        trade_date: str,
        symbol: str,
        good: str = "",
        bad: str = "",
        daily_path: str = "",
        minute_path: str = "",
    ) -> None:
        """일지 저장. 빈 문자열로 넘긴 항목은 기존 값을 지우지 않는다.

        차트 경로는 종료 시 자동으로, 코멘트는 나중에 사람이 쓰므로 서로 다른 시점에
        같은 행을 갱신하게 된다.
        """
        with self._conn:
            self._conn.execute(
                """INSERT INTO journal (trade_date, symbol, good, bad,
                                        daily_path, minute_path, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(trade_date, symbol) DO UPDATE SET
                     good=CASE WHEN excluded.good='' THEN journal.good ELSE excluded.good END,
                     bad=CASE WHEN excluded.bad='' THEN journal.bad ELSE excluded.bad END,
                     daily_path=CASE WHEN excluded.daily_path='' THEN journal.daily_path
                                     ELSE excluded.daily_path END,
                     minute_path=CASE WHEN excluded.minute_path='' THEN journal.minute_path
                                      ELSE excluded.minute_path END,
                     updated_at=excluded.updated_at""",
                (trade_date, symbol, good, bad, daily_path, minute_path, _now()),
            )

    def load_journal(self, trade_date: str, symbol: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM journal WHERE trade_date=? AND symbol=?",
            (trade_date, symbol),
        ).fetchone()
        return dict(row) if row else {}

    def journal_entries(self, since: str = "", until: str = "") -> list[dict]:
        """일지 대상 목록 — 매매가 있었던 종목과 그 코멘트 작성 여부.

        '무엇을 아직 안 썼는지' 를 알려주는 것이 목적이라 매매 요약도 함께 붙인다.
        """
        where = ["p.total_bought > 0"]
        params: list[str] = []
        if since:
            where.append("s.trade_date >= ?")
            params.append(since)
        if until:
            where.append("s.trade_date <= ?")
            params.append(until)
        rows = self._conn.execute(
            f"""SELECT s.trade_date, s.symbol, s.name, s.tags, s.base_date, s.memo,
                       p.state, p.avg_price, p.total_bought, p.realized_pnl, p.fees,
                       p.high_price, p.low_price, p.day_open, p.day_close,
                       COALESCE(j.good, '') good, COALESCE(j.bad, '') bad,
                       COALESCE(j.daily_path, '') daily_path,
                       COALESCE(j.minute_path, '') minute_path
                FROM symbols s JOIN positions p USING(trade_date, symbol)
                LEFT JOIN journal j ON j.trade_date = s.trade_date AND j.symbol = s.symbol
                WHERE {' AND '.join(where)}
                ORDER BY s.trade_date DESC, s.symbol""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_trade_dates(self, limit: int = 10) -> list[str]:
        """기록이 있는 매매일 (최근 순)."""
        rows = self._conn.execute(
            "SELECT DISTINCT trade_date FROM symbols ORDER BY trade_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r["trade_date"] for r in rows]

    def slippage_report(self, since: str = "", until: str = "") -> list[dict]:
        """시장가 체결 오차 집계용 원자료 — 판정가·체결가가 모두 있는 체결만.

        (매매일, 종목, 구분, 수량, 판정가, 체결가, 오차율) 목록. 기간을 비우면 전체.
        나중에 "평균 오차 -0.4%, 최악 -1.9%, 손실 총액 X원" 같은 집계에 쓴다.
        """
        where = ["kind='전이'", "side IS NOT NULL", "price > 0", "trigger_price > 0"]
        params: list[str] = []
        if since:
            where.append("trade_date >= ?")
            params.append(since)
        if until:
            where.append("trade_date <= ?")
            params.append(until)
        rows = self._conn.execute(
            f"""SELECT trade_date, ts, symbol, side, qty, trigger_price, price, reason
                FROM events WHERE {' AND '.join(where)} ORDER BY id""",
            params,
        ).fetchall()
        out = []
        for r in rows:
            gap = (r["price"] - r["trigger_price"]) / r["trigger_price"]
            # 매수는 비싸게 사면 손해, 매도는 싸게 팔면 손해 — 부호를 손익 기준으로 통일
            cost = -gap if r["side"] == "매수" else gap
            out.append(
                {
                    **dict(r),
                    "gap": gap,
                    "cost_rate": cost,
                    "cost_amount": cost * (r["price"] * (r["qty"] or 0)),
                }
            )
        return out

    def recent_events(
        self, trade_date: str, limit: int = 500
    ) -> list[tuple[str, str, str, str]]:
        """해당 매매일의 일반 로그 (ts, symbol, kind, reason) — 오래된 순.

        재시작·매매일 전환 시 로그 화면 복원용. 상태 정보를 담은 감사 행(등록·전이 등,
        from_state/to_state 중 하나라도 있음)은 제외한다 — 같은 사건을 화면 로그 줄이
        이미 남기고 있어, 포함하면 복원 시 같은 줄이 두 번 보인다.
        """
        rows = self._conn.execute(
            "SELECT ts, symbol, kind, reason FROM events "
            "WHERE trade_date = ? AND from_state IS NULL AND to_state IS NULL "
            "ORDER BY rowid DESC LIMIT ?",
            (trade_date, limit),
        ).fetchall()
        return [(r["ts"], r["symbol"], r["kind"], r["reason"]) for r in reversed(rows)]

    # ── 주문 기록 (broker 연동 시 사용) ─────────────────────────

    def record_order(self, symbol: str, side: str, qty: int) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO orders (ts, symbol, side, qty, updated_at) VALUES (?,?,?,?,?)",
                (_now(), symbol, side, qty, _now()),
            )
            return cur.lastrowid

    def update_order(
        self,
        order_id: int,
        status: str,
        fill_price: float | None = None,
        fill_qty: int | None = None,
        broker_order_no: str | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """UPDATE orders SET status=?, fill_price=?, fill_qty=?,
                   broker_order_no=COALESCE(?, broker_order_no), updated_at=?
                   WHERE id=?""",
                (status, fill_price, fill_qty, broker_order_no, _now(), order_id),
            )

    # ── 전역 설정 ───────────────────────────────────────────────

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ── 조회 (통계·UI용) ────────────────────────────────────────

    def fetch_events(
        self, symbol: str | None = None, since: str | None = None
    ) -> list[sqlite3.Row]:
        """이벤트 이력 조회. since 는 ISO 문자열 (예: '2026-07-01')."""
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if symbol:
            sql += " AND symbol=?"
            args.append(symbol)
        if since:
            sql += " AND ts>=?"
            args.append(since)
        return self._conn.execute(sql + " ORDER BY id", args).fetchall()

    # ── 내부 헬퍼 ───────────────────────────────────────────────

    def _load_position(self, trade_date: str, symbol: str) -> Position | None:
        r = self._conn.execute(
            "SELECT state, avg_price, total_bought, remaining, realized_pnl, pending "
            "FROM positions WHERE trade_date=? AND symbol=?",
            (trade_date, symbol),
        ).fetchone()
        if r is None:
            return None
        return Position(
            state=State(r["state"]),
            avg_price=r["avg_price"],
            total_bought=r["total_bought"],
            remaining=r["remaining"],
            pending=bool(r["pending"]),
            realized_pnl=r["realized_pnl"],
        )

    def _write_position(self, trade_date: str, symbol: str, pos: Position) -> None:
        self._conn.execute(
            """INSERT INTO positions
               (trade_date, symbol, state, avg_price, total_bought, remaining,
                realized_pnl, fees, high_price, low_price, day_low, day_open, day_close,
                pending, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(trade_date, symbol) DO UPDATE SET
                state=excluded.state, avg_price=excluded.avg_price,
                total_bought=excluded.total_bought, remaining=excluded.remaining,
                realized_pnl=excluded.realized_pnl, fees=excluded.fees,
                high_price=excluded.high_price, low_price=excluded.low_price,
                day_low=excluded.day_low, day_open=excluded.day_open,
                day_close=excluded.day_close,
                pending=excluded.pending, updated_at=excluded.updated_at""",
            (
                trade_date,
                symbol,
                pos.state.value,
                pos.avg_price,
                pos.total_bought,
                pos.remaining,
                pos.realized_pnl,
                pos.fees,
                pos.high_price,
                pos.low_price,
                pos.day_low,
                pos.day_open,
                pos.day_close,
                int(pos.pending),
                _now(),
            ),
        )

    def _insert_event(
        self,
        trade_date: str,
        symbol: str,
        kind: str,
        from_state: str | None = None,
        to_state: str | None = None,
        side: str | None = None,
        qty: int | None = None,
        price: float | None = None,
        reason: str = "",
        trigger_price: float | None = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO events
               (ts, trade_date, symbol, kind, from_state, to_state, side, qty, price,
                trigger_price, reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _now(),
                trade_date,
                symbol,
                kind,
                from_state,
                to_state,
                side,
                qty,
                price,
                trigger_price,
                reason,
            ),
        )
