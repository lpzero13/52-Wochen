"""Free EOD snapshots for screening. Never writes to the Norgate archive."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .settings import LIVE_DATA_DIR, NORGATE_DB_PATH, SCANNER_DB_PATH

NASDAQ_URL = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"
HOLDINGS_URL = "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-{ticker}.xlsx"
MEMBER_LIMITS = {"nasdaq100": (100, 105), "sp500": (500, 510), "dow": (30, 30)}
PRICE_BASIS = "Yahoo dividend-/split-adjusted OHLC (Adj Close / Close)"
logger = logging.getLogger(__name__)
_thread_guard = threading.Lock()
_update_thread: threading.Thread | None = None


class UpdateError(RuntimeError):
    pass


def live_database_path() -> Path:
    return LIVE_DATA_DIR / "current.sqlite"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def update_status() -> dict[str, Any]:
    try:
        return json.loads((LIVE_DATA_DIR / "last_update.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "idle", "message": "Noch keine kostenlose Aktualisierung durchgeführt."}


@contextmanager
def _update_lock():
    """An OS lock survives process crashes without leaving a stale lock."""
    LIVE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (LIVE_DATA_DIR / "update.lock").open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise UpdateError("Eine andere Datenaktualisierung läuft bereits.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def market_window(now: datetime | None = None) -> tuple[str, list[str]]:
    """NYSE calendar includes holidays, early closes and a 2h publication buffer."""
    import exchange_calendars as xc
    import pandas as pd

    stamp = pd.Timestamp(now or datetime.now(timezone.utc))
    calendar = xc.get_calendar("XNYS")
    schedule = calendar.schedule.loc[
        (stamp - pd.Timedelta(days=760)).date().isoformat():stamp.date().isoformat()
    ]
    completed = schedule[schedule["close"] + pd.Timedelta(hours=2) <= stamp]
    if completed.empty:
        raise UpdateError("Keine abgeschlossene US-Handelssession im Börsenkalender gefunden.")
    dates = [item.date().isoformat() for item in completed.index]
    return dates[-1], dates


def _fetch(url: str, destination: Path) -> tuple[bytes, dict[str, Any]]:
    import requests

    last_error = None
    for attempt in range(3):
        try:
            response = requests.get(
                url, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"}, timeout=(10, 30)
            )
            response.raise_for_status()
            if not response.content:
                raise UpdateError("Leere Anbieterantwort")
            destination.write_bytes(response.content)
            return response.content, {
                "url": url, "observed_at": datetime.now(timezone.utc).isoformat(),
                "sha256": hashlib.sha256(response.content).hexdigest(),
            }
        except (requests.RequestException, UpdateError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise UpdateError(f"Mitgliederquelle nicht erreichbar ({url}): {last_error}")


def validate_members(universe: str, rows: list[dict[str, str]]) -> None:
    lower, upper = MEMBER_LIMITS[universe]
    tickers = [item["ticker"] for item in rows]
    if not lower <= len(rows) <= upper or len(set(tickers)) != len(tickers):
        raise UpdateError(f"{universe}: unplausible oder doppelte Mitglieder ({len(rows)}).")
    if any(not re.fullmatch(r"[A-Z][A-Z0-9.\-]*", ticker) for ticker in tickers):
        raise UpdateError(f"{universe}: ungültiges Tickersymbol in Mitgliederquelle.")


def parse_holdings(content: bytes, universe: str, target: str) -> tuple[list[dict[str, str]], str]:
    import pandas as pd

    table = pd.read_excel(io.BytesIO(content), header=None)
    headers = table.index[table.iloc[:, 0].astype(str).str.strip().eq("Name")].tolist()
    if not headers:
        raise UpdateError(f"{universe}: Holdings-Kopfzeile fehlt.")
    header = int(headers[0])
    date_text = " ".join(table.iloc[:header].fillna("").astype(str).values.flatten())
    match = re.search(r"As of\s+(\d{1,2}-[A-Za-z]{3}-\d{4})", date_text)
    if not match:
        raise UpdateError(f"{universe}: Holdings-Stichtag fehlt.")
    source_date = pd.to_datetime(match.group(1), format="%d-%b-%Y").date().isoformat()
    # Do not claim that stale or future holdings describe today's EOD universe.
    if source_date != target:
        raise UpdateError(f"{universe}: Holdings vom {source_date}, benötigt wird {target}; später erneut versuchen.")
    table.columns = [str(item).strip() for item in table.iloc[header]]
    rows = []
    for _, row in table.iloc[header + 1:].iterrows():
        if pd.isna(row.get("Ticker")):
            continue
        ticker = str(row.get("Ticker", "")).strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]*", ticker):
            continue  # Exclude cash, disclaimers and money market positions.
        name = str(row.get("Name", ticker)).strip()
        if "CASH" in name.upper() or "MONEY MARKET" in name.upper():
            continue
        rows.append({"ticker": ticker, "name": name})
    validate_members(universe, rows)
    return rows, source_date


def download_members(target: str, run_dir: Path) -> tuple[dict, dict]:
    raw, meta = _fetch(NASDAQ_URL, run_dir / "nasdaq100.json")
    try:
        payload = json.loads(raw)["data"]
        rows = [{"ticker": item["symbol"].strip().upper(), "name": item["companyName"]}
                for item in payload["data"]["rows"]]
        if int(payload["totalrecords"]) != len(rows):
            raise UpdateError("Nasdaq liefert nur einen Teil der Mitgliederliste.")
    except (KeyError, TypeError, ValueError) as exc:
        raise UpdateError("Nasdaq-Mitgliederliste hat ein unerwartetes Format.") from exc
    validate_members("nasdaq100", rows)
    # Nasdaq's endpoint is a CURRENT snapshot, not a dated historical membership feed.
    meta.update({"source_date": None, "basis": "current-observed-list"})
    members, sources = {"nasdaq100": rows}, {"nasdaq100": meta}
    for universe, ticker in (("sp500", "spy"), ("dow", "dia")):
        content, meta = _fetch(HOLDINGS_URL.format(ticker=ticker), run_dir / f"{ticker}.xlsx")
        rows, source_date = parse_holdings(content, universe, target)
        meta.update({"source_date": source_date, "basis": f"{ticker.upper()}-ETF-holdings-proxy"})
        members[universe], sources[universe] = rows, meta
    _write_json(run_dir / "constituents.json", {"members": members, "sources": sources})
    return members, sources


def frame_for_symbol(frame, symbol: str):
    if frame is None or frame.empty:
        return None
    if getattr(frame.columns, "nlevels", 1) == 1:
        return frame
    for level in (0, 1):
        if symbol in frame.columns.get_level_values(level):
            return frame.xs(symbol, axis=1, level=level)
    return None


def normalize_bars(frame, symbol: str, calendar: list[str]) -> tuple[list[dict], list[dict]]:
    """One consistent Yahoo basis for the whole window, including past dividends."""
    allowed = set(calendar)
    adjusted, original = [], []
    if frame is None:
        return adjusted, original
    for stamp, values in frame.iterrows():
        day = stamp.date().isoformat()
        if day not in allowed:
            continue
        fields = ("Open", "High", "Low", "Close", "Adj Close", "Volume")
        try:
            numbers = [float(values[field]) for field in fields]
        except (KeyError, ValueError, TypeError):
            continue
        if not all(math.isfinite(value) for value in numbers):
            continue
        op, hi, lo, close, adj_close, volume = numbers
        if min(op, hi, lo, close, adj_close) <= 0 or volume < 0:
            continue
        if hi < max(op, close) or lo > min(op, close) or hi < lo:
            continue
        factor = adj_close / close
        raw = {"ticker": symbol, "date": day, "open": op, "high": hi, "low": lo,
               "close": close, "adj_close": adj_close, "volume": volume,
               "dividend": float(values.get("Dividends", 0) or 0),
               "split": float(values.get("Stock Splits", 0) or 0)}
        original.append(raw)
        adjusted.append({**raw, "open": op * factor, "high": hi * factor,
                         "low": lo * factor, "close": adj_close})
    return adjusted, original


def download_prices(symbols: list[str], calendar: list[str], run_dir: Path, progress) -> tuple[dict, dict]:
    import yfinance as yf

    prices, failures = {}, {}
    end = (datetime.fromisoformat(calendar[-1]) + timedelta(days=1)).date().isoformat()
    with gzip.open(run_dir / "yahoo-bars.jsonl.gz", "wt", encoding="utf-8") as archive:
        for offset in range(0, len(symbols), 30):
            batch = symbols[offset:offset + 30]
            progress(f"Yahoo-Kurse: {offset}/{len(symbols)} Aktien geladen …")
            try:
                frame = yf.download(batch, start=calendar[0], end=end, auto_adjust=False,
                                    actions=True, repair=True, group_by="ticker", threads=3,
                                    progress=False, timeout=20)
            except Exception as exc:
                frame = None
                for symbol in batch:
                    failures[symbol] = str(exc)
            for symbol in batch:
                bars, raw = normalize_bars(frame_for_symbol(frame, symbol), symbol, calendar)
                if not bars or bars[-1]["date"] != calendar[-1]:
                    # Retry single tickers; a provider error must not remove valid cache data.
                    try:
                        retry = yf.download(symbol, start=calendar[0], end=end, auto_adjust=False,
                                            actions=True, repair=True, threads=False,
                                            progress=False, timeout=20)
                        bars, raw = normalize_bars(frame_for_symbol(retry, symbol), symbol, calendar)
                    except Exception as exc:
                        failures[symbol] = str(exc)
                if bars and bars[-1]["date"] == calendar[-1]:
                    prices[symbol] = bars
                    failures.pop(symbol, None)
                else:
                    failures.setdefault(symbol, "Keine gültigen Kurse bis zur Zielsession.")
                for row in raw:
                    archive.write(json.dumps(row, allow_nan=False) + "\n")
            time.sleep(0.5)
    return prices, failures


def _security_ids(members: dict) -> dict[str, tuple[int, str | None]]:
    """Reuse only active, unambiguous Norgate IDs; new symbols get stable negative IDs."""
    mapping: dict[str, tuple[int, str | None]] = {}
    if NORGATE_DB_PATH.is_file():
        with closing(sqlite3.connect(f"{NORGATE_DB_PATH.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            columns = {row[1] for row in conn.execute("PRAGMA table_info(securities)")}
            if "is_active_database" in columns:
                candidates: dict[str, list] = {}
                for row in conn.execute("SELECT assetid, current_symbol, gics_sector FROM securities WHERE is_active_database = 1 AND last_quoted_date IS NULL"):
                    if not row["current_symbol"]:
                        continue
                    symbol = row["current_symbol"].replace(".", "-")
                    candidates.setdefault(symbol, []).append(row)
                for symbol, rows in candidates.items():
                    if len(rows) == 1:
                        mapping[symbol] = (int(rows[0]["assetid"]), rows[0]["gics_sector"])
    for rows in members.values():
        for row in rows:
            symbol = row["ticker"].replace(".", "-")
            if symbol not in mapping:
                digest = hashlib.sha256(symbol.encode("utf-8")).digest()
                # Stay inside JavaScript's exact integer range for UI/API IDs.
                mapping[symbol] = (-int.from_bytes(digest[:6], "big") - 1, None)
    return mapping


def publish_snapshot(members: dict, sources: dict, prices: dict, calendar: list[str], manifest: dict) -> None:
    path = live_database_path()
    if path.resolve() in {NORGATE_DB_PATH.resolve(), SCANNER_DB_PATH.resolve()}:
        raise UpdateError("Der Live-Cache darf nicht auf die Norgate- oder Research-Datenbank zeigen.")
    mapping = _security_ids(members)
    names = {row["ticker"].replace(".", "-"): row["name"] for rows in members.values() for row in rows}
    used_ids = [mapping[symbol][0] for symbol in names]
    if len(set(used_ids)) != len(used_ids):
        raise UpdateError("Mehrere Ticker wurden auf dieselbe Wertpapier-ID abgebildet.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=30)) as conn, conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS securities (assetid INTEGER PRIMARY KEY, current_symbol TEXT,
                security_name TEXT, currency TEXT, gics_sector TEXT, first_quoted_date TEXT, last_quoted_date TEXT);
            CREATE TABLE IF NOT EXISTS prices (assetid INTEGER, date TEXT, open REAL, high REAL,
                low REAL, close REAL, volume REAL, symbol_at_load_time TEXT, security_name TEXT,
                PRIMARY KEY (assetid, date));
            CREATE TABLE IF NOT EXISTS indices (universe TEXT, date TEXT, PRIMARY KEY (universe, date));
            CREATE TABLE IF NOT EXISTS index_membership (universe TEXT, assetid INTEGER, date TEXT,
                is_member INTEGER, symbol_at_load_time TEXT, security_name TEXT,
                PRIMARY KEY (universe, assetid, date));
            CREATE TABLE IF NOT EXISTS table_counts (table_name TEXT PRIMARY KEY, row_count INTEGER);
        """)
        # Everything is replaced in ONE transaction. Readers see old or new, never a mixture.
        conn.execute("BEGIN IMMEDIATE")
        for table in ("metadata", "securities", "prices", "indices", "index_membership", "table_counts"):
            conn.execute(f"DELETE FROM {table}")
        for symbol, bars in prices.items():
            asset_id, sector = mapping[symbol]
            conn.execute("INSERT INTO securities VALUES (?,?,?,?,?,?,?)",
                         (asset_id, symbol, names[symbol], "USD", sector, bars[0]["date"], None))
            conn.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?,?,?)", [
                (asset_id, row["date"], row["open"], row["high"], row["low"], row["close"],
                 row["volume"], symbol, names[symbol]) for row in bars
            ])
        for universe, rows in members.items():
            conn.executemany("INSERT INTO indices VALUES (?,?)", [(universe, day) for day in calendar])
            for row in rows:
                symbol = row["ticker"].replace(".", "-")
                # Also record members whose prices failed so the screener reports exclusions.
                if symbol not in prices:
                    asset_id, sector = mapping[symbol]
                    conn.execute("INSERT OR IGNORE INTO securities VALUES (?,?,?,?,?,?,?)",
                                 (asset_id, symbol, row["name"], "USD", sector, None, None))
                conn.execute("INSERT INTO index_membership VALUES (?,?,?,?,?,?)",
                             (universe, mapping[symbol][0], calendar[-1], 1, symbol, row["name"]))
        metadata = {"source": "Yahoo Finance + Nasdaq + SPY/DIA holdings",
                    "source_kind": "live", "price_adjustment": PRICE_BASIS,
                    "manifest_is_complete": str(not manifest["failures"]).lower(),
                    "manifest_last_updated_at": manifest["finished_at"],
                    "snapshot_date": calendar[-1], "membership_sources": json.dumps(sources),
                    "run_id": manifest["run_id"], "warnings": json.dumps(manifest["warnings"])}
        conn.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
        for table in ("prices", "securities", "indices", "index_membership"):
            conn.execute(f"INSERT INTO table_counts VALUES (?, (SELECT COUNT(*) FROM {table}))", (table,))


