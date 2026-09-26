from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .settings import NORGATE_DB_PATH


UNIVERSES: dict[str, dict[str, object]] = {
    "nasdaq100": {"label": "Nasdaq 100", "expected_members": 100},
    "sp500": {"label": "S&P 500", "expected_members": 500},
    "dow": {"label": "Dow Jones 30", "expected_members": 30},
}

REQUIRED_COLUMNS = {
    "prices": {"assetid", "date", "open", "high", "low", "close", "volume", "symbol_at_load_time", "security_name"},
    "index_membership": {"universe", "assetid", "date", "is_member", "symbol_at_load_time", "security_name"},
    "indices": {"universe", "date"},
    "securities": {"assetid", "current_symbol", "security_name"},
    "metadata": {"key", "value"},
}


class SourceError(RuntimeError):
    """Lesbare Meldung bei nicht erreichbarer oder inkompatibler Quelldatenbank."""


def database_path() -> Path:
    return NORGATE_DB_PATH


@contextmanager
def open_source() -> Iterator[sqlite3.Connection]:
    path = database_path()
    if not path.is_file():
        raise SourceError(
            f"Norgate-Datenbank nicht gefunden: {path}. Bitte NORGATE_DB_PATH in .env prüfen."
        )
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 20000")
        validate_schema(connection)
    except sqlite3.Error as exc:
        raise SourceError(f"Die Norgate-Datei kann nicht gelesen werden: {exc}") from exc
    try:
        yield connection
    finally:
        connection.close()


def validate_schema(connection: sqlite3.Connection) -> None:
    table_names = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing_tables = sorted(set(REQUIRED_COLUMNS) - table_names)
    if missing_tables:
        raise SourceError("Inkompatibles Norgate-Schema. Es fehlen Tabellen: " + ", ".join(missing_tables))
    for table, required in REQUIRED_COLUMNS.items():
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        missing = sorted(required - columns)
        if missing:
            raise SourceError(
                f"Inkompatibles Norgate-Schema in {table}. Es fehlen Spalten: " + ", ".join(missing)
            )


def read_metadata(connection: sqlite3.Connection) -> dict[str, str]:
    return {str(row["key"]): str(row["value"]) for row in connection.execute("SELECT key, value FROM metadata")}


def source_status(connection: sqlite3.Connection | None = None) -> dict[str, object]:
    if connection is None:
        with open_source() as opened:
            return source_status(opened)

    path = database_path()
    try:
        file_stat = path.stat()
        modified = datetime.fromtimestamp(file_stat.st_mtime, timezone.utc).isoformat()
        bytes_size = file_stat.st_size
    except OSError:
        modified, bytes_size = None, None

    metadata = read_metadata(connection)
    complete = metadata.get("manifest_is_complete", "unknown").strip().lower() in {"true", "1", "yes"}
    table_counts = {
        str(row["table_name"]): int(row["row_count"])
        for row in connection.execute("SELECT table_name, row_count FROM table_counts")
    }
    universes: list[dict[str, object]] = []
    warnings: list[str] = []
    if not complete:
        warnings.append("Das Norgate-Exportmanifest kennzeichnet den Datenbankexport als unvollständig.")

    all_dates: list[str] = []
    for code, definition in UNIVERSES.items():
        span = connection.execute(
            "SELECT MIN(date) AS start_date, MAX(date) AS end_date, COUNT(*) AS sessions "
            "FROM indices WHERE universe = ?",
            (code,),
        ).fetchone()
        end_date = span["end_date"] if span else None
        start_date = span["start_date"] if span else None
        members = 0
        if end_date:
            count_row = connection.execute(
                "SELECT COUNT(*) FROM index_membership "
                "WHERE universe = ? AND date = ? AND is_member = 1",
                (code, end_date),
            ).fetchone()
            members = int(count_row[0]) if count_row else 0
            all_dates.append(str(end_date))
        expected = int(definition["expected_members"])
        universe_warning = None
        if end_date and members != expected:
            universe_warning = (
                f"{definition['label']}: {members} Mitglieder am letzten Stichtag; "
                f"der Index umfasst üblicherweise etwa {expected}."
            )
            warnings.append(universe_warning)
        universes.append(
            {
                "code": code,
                "label": definition["label"],
                "expected_members": expected,
                "latest_members": members,
                "start_date": start_date,
                "end_date": end_date,
                "sessions": int(span["sessions"] or 0) if span else 0,
                "warning": universe_warning,
            }
        )

    if not all_dates:
        warnings.append("Die Norgate-Datenbank enthält keine Indexsessions für die unterstützten Universen.")

    return {
        "available": True,
        "database_path": str(path),
        "database_name": path.name,
        "database_bytes": bytes_size,
        "database_modified_at": modified,
        "source": metadata.get("source", "Norgate SQLite"),
        "price_adjustment": metadata.get("price_adjustment", "unbekannt"),
        "manifest_complete": complete,
        "manifest_updated_at": metadata.get("manifest_last_updated_at"),
        "start_date": min((str(u["start_date"]) for u in universes if u["start_date"]), default=None),
        "end_date": max(all_dates, default=None),
        "security_count": table_counts.get("securities"),
        "price_rows": table_counts.get("prices"),
        "quality_status": "limited" if warnings else "available",
        "quality_label": "Eingeschränktes Research" if warnings else "Datenstand verfügbar",
        "warnings": warnings,
        "universes": universes,
    }


def session_dates(connection: sqlite3.Connection, universe: str) -> list[str]:
    if universe not in UNIVERSES:
        raise SourceError("Unbekanntes Universum.")
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT date FROM indices WHERE universe = ? ORDER BY date",
            (universe,),
        )
    ]
