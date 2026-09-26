from __future__ import annotations

import unittest
from datetime import date, timedelta

from backend.optimizer import _metric_summary, _period_split
from backend.research import ResearchError


class OptimizerPeriodTests(unittest.TestCase):
    def setUp(self) -> None:
        first = date(2024, 1, 1)
        self.calendar = [(first + timedelta(days=offset)).isoformat() for offset in range(20)]

    def test_training_exits_end_before_validation_and_validation_exits_by_end(self) -> None:
        period = _period_split(
            self.calendar,
            start_date=self.calendar[0],
            validation_start_date=self.calendar[10],
            end_date=self.calendar[19],
            max_horizon=3,
        )
        train_end = self.calendar.index(period["training_end_date"])
        validation_start = self.calendar.index(period["validation_start_date"])
        validation_end = self.calendar.index(period["validation_end_date"])
        requested_end = self.calendar.index(period["requested_end_date"])

        self.assertLess(train_end + 3, validation_start)
        self.assertLessEqual(validation_end + 3, requested_end)

    def test_rejects_validation_period_too_short_for_longest_horizon(self) -> None:
        with self.assertRaises(ResearchError):
            _period_split(
                self.calendar,
                start_date=self.calendar[0],
                validation_start_date=self.calendar[17],
                end_date=self.calendar[19],
                max_horizon=3,
            )


class OptimizerMetricTests(unittest.TestCase):
    def test_threshold_lookup_handles_json_float_keys(self) -> None:
        metrics = _metric_summary(
            {
                "results": {
                    "1.0": {
                        "21": {
                            "event_count": 42,
                            "signal_date_count": 12,
                            "comparison_date_count": 10,
                            "date_balanced_excess_vs_control": 0.025,
                        }
                    }
                }
            },
            threshold=1,
            horizon=21,
        )
        self.assertEqual(metrics["event_count"], 42)
        self.assertEqual(metrics["signal_date_count"], 12)
        self.assertEqual(metrics["comparison_date_count"], 10)
        self.assertEqual(metrics["date_balanced_excess_vs_control"], 0.025)


if __name__ == "__main__":
    unittest.main()