def refresh_live_data(force: bool = False) -> dict:
    with _update_lock():
        target, calendar = market_window()
        if not force and live_database_path().is_file():
            with closing(sqlite3.connect(f"{live_database_path().resolve().as_uri()}?mode=ro", uri=True)) as conn:
                row = conn.execute("SELECT value FROM metadata WHERE key = 'snapshot_date'").fetchone()
                if row and row[0] == target:
                    result = {"status": "up_to_date", "target_date": target, "message": f"Kostenloser Cache bereits aktuell bis {target}."}
                    _write_json(LIVE_DATA_DIR / "last_update.json", result)
                    return result
        run_id = uuid.uuid4().hex
        run_dir = LIVE_DATA_DIR / "archive" / run_id
        run_dir.mkdir(parents=True)
        manifest = {"run_id": run_id, "status": "running", "target_date": target,
                    "started_at": datetime.now(timezone.utc).isoformat(), "price_basis": PRICE_BASIS}

        def progress(message):
            _write_json(LIVE_DATA_DIR / "last_update.json", {**manifest, "message": message})

        try:
            progress("Aktuelle Indexmitglieder von Nasdaq und State Street laden …")
            members, sources = download_members(target, run_dir)
            symbols = sorted({row["ticker"].replace(".", "-") for rows in members.values() for row in rows})
            prices, failures = download_prices(symbols, calendar, run_dir, progress)
            coverage = {}
            for universe, rows in members.items():
                usable = sum(row["ticker"].replace(".", "-") in prices for row in rows)
                coverage[universe] = {"members": len(rows), "latest_prices": usable}
                if usable / len(rows) < 0.95:
                    raise UpdateError(f"{universe}: Nur {usable}/{len(rows)} aktuelle Kursreihen; bisheriger Cache bleibt erhalten.")
            warnings = [
                "Aktueller Scanner: Yahoo-Kurse mit eigener Dividenden-/Split-Bereinigung; Norgate-Backtests verwenden weiterhin ausschließlich die historische Datei.",
                "S&P 500 und Dow: SPY-/DIA-Bestände als Mitglieder-Proxy. Nasdaq: aktuelle beobachtete Liste ohne historischen Gültigkeitsstichtag; keine rückwirkenden Point-in-Time-Mitgliedschaften.",
            ]
            if failures:
                warnings.append(f"{len(failures)} Aktien ohne aktuelle Yahoo-Kurse; Ausschlüsse werden im Scanner angezeigt.")
            manifest.update({"sources": sources, "coverage": coverage, "failures": failures,
                             "warnings": warnings, "finished_at": datetime.now(timezone.utc).isoformat()})
            progress("Kursabdeckung geprüft; kostenlosen Cache veröffentlichen …")
            publish_snapshot(members, sources, prices, calendar, manifest)
            manifest.update({"status": "published", "message": f"Kostenlose Daten bis {target} aktualisiert ({len(prices)}/{len(symbols)} Aktien)."})
        except Exception as exc:
            manifest.update({"status": "failed", "finished_at": datetime.now(timezone.utc).isoformat(),
                             "message": str(exc)})
            logger.exception("Kostenlose Datenaktualisierung fehlgeschlagen")
        _write_json(run_dir / "manifest.json", manifest)
        _write_json(LIVE_DATA_DIR / "last_update.json", manifest)
        return manifest


