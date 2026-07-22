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
    price      REAL,
    reason     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_symbol_ts ON events(symbol, ts);

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


_SCHEMA_VERSION = 5  # 스키마 변경 시 1 증가. 구버전 DB 파일은 명확한 에러로 안내한다.


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
            self._conn.close()
            raise RuntimeError(
                f"DB 스키마 버전 불일치: 파일 v{version}, 프로그램 v{_SCHEMA_VERSION}. "
                f"개발 단계에서는 '{path}' 파일을 삭제하고 다시 실행하세요."
            )

        self._conn.executescript(_SCHEMA)
        self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        self._conn.commit()

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
                    updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(trade_date, symbol) DO UPDATE SET
                    name=excluded.name, line1=excluded.line1, line2=excluded.line2,
                    line3=excluded.line3, buy1_amount=excluded.buy1_amount,
                    buy2_amount=excluded.buy2_amount,
                    tp_rate1=excluded.tp_rate1, tp_rate2=excluded.tp_rate2,
                    tp_rate3=excluded.tp_rate3, tp_ratio1=excluded.tp_ratio1,
                    tp_ratio2=excluded.tp_ratio2, tp_ratio3=excluded.tp_ratio3,
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
            self._insert_event(trade_date, symbol, kind="삭제", reason="관심종목 제외")

    # ── 복원 ────────────────────────────────────────────────────

    def load_all(self, trade_date: str) -> dict[str, tuple[str, Params, Position]]:
        """해당 매매일의 전 종목 복원: {종목코드: (종목명, 설정, 포지션)}.

        Position 생성자 검증을 통과하지 못하는 행이 있으면 즉시 실패한다.
        """
        result: dict[str, tuple[str, Params, Position]] = {}
        rows = self._conn.execute(
            """SELECT s.*, p.state, p.avg_price, p.total_bought, p.remaining,
                      p.realized_pnl, p.pending
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
                )
            except ValueError as e:
                raise ValueError(
                    f"복원 실패 — 종목 {r['symbol']} 데이터 이상: {e}"
                ) from e
            result[r["symbol"]] = (r["name"], params, position)
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
    ) -> None:
        """전이 확정 직후 호출. 포지션 갱신 + 이벤트 기록을 한 트랜잭션으로."""
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

    def recent_events(
        self, trade_date: str, limit: int = 500
    ) -> list[tuple[str, str, str, str]]:
        """해당 매매일의 일반 로그 (ts, symbol, kind, reason) — 오래된 순.

        재시작·매매일 전환 시 로그 화면 복원용. 전이 상세 행(from_state 있음)은
        실시간 로그와 목록을 일치시키기 위해 제외한다 (전이는 별도 로그 줄로 이미 기록됨).
        """
        rows = self._conn.execute(
            "SELECT ts, symbol, kind, reason FROM events "
            "WHERE trade_date = ? AND from_state IS NULL "
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
                realized_pnl, pending, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(trade_date, symbol) DO UPDATE SET
                state=excluded.state, avg_price=excluded.avg_price,
                total_bought=excluded.total_bought, remaining=excluded.remaining,
                realized_pnl=excluded.realized_pnl,
                pending=excluded.pending, updated_at=excluded.updated_at""",
            (
                trade_date,
                symbol,
                pos.state.value,
                pos.avg_price,
                pos.total_bought,
                pos.remaining,
                pos.realized_pnl,
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
    ) -> None:
        self._conn.execute(
            """INSERT INTO events
               (ts, trade_date, symbol, kind, from_state, to_state, side, qty, price, reason)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
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
                reason,
            ),
        )
