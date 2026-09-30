from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from backend import live, source
from backend.research import ResearchError, run_event_study, screener, security_detail


class NormalizationTests(unittest.TestCase):
    def test_adjusts_all_ohlc_once_and_excludes_incomplete_session(self):
        frame = pd.DataFrame({
            "Open": [100, 110], "High": [105, 115], "Low": [95, 105],
            "Close": [100, 110], "Adj Close": [90, 110], "Volume": [1000, 1100],
            "Dividends": [1, 0], "Stock Splits": [4, 0],
        }, index=pd.to_datetime(["2026-09-28", "2026-09-29"]))
        adjusted, raw = live.normalize_bars(frame, "TEST", ["2026-09-28"])
        self.assertEqual(len(adjusted), 1)
        self.assertEqual(adjusted[0]["close"], 90)
        self.assertEqual(adjusted[0]["open"], 90)
        self.assertEqual(adjusted[0]["high"], 94.5)
        self.assertEqual(adjusted[0]["volume"], 1000)
        self.assertEqual(raw[0]["close"], 100)

    def test_invalid_ohlc_is_excluded(self):
        frame = pd.DataFrame({"Open": [100], "High": [95], "Low": [90], "Close": [100],
                              "Adj Close": [100], "Volume": [1]}, index=pd.to_datetime(["2026-09-28"]))
        self.assertEqual(live.normalize_bars(frame, "TEST", ["2026-09-28"]), ([], []))

    def test_multiindex_both_yahoo_layouts(self):
        for columns in [pd.MultiIndex.from_tuples([("AAPL", "Close")]),
                        pd.MultiIndex.from_tuples([("Close", "AAPL")])]:
            frame = pd.DataFrame([[1]], columns=columns)
            self.assertIn("Close", live.frame_for_symbol(frame, "AAPL").columns)
            self.assertIsNone(live.frame_for_symbol(frame, "MSFT"))