def _background_refresh(force: bool) -> None:
    try:
        refresh_live_data(force)
    except UpdateError:
        logger.info("Eine Datenaktualisierung läuft bereits.")
    except Exception as exc:
        logger.exception("Aktualisierung konnte nicht starten")
        _write_json(LIVE_DATA_DIR / "last_update.json", {"status": "failed", "message": str(exc)})


def start_refresh(force: bool = False) -> dict:
    global _update_thread
    with _thread_guard:
        if _update_thread is not None and _update_thread.is_alive():
            return update_status()
        _update_thread = threading.Thread(target=_background_refresh, args=(force,), daemon=True)
        _update_thread.start()
    return {"status": "running", "message": "Kostenlose Datenaktualisierung gestartet."}


def start_auto_updates() -> threading.Event:
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            start_refresh()
            stop.wait(3600)

    threading.Thread(target=loop, name="free-eod-updates", daemon=True).start()
    return stop


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aktuellen 52W-Scanner kostenlos aktualisieren.")
    parser.add_argument("--force", action="store_true", help="Auch einen bereits aktuellen Cache erneut laden.")
    args = parser.parse_args()
    try:
        result = refresh_live_data(force=args.force)
    except Exception as exc:
        result = {"status": "failed", "message": str(exc)}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(1 if result["status"] == "failed" else 0)
