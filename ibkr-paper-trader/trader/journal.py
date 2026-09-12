"""The journal: every decision, order and NAV point, in one SQLite file.

This is the point of the exercise. Without a complete record of what was
decided, on what data, under which rule, and what it cost, six months of
paper trading teaches you nothing.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    trade_date    TEXT NOT NULL,
    kind          TEXT NOT NULL,           -- 'trade' | 'strategy' | 'check'
    model         TEXT,
    nav           REAL,
    cash          REAL,
    halted        INTEGER DEFAULT 0,
    halt_reason   TEXT DEFAULT '',
    notes         TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS snapshots (
    run_id        INTEGER NOT NULL REFERENCES runs(id),
    symbol        TEXT NOT NULL,
    last          REAL,
    metrics_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    run_id        INTEGER NOT NULL REFERENCES runs(id),
    assessment    TEXT,
    no_trade_reason TEXT,
    raw_json      TEXT NOT NULL,
    input_tokens  INTEGER,
    output_tokens INTEGER
);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id),
    ts            TEXT NOT NULL,
    trade_date    TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    action        TEXT NOT NULL,
    quantity      INTEGER NOT NULL,
    limit_price   REAL,
    notional      REAL,
    rule_id       TEXT,
    rationale     TEXT,
    outcome       TEXT NOT NULL,           -- 'rejected' | 'placed' | 'error' | 'dry_run'
    reason        TEXT DEFAULT '',
    ib_order_id   INTEGER,
    ib_status     TEXT,
    filled        REAL DEFAULT 0,
    avg_fill_price REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS equity (
    trade_date       TEXT PRIMARY KEY,
    ts               TEXT NOT NULL,
    nav              REAL NOT NULL,
    cash             REAL NOT NULL,
    positions_value  REAL NOT NULL,
    benchmark_price  REAL
);

CREATE TABLE IF NOT EXISTS strategy_versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER REFERENCES runs(id),
    ts            TEXT NOT NULL,
    rule_ids      TEXT NOT NULL,
    markdown      TEXT NOT NULL,
    raw_json      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ladder_state (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    fired              TEXT NOT NULL DEFAULT '',
    reserve_base       REAL NOT NULL DEFAULT 0,
    carried_notional   REAL NOT NULL DEFAULT 0,
    cycle_low_drawdown REAL NOT NULL DEFAULT 0,
    updated_ts         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ladder_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER REFERENCES runs(id),
    ts            TEXT NOT NULL,
    trade_date    TEXT NOT NULL,
    drawdown_pct  REAL NOT NULL,
    rungs         TEXT NOT NULL DEFAULT '',
    notional      REAL NOT NULL DEFAULT 0,
    outcome       TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(trade_date);
CREATE INDEX IF NOT EXISTS idx_runs_date ON runs(trade_date);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class LadderStateRow:
    fired: set[int]
    reserve_base: float
    carried_notional: float
    cycle_low_drawdown: float


class Journal:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        with closing(self.conn.cursor()) as cur:
            cur.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writes ------------------------------------------------------------
    def start_run(
        self,
        kind: str,
        model: str = "",
        nav: float = 0.0,
        cash: float = 0.0,
        trade_day: date | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (ts, trade_date, kind, model, nav, cash) VALUES (?,?,?,?,?,?)",
            (_now(), (trade_day or date.today()).isoformat(), kind, model, nav, cash),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, halted: bool = False, halt_reason: str = "", notes: str = "") -> None:
        self.conn.execute(
            "UPDATE runs SET halted=?, halt_reason=?, notes=? WHERE id=?",
            (1 if halted else 0, halt_reason, notes, run_id),
        )
        self.conn.commit()

    def record_snapshots(self, run_id: int, snapshots: dict[str, Any]) -> None:
        rows = [
            (run_id, sym, snap.last, json.dumps(snap.to_row()))
            for sym, snap in snapshots.items()
        ]
        self.conn.executemany(
            "INSERT INTO snapshots (run_id, symbol, last, metrics_json) VALUES (?,?,?,?)", rows
        )
        self.conn.commit()

    def record_decision(
        self,
        run_id: int,
        assessment: str,
        no_trade_reason: str,
        raw: dict[str, Any],
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        self.conn.execute(
            "INSERT INTO decisions (run_id, assessment, no_trade_reason, raw_json, "
            "input_tokens, output_tokens) VALUES (?,?,?,?,?,?)",
            (run_id, assessment, no_trade_reason, json.dumps(raw), input_tokens, output_tokens),
        )
        self.conn.commit()

    def record_order(
        self,
        run_id: int,
        symbol: str,
        action: str,
        quantity: int,
        limit_price: float,
        notional: float,
        rule_id: str,
        rationale: str,
        outcome: str,
        reason: str = "",
        ib_order_id: int | None = None,
        ib_status: str = "",
        filled: float = 0.0,
        avg_fill_price: float = 0.0,
        trade_day: date | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO orders (run_id, ts, trade_date, symbol, action, quantity, limit_price, "
            "notional, rule_id, rationale, outcome, reason, ib_order_id, ib_status, filled, "
            "avg_fill_price) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, _now(), (trade_day or date.today()).isoformat(), symbol, action,
                quantity, limit_price, notional, rule_id, rationale, outcome, reason,
                ib_order_id, ib_status, filled, avg_fill_price,
            ),
        )
        self.conn.commit()

    def record_equity(
        self,
        nav: float,
        cash: float,
        positions_value: float,
        benchmark_price: float | None,
        trade_day: date | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO equity (trade_date, ts, nav, cash, positions_value, benchmark_price) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(trade_date) DO UPDATE SET "
            "ts=excluded.ts, nav=excluded.nav, cash=excluded.cash, "
            "positions_value=excluded.positions_value, benchmark_price=excluded.benchmark_price",
            (
                (trade_day or date.today()).isoformat(), _now(), nav, cash,
                positions_value, benchmark_price,
            ),
        )
        self.conn.commit()

    def record_strategy(self, run_id: int, rule_ids: list[str], markdown: str, raw: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO strategy_versions (run_id, ts, rule_ids, markdown, raw_json) "
            "VALUES (?,?,?,?,?)",
            (run_id, _now(), ",".join(rule_ids), markdown, json.dumps(raw)),
        )
        self.conn.commit()

    # -- reads -------------------------------------------------------------
    def peak_nav(self) -> float:
        row = self.conn.execute("SELECT MAX(nav) AS peak FROM equity").fetchone()
        return float(row["peak"] or 0.0)

    def orders_today(self, trade_day: date | None = None) -> tuple[int, float]:
        day = (trade_day or date.today()).isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(notional),0) AS turnover FROM orders "
            "WHERE trade_date=? AND outcome='placed'",
            (day,),
        ).fetchone()
        return int(row["n"]), float(row["turnover"])

    def equity_curve(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute("SELECT * FROM equity ORDER BY trade_date ASC").fetchall()
        )

    def recent_orders(self, limit: int = 40) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        )

    def recent_decisions(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT r.trade_date, d.assessment, d.no_trade_reason FROM decisions d "
                "JOIN runs r ON r.id = d.run_id ORDER BY d.rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        )

    # -- crash ladder ------------------------------------------------------
    def load_ladder_state(self) -> "LadderStateRow":
        row = self.conn.execute("SELECT * FROM ladder_state WHERE id=1").fetchone()
        if row is None:
            return LadderStateRow(set(), 0.0, 0.0, 0.0)
        fired = {int(x) for x in (row["fired"] or "").split(",") if x.strip()}
        return LadderStateRow(
            fired,
            float(row["reserve_base"]),
            float(row["carried_notional"]),
            float(row["cycle_low_drawdown"]),
        )

    def save_ladder_state(
        self,
        fired: set[int],
        reserve_base: float,
        carried_notional: float,
        cycle_low_drawdown: float,
    ) -> None:
        self.conn.execute(
            "INSERT INTO ladder_state (id, fired, reserve_base, carried_notional, "
            "cycle_low_drawdown, updated_ts) VALUES (1,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET fired=excluded.fired, "
            "reserve_base=excluded.reserve_base, carried_notional=excluded.carried_notional, "
            "cycle_low_drawdown=excluded.cycle_low_drawdown, updated_ts=excluded.updated_ts",
            (
                ",".join(str(i) for i in sorted(fired)),
                reserve_base,
                carried_notional,
                cycle_low_drawdown,
                _now(),
            ),
        )
        self.conn.commit()

    def record_ladder_event(
        self,
        run_id: int | None,
        drawdown_pct: float,
        rungs: list[int],
        notional: float,
        outcome: str,
        note: str = "",
        trade_day: date | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO ladder_events (run_id, ts, trade_date, drawdown_pct, rungs, "
            "notional, outcome, note) VALUES (?,?,?,?,?,?,?,?)",
            (
                run_id, _now(), (trade_day or date.today()).isoformat(), drawdown_pct,
                ",".join(str(r) for r in rungs), notional, outcome, note,
            ),
        )
        self.conn.commit()

    def ladder_events(self, limit: int = 25) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM ladder_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        )

    def latest_strategy_rule_ids(self) -> set[str]:
        row = self.conn.execute(
            "SELECT rule_ids FROM strategy_versions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row or not row["rule_ids"]:
            return set()
        return {r for r in row["rule_ids"].split(",") if r}