class HoldingsTests(unittest.TestCase):
    @staticmethod
    def workbook() -> bytes:
        content = io.BytesIO()
        rows = [["Holdings:", "As of 29-Sep-2026"], ["Name", "Ticker"]]
        rows += [[f"Company {index}", "T" + chr(65 + index // 26) + chr(65 + index % 26)] for index in range(30)]
        rows += [["Disclaimer", None], ["Cash", "USD"]]
        pd.DataFrame(rows).to_excel(content, header=False, index=False)
        return content.getvalue()

    def test_ignores_empty_tickers_and_cash(self):
        members, day = live.parse_holdings(self.workbook(), "dow", "2026-09-29")
        self.assertEqual(len(members), 30)
        self.assertEqual(day, "2026-09-29")

    def test_rejects_stale_holdings(self):
        with self.assertRaises(live.UpdateError):
            live.parse_holdings(self.workbook(), "dow", "2026-09-30")

    def test_rejects_truncated_or_duplicate_member_lists(self):
        for members in ([{"ticker": "AAPL"}], [{"ticker": "AAPL"}] * 30):
            with self.assertRaises(live.UpdateError):
                live.validate_members("dow", members)


class CalendarTests(unittest.TestCase):
    def test_open_session_is_never_published(self):
        target, _ = live.market_window(datetime(2026, 9, 30, 14, tzinfo=timezone.utc))
        self.assertEqual(target, "2026-09-29")

    def test_weekend_and_holiday(self):
        target, _ = live.market_window(datetime(2026, 7, 5, 12, tzinfo=timezone.utc))
        self.assertEqual(target, "2026-07-02")

    def test_early_close_respects_two_hour_buffer(self):
        before, _ = live.market_window(datetime(2026, 11, 27, 19, tzinfo=timezone.utc))
        after, _ = live.market_window(datetime(2026, 11, 27, 21, tzinfo=timezone.utc))
        self.assertEqual(before, "2026-11-25")
        self.assertEqual(after, "2026-11-27")


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.history = root / "history.sqlite"
        self.patches = [patch.object(live, "LIVE_DATA_DIR", root / "live"),
                        patch.object(live, "NORGATE_DB_PATH", self.history),
                        patch.object(source, "NORGATE_DB_PATH", self.history),
                        patch.object(live, "_security_ids", return_value={"TEST": (42, "Tech")})]
        for item in self.patches:
            item.start()
        self.calendar = [item.date().isoformat() for item in pd.bdate_range("2025-07-01", periods=320)]
        self.members = {"dow": [{"ticker": "TEST", "name": "Test Company"}]}
        self.prices = {"TEST": [{"date": day, "open": 100, "high": 102, "low": 98,
                                 "close": 100, "volume": 500} for day in self.calendar]}
        self.manifest = {"run_id": "test", "finished_at": "2026-09-30", "failures": {}, "warnings": []}
        live.publish_snapshot(self.members, {}, self.prices, self.calendar[:-1], self.manifest)
        # This is a disposable, read-only baseline fixture, never the real Norgate file.
        with closing(sqlite3.connect(live.live_database_path())) as src, closing(sqlite3.connect(self.history)) as dst, dst:
            src.backup(dst)
            dst.execute("UPDATE metadata SET value = 'historical' WHERE key = 'source_kind'")
            dst.execute("UPDATE metadata SET value = 'TOTALRETURN' WHERE key = 'price_adjustment'")
        live.publish_snapshot(self.members, {}, self.prices, self.calendar, self.manifest)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_latest_screen_uses_free_prices_and_details_same_source(self):
        with source.open_screen_source("dow", None) as conn:
            result = screener(conn, "dow", None)
            self.assertEqual(result["data_source"], "live")
            self.assertEqual(result["as_of_date"], self.calendar[-1])
            self.assertEqual(result["eligible_count"], 1)
            detail = security_detail(conn, "dow", 42, self.calendar[-1])
            self.assertEqual(detail["data_source"], "live")

    def test_historical_date_stays_on_original_database(self):
        with source.open_screen_source("dow", self.calendar[-2]) as conn:
            self.assertEqual(source.read_metadata(conn)["price_adjustment"], "TOTALRETURN")

    def test_weekend_after_historical_end_keeps_previous_historical_session(self):
        with closing(sqlite3.connect(self.history)) as conn, conn:
            conn.execute("UPDATE indices SET date = '2026-09-25' WHERE date = ?", (self.calendar[-2],))
            conn.execute("DELETE FROM indices WHERE date > '2026-09-25'")
        with source.open_screen_source("dow", "2026-09-26") as conn:
            self.assertEqual(source.read_metadata(conn)["source_kind"], "historical")

    def test_new_security_ids_are_stable_and_exact_in_browser(self):
        # Temporarily remove the identity fixture to exercise real ID generation.
        self.patches[-1].stop()
        first = live._security_ids({"dow": [{"ticker": "NEWCOMPANY", "name": "New"}]})
        second = live._security_ids({"dow": [{"ticker": "NEWCOMPANY", "name": "New"}]})
        self.assertEqual(first, second)
        self.assertLess(first["NEWCOMPANY"][0], 0)
        self.assertLess(abs(first["NEWCOMPANY"][0]), 2**53)
        self.patches[-1].start()

    def test_no_backfill_of_current_memberships(self):
        with source.open_source(live.live_database_path()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(DISTINCT date) FROM index_membership").fetchone()[0], 1)
            with self.assertRaises(ResearchError):
                screener(conn, "dow", self.calendar[-2])
            with self.assertRaises(ResearchError):
                run_event_study(conn, {})

    def test_unobserved_membership_in_gap_is_rejected(self):
        with closing(sqlite3.connect(self.history)) as conn, conn:
            conn.execute("DELETE FROM indices WHERE date >= ?", (self.calendar[-2],))
        with self.assertRaises(source.SourceError):
            with source.open_screen_source("dow", self.calendar[-2]):
                self.fail("Must not invent historical members")

    def test_failed_publish_rolls_back_and_preserves_history(self):
        before = self.history.read_bytes()
        bad_prices = {"TEST": [{"date": self.calendar[-1], "open": 3}]}
        with self.assertRaises(KeyError):
            live.publish_snapshot(self.members, {}, bad_prices, self.calendar, self.manifest)
        with source.open_source(live.live_database_path()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0], 320)
        self.assertEqual(before, self.history.read_bytes())

    def test_update_rejection_preserves_previous_snapshot(self):
        with self.assertLogs(live.logger, level="ERROR"), \
             patch.object(live, "market_window", return_value=("2099-01-01", ["2099-01-01"])), \
             patch.object(live, "download_members", return_value=(self.members, {})), \
             patch.object(live, "download_prices", return_value=({}, {"TEST": "missing"})):
            result = live.refresh_live_data(force=True)
        self.assertEqual(result["status"], "failed")
        with source.open_source(live.live_database_path()) as conn:
            self.assertEqual(source.read_metadata(conn)["snapshot_date"], self.calendar[-1])

    def test_source_connection_rejects_writes(self):
        with source.open_source() as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM prices")


if __name__ == "__main__":
    unittest.main()
