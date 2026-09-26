from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .settings import SCANNER_DB_PATH


HORIZONS = (21, 63, 126, 252)


def _connect() -> sqlite3.Connection:
    SCANNER_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(SCANNER_DB_PATH, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize() -> None:
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS backtest_runs (
                run_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                universe TEXT NOT NULL,
                params_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                event_count INTEGER NOT NULL,
                source_marker TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS backtest_events (
                event_id INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
                cohort TEXT NOT NULL,
                market_date TEXT NOT NULL,
                execution_date TEXT NOT NULL,
                security_id INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                name TEXT NOT NULL,
                distance_pct REAL NOT NULL,
                high_52w REAL NOT NULL,
                signal_close REAL NOT NULL,
                entry_open REAL NOT NULL,
                ret_21_gross REAL,
                ret_21_net REAL,
                ret_63_gross REAL,
                ret_63_net REAL,
                ret_126_gross REAL,
                ret_126_net REAL,
                ret_252_gross REAL,
                ret_252_net REAL
            );
            CREATE INDEX IF NOT EXISTS idx_events_run_date
                ON backtest_events(run_id, market_date, ticker);
            CREATE TABLE IF NOT EXISTS optimization_runs (
                optimization_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                params_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                source_marker TEXT NOT NULL
            );
            """
        )


def save_run(params: dict[str, Any], result: dict[str, Any], events: list[dict[str, Any]], source_marker: str) -> str:
    run_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    horizons = result["horizons"]
    with _connect() as connection:
        connection.execute(
            "INSERT INTO backtest_runs "
            "(run_id, created_at, universe, params_json, result_json, event_count, source_marker) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                created_at,
                str(params["universe"]),
                json.dumps(params, sort_keys=True),
                json.dumps(result, sort_keys=True),
                len(events),
                source_marker,
            ),
        )
        columns = [
            "run_id", "cohort", "market_date", "execution_date", "security_id", "ticker", "name",
            "distance_pct", "high_52w", "signal_close", "entry_open",
        ]
        for horizon in HORIZONS:
            columns.extend((f"ret_{horizon}_gross", f"ret_{horizon}_net"))
        placeholders = ",".join("?" for _ in columns)
        sql = f"INSERT INTO backtest_events ({','.join(columns)}) VALUES ({placeholders})"
        batch: list[tuple[Any, ...]] = []
        for event in events:
            values: list[Any] = [
                run_id,
                event["cohort"],
                event["market_date"],
                event["execution_date"],
                event["security_id"],
                event["ticker"],
                event["name"],
                event["distance_pct"],
                event["high_52w"],
                event["signal_close"],
                event["entry_open"],
            ]
            for horizon in HORIZONS:
                outcome = event["returns"].get(str(horizon))
                values.extend(
                    (outcome.get("gross") if outcome else None, outcome.get("net") if outcome else None)
                )
            batch.append(tuple(values))
            if len(batch) >= 2000:
                connection.executemany(sql, batch)
                batch.clear()
        if batch:
            connection.executemany(sql, batch)
    return run_id


def list_runs(limit: int = 30) -> list[dict[str, Any]]:
    if not SCANNER_DB_PATH.exists():
        return []
    with _connect() as connection:
        rows = connection.execute(
            "SELECT run_id, created_at, universe, params_json, result_json, event_count, source_marker "
            "FROM backtest_runs ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 100)),),
        ).fetchall()
    return [_run_payload(row, include_result=True) for row in rows]


def get_run(run_id: str) -> dict[str, Any] | None:
    if not SCANNER_DB_PATH.exists():
        return None
    with _connect() as connection:
        row = connection.execute("SELECT * FROM backtest_runs WHERE run_id = ?", (run_id,)).fetchone()
    return _run_payload(row, include_result=True) if row else None


def save_optimization(
    params: dict[str, Any], result: dict[str, Any], source_marker: str
) -> str:
    optimization_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        connection.execute(
            "INSERT INTO optimization_runs "
            "(optimization_id, created_at, params_json, result_json, source_marker) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                optimization_id,
                created_at,
                json.dumps(params, sort_keys=True),
                json.dumps(result, sort_keys=True),
                source_marker,
            ),
        )
    return optimization_id


def list_optimizations(limit: int = 30) -> list[dict[str, Any]]:
    if not SCANNER_DB_PATH.exists():
        return []
    with _connect() as connection:
        rows = connection.execute(
            "SELECT optimization_id, created_at, params_json, result_json, source_marker "
            "FROM optimization_runs ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 100)),),
        ).fetchall()
    summaries = []
    for row in rows:
        result = json.loads(row["result_json"])
        summaries.append(
            {
                "optimization_id": row["optimization_id"],
                "created_at": row["created_at"],
                "params": json.loads(row["params_json"]),
                "source_marker": row["source_marker"],
                "status": result.get("status"),
                "objective": result.get("objective"),
                "candidate_count": result.get("candidate_count", 0),
                "ranked_candidate_count": result.get("ranked_candidate_count", 0),
                "top_candidate": next(iter(result.get("top_candidates", [])), None),
            }
        )
    return summaries


def get_optimization(optimization_id: str) -> dict[str, Any] | None:
    if not SCANNER_DB_PATH.exists():
        return None
    with _connect() as connection:
        row = connection.execute(
            "SELECT optimization_id, created_at, params_json, result_json, source_marker "
            "FROM optimization_runs WHERE optimization_id = ?",
            (optimization_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "optimization_id": row["optimization_id"],
        "created_at": row["created_at"],
        "params": json.loads(row["params_json"]),
        "source_marker": row["source_marker"],
        "result": json.loads(row["result_json"]),
    }


def _run_payload(row: sqlite3.Row, include_result: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": row["run_id"],
        "created_at": row["created_at"],
        "universe": row["universe"],
        "params": json.loads(row["params_json"]),
        "event_count": int(row["event_count"]),
        "source_marker": row["source_marker"],
    }
    if include_result:
        payload["result"] = json.loads(row["result_json"])
    return payload


def event_rows(run_id: str, cohort: str | None = None) -> Iterator[sqlite3.Row]:
    if not SCANNER_DB_PATH.exists():
        return iter(())
    connection = _connect()
    if cohort in {"near", "control"}:
        cursor = connection.execute(
            "SELECT * FROM backtest_events WHERE run_id = ? AND cohort = ? "
            "ORDER BY market_date, ticker",
            (run_id, cohort),
        )
    else:
        cursor = connection.execute(
            "SELECT * FROM backtest_events WHERE run_id = ? ORDER BY market_date, ticker",
            (run_id,),
        )

    def iterate() -> Iterator[sqlite3.Row]:
        try:
            yield from cursor
        finally:
            connection.close()

    return iterate()


def paged_events(run_id: str, cohort: str, limit: int, offset: int) -> list[dict[str, Any]]:
    if not SCANNER_DB_PATH.exists():
        return []
    with _connect() as connection:
        rows = connection.execute(
            "SELECT market_date, execution_date, security_id, ticker, name, distance_pct, "
            "high_52w, signal_close, entry_open, ret_21_net, ret_63_net, ret_126_net, ret_252_net "
            "FROM backtest_events WHERE run_id = ? AND cohort = ? "
            "ORDER BY market_date DESC, ticker LIMIT ? OFFSET ?",
            (run_id, cohort, max(1, min(limit, 250)), max(0, offset)),
        ).fetchall()
    return [dict(row) for row in rows]


def event_count(run_id: str, cohort: str) -> int:
    if not SCANNER_DB_PATH.exists():
        return 0
    with _connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM backtest_events WHERE run_id = ? AND cohort = ?",
            (run_id, cohort),
        ).fetchone()
    return int(row[0]) if row else 0
