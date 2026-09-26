from __future__ import annotations

from datetime import date as Date
from typing import Literal

from pydantic import BaseModel, Field


class BacktestRequest(BaseModel):
    universe: Literal["nasdaq100", "sp500", "dow"] = "sp500"
    start_date: Date
    end_date: Date
    frequency: Literal["daily", "weekly", "monthly"] = "monthly"
    thresholds: list[float] = Field(
        default_factory=lambda: [1, 3, 5, 10], min_length=1, max_length=8
    )
    horizons: list[int] = Field(
        default_factory=lambda: [21, 63, 126, 252], min_length=1, max_length=4
    )
    control_min_pct: float = Field(default=15, gt=0, le=50)
    control_max_pct: float = Field(default=25, gt=0, le=50)
    cost_bps: float = Field(default=25, ge=0, le=1000)


class OptimizationRequest(BaseModel):
    universes: list[Literal["nasdaq100", "sp500", "dow"]] = Field(
        default_factory=lambda: ["sp500"], min_length=1, max_length=3
    )
    start_date: Date
    validation_start_date: Date
    end_date: Date
    frequencies: list[Literal["daily", "weekly", "monthly"]] = Field(
        default_factory=lambda: ["monthly", "weekly"], min_length=1, max_length=3
    )
    thresholds: list[float] = Field(
        default_factory=lambda: [1, 3, 5, 10], min_length=1, max_length=8
    )
    horizons: list[int] = Field(
        default_factory=lambda: [21, 63, 126, 252], min_length=1, max_length=4
    )
    costs_bps: list[float] = Field(default_factory=lambda: [25], min_length=1, max_length=4)
    control_min_pct: float = Field(default=15, gt=0, le=50)
    control_max_pct: float = Field(default=25, gt=0, le=50)
    objective: Literal[
        "date_balanced_excess_vs_control",
        "mean_net",
        "median_net",
        "win_rate",
        "outperforming_date_rate",
    ] = "date_balanced_excess_vs_control"
    min_validation_events: int = Field(default=20, ge=1, le=100_000)
    min_validation_dates: int = Field(default=8, ge=1, le=10_000)
    top_n: int = Field(default=20, ge=1, le=100)
