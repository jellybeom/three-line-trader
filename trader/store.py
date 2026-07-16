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
    symbol   TEXT PRIMARY KEY,          -- 종목코드 (예: '005930')
    name     TEXT NOT NULL DEFAULT '',  -- 종목명 (표시용)
    line1    REAL NOT NULL,
    line2    REAL NOT NULL,
    line3    REAL NOT NULL,
    buy1_qty INTEGER NOT NULL,
    buy2_qty INTEGER NOT NULL,
    tp_rate1  REAL NOT NULL, tp_rate2  REAL NOT NULL, tp_rate3  REAL NOT NULL,
    tp_ratio1 REAL NOT NULL, tp_ratio2 REAL NOT NULL, tp_ratio3 REAL NOT NULL,
    breakeven_buffer REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    symbol       TEXT PRIMARY KEY REFERENCES symbols(symbol) ON DELETE CASCADE,
    state        TEXT NOT NULL,
    avg_price    REAL NOT NULL,
    total_bought INTEGER NOT NULL,
    remaining    INTEGER NOT NULL,
    pending      INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (       -- append-only 이력
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
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
    return datetime.now().isoformat(timespec="seconds")


class Store:
    """SQLite 저장소. 매매 코어 스레드가 단독으로 소유한다."""

    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")  # 쓰기 도중 죽어도 DB 무결성 보장
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── 저녁 등록 워크플로우 ─────────────────────────────────────

    def register_symbol(
        self, symbol: str, name: str, params: Params, position: Position = Position()
    ) -> None:
        """관심종목 등록/갱신. 기존 설정과 포지션을 통째로 대체한다.

        신규 종목은 기본값(대기)으로, 오버나이트 보유분은 전일 마감 상태의
        Position 을 직접 넘겨 시작 상태를 지정한다. 기존 포지션을 덮어쓰는
        작업이므로 이전 상태를 이벤트에 남겨 감사 가능하게 한다.
        """
        prev = self._load_position(symbol)
        with self._conn:
            self._conn.execute(
                """INSERT INTO symbols
                   (symbol, name, line1, line2, line3, buy1_qty, buy2_qty,
                    tp_rate1, tp_rate2, tp_rate3, tp_ratio1, tp_ratio2, tp_ratio3,
                    breakeven_buffer, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol) DO UPDATE SET
                    name=excluded.name, line1=excluded.line1, line2=excluded.line2,
                    line3=excluded.line3, buy1_qty=excluded.buy1_qty,
                    buy2_qty=excluded.buy2_qty,
                    tp_rate1=excluded.tp_rate1, tp_rate2=excluded.tp_rate2,
                    tp_rate3=excluded.tp_rate3, tp_ratio1=excluded.tp_ratio1,
                    tp_ratio2=excluded.tp_ratio2, tp_ratio3=excluded.tp_ratio3,
                    breakeven_buffer=excluded.breakeven_buffer,
                    updated_at=excluded.updated_at""",
                (
                    symbol,
                    name,
                    params.line1,
                    params.line2,
                    params.line3,
                    params.buy1_qty,
                    params.buy2_qty,
                    *params.tp_rates,
                    *params.tp_ratios,
                    params.breakeven_buffer,
                    _now(),
                ),
            )
            self._write_position(symbol, position)
            self._insert_event(
                symbol,
                kind="등록",
                from_state=prev.state.value if prev else None,
                to_state=position.state.value,
                reason=f"{name} 등록 (시작 상태: {position.state.value})",
            )

    def delete_symbol(self, symbol: str) -> None:
        """관심종목 제외. 포지션은 CASCADE 로 함께 삭제, events 이력은 남는다."""
        with self._conn:
            self._conn.execute("DELETE FROM symbols WHERE symbol=?", (symbol,))
            self._insert_event(symbol, kind="삭제", reason="관심종목 제외")

    # ── 복원 ────────────────────────────────────────────────────

    def load_all(self) -> dict[str, tuple[str, Params, Position]]:
        """시작 시 전 종목 복원: {종목코드: (종목명, 설정, 포지션)}.

        Position 생성자 검증을 통과하지 못하는 행이 있으면 즉시 실패한다.
        """
        result: dict[str, tuple[str, Params, Position]] = {}
        rows = self._conn.execute(
            """SELECT s.*, p.state, p.avg_price, p.total_bought, p.remaining, p.pending
               FROM symbols s JOIN positions p USING(symbol)"""
        ).fetchall()
        for r in rows:
            try:
                params = Params(
                    line1=r["line1"],
                    line2=r["line2"],
                    line3=r["line3"],
                    buy1_qty=r["buy1_qty"],
                    buy2_qty=r["buy2_qty"],
                    tp_rates=(r["tp_rate1"], r["tp_rate2"], r["tp_rate3"]),
                    tp_ratios=(r["tp_ratio1"], r["tp_ratio2"], r["tp_ratio3"]),
                    breakeven_buffer=r["breakeven_buffer"],
                )
                position = Position(
                    state=State(r["state"]),
                    avg_price=r["avg_price"],
                    total_bought=r["total_bought"],
                    remaining=r["remaining"],
                    pending=bool(r["pending"]),
                )
            except ValueError as e:
                raise ValueError(
                    f"복원 실패 — 종목 {r['symbol']} 데이터 이상: {e}"
                ) from e
            result[r["symbol"]] = (r["name"], params, position)
        return result

    # ── 상태 변경 기록 ──────────────────────────────────────────

    def save_transition(
        self,
        symbol: str,
        from_state: State,
        position: Position,
        decision: Decision,
        price: float | None,
    ) -> None:
        """전이 확정 직후 호출. 포지션 갱신 + 이벤트 기록을 한 트랜잭션으로."""
        with self._conn:
            self._write_position(symbol, position)
            self._insert_event(
                symbol,
                kind="전이",
                from_state=from_state.value,
                to_state=decision.to_state.value,
                side=decision.side.value if decision.side else None,
                qty=decision.qty or None,
                price=price,
                reason=decision.reason,
            )

    def save_position(self, symbol: str, position: Position) -> None:
        """전이 없는 포지션 갱신 (예: 주문 전송 직후 pending 표시)."""
        with self._conn:
            self._write_position(symbol, position)

    def admin_reset(self, symbol: str, position: Position) -> Position:
        """관리자 개입: 종료 → 대기. 규칙 검증은 state_machine.reset 이 담당."""
        from trader.state_machine import reset  # 순환 아님: 규칙의 단일 출처 유지

        new_pos = reset(position)
        with self._conn:
            self._write_position(symbol, new_pos)
            self._insert_event(
                symbol,
                kind="리셋",
                from_state=position.state.value,
                to_state=new_pos.state.value,
                reason="관리자 수동 초기화 (종료 → 대기)",
            )
        return new_pos

    def log(self, symbol: str, kind: str, reason: str) -> None:
        """전이 외 일반 이벤트 기록 (에러, 재연결, 잔고 불일치 경고 등)."""
        with self._conn:
            self._insert_event(symbol, kind=kind, reason=reason)

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

    def _load_position(self, symbol: str) -> Position | None:
        r = self._conn.execute(
            "SELECT state, avg_price, total_bought, remaining, pending "
            "FROM positions WHERE symbol=?",
            (symbol,),
        ).fetchone()
        if r is None:
            return None
        return Position(
            state=State(r["state"]),
            avg_price=r["avg_price"],
            total_bought=r["total_bought"],
            remaining=r["remaining"],
            pending=bool(r["pending"]),
        )

    def _write_position(self, symbol: str, pos: Position) -> None:
        self._conn.execute(
            """INSERT INTO positions
               (symbol, state, avg_price, total_bought, remaining, pending, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET
                state=excluded.state, avg_price=excluded.avg_price,
                total_bought=excluded.total_bought, remaining=excluded.remaining,
                pending=excluded.pending, updated_at=excluded.updated_at""",
            (
                symbol,
                pos.state.value,
                pos.avg_price,
                pos.total_bought,
                pos.remaining,
                int(pos.pending),
                _now(),
            ),
        )

    def _insert_event(
        self,
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
            """INSERT INTO events (ts, symbol, kind, from_state, to_state, side, qty, price, reason)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (_now(), symbol, kind, from_state, to_state, side, qty, price, reason),
        )
