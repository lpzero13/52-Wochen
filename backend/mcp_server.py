from __future__ import annotations

from datetime import date
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from . import live, storage
from .models import BacktestRequest, OptimizationRequest
from .optimizer import optimize_event_studies
from .research import ResearchError, run_event_study, screener, security_detail
from .source import SourceError, data_status, open_screen_source, open_source, session_dates


mcp = FastMCP(
    "52W High Research",
    instructions=(
        "Unabhängige lokale Werkzeuge für Screener, Ereignisstudien und die Suche nach "
        "52-Wochen-Hoch-Backtestvarianten. Die Norgate-Quelldatenbank wird ausschließlich "
        "lesend geöffnet. Optimierungen bewerten Varianten im zeitlich getrennten Prüfzeitraum; "
        "historische Ergebnisse sind keine Prognose."
    ),
)


def _model_params(model_type, values: dict[str, Any]) -> dict[str, Any]:
    try:
        model = model_type.model_validate(values)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise ValueError(f"Ungültige Backtest-Parameter: {details}") from exc
    return model.model_dump(mode="json")


def _requested_date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Datum muss im ISO-Format YYYY-MM-DD angegeben werden.") from exc
    return value


@mcp.tool()
def get_data_status() -> dict[str, Any]:
    """Zeigt Norgate-Historie, kostenlosen Scanner-Cache und Aktualisierungsstatus."""
    try:
        return data_status()
    except SourceError as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
def update_free_market_data(force: bool = False) -> dict[str, Any]:
    """Startet kostenloses EOD-Update; Fortschritt mit get_data_status abfragen."""
    return live.start_refresh(force=force)


@mcp.tool()
def scan_near_52w_high(
    universe: Literal["nasdaq100", "sp500", "dow"] = "sp500",
    as_of_date: str | None = None,
    threshold_pct: float = 5,
    limit: int = 50,
    data_source: Literal["auto", "historical", "live"] = "auto",
) -> dict[str, Any]:
    """Listet Aktien nahe ihrem 52-Wochen-Hoch heute oder an einem historischen Datum."""
    if not 1 <= limit <= 500:
        raise ValueError("limit muss zwischen 1 und 500 liegen.")
    as_of_date = _requested_date(as_of_date)
    try:
        with open_screen_source(universe, as_of_date, data_source) as connection:
            result = screener(connection, universe, as_of_date, threshold_pct)
    except (ResearchError, SourceError) as exc:
        raise ValueError(str(exc)) from exc
    rows = result.pop("rows")
    result["returned_count"] = min(limit, len(rows))
    result["rows"] = rows[:limit]
    return result


@mcp.tool()
def get_security_detail(
    asset_id: int,
    universe: Literal["nasdaq100", "sp500", "dow"] = "sp500",
    as_of_date: str | None = None,
    threshold_pct: float = 5,
    data_source: Literal["auto", "historical", "live"] = "auto",
) -> dict[str, Any]:
    """Liefert 252 Sitzungen Kursverlauf und Hoch-Abstand einer Indexaktie."""
    as_of_date = _requested_date(as_of_date)
    try:
        with open_screen_source(universe, as_of_date, data_source) as connection:
            return security_detail(connection, universe, asset_id, as_of_date, threshold_pct)
    except (ResearchError, SourceError) as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
def run_backtest(
    start_date: str,
    end_date: str,
    universe: Literal["nasdaq100", "sp500", "dow"] = "sp500",
    frequency: Literal["daily", "weekly", "monthly"] = "monthly",
    thresholds: list[float] | None = None,
    horizons: list[int] | None = None,
    control_min_pct: float = 15,
    control_max_pct: float = 25,
    cost_bps: float = 25,
) -> dict[str, Any]:
    """Berechnet und speichert eine einzelne 52-Wochen-Hoch-Ereignisstudie."""
    params = _model_params(
        BacktestRequest,
        {
            "start_date": start_date,
            "end_date": end_date,
            "universe": universe,
            "frequency": frequency,
            "thresholds": thresholds if thresholds is not None else [1, 3, 5, 10],
            "horizons": horizons if horizons is not None else [21, 63, 126, 252],
            "control_min_pct": control_min_pct,
            "control_max_pct": control_max_pct,
            "cost_bps": cost_bps,
        },
    )
    try:
        with open_source() as connection:
            result, events = run_event_study(connection, params)
            marker = f"{result['price_basis']} | Datenstand bis {result['source_end_date']}"
        storage.initialize()
        run_id = storage.save_run(params, result, events, marker)
    except (ResearchError, SourceError) as exc:
        raise ValueError(str(exc)) from exc
    return {"run_id": run_id, "params": params, "result": result}


@mcp.tool()
def optimize_backtests(
    universes: list[str] | None = None,
    start_date: str | None = None,
    validation_start_date: str | None = None,
    end_date: str | None = None,
    frequencies: list[str] | None = None,
    thresholds: list[float] | None = None,
    horizons: list[int] | None = None,
    costs_bps: list[float] | None = None,
    control_min_pct: float = 15,
    control_max_pct: float = 25,
    objective: Literal[
        "date_balanced_excess_vs_control",
        "mean_net",
        "median_net",
        "win_rate",
        "outperforming_date_rate",
    ] = "date_balanced_excess_vs_control",
    min_validation_events: int = 20,
    min_validation_dates: int = 8,
    top_n: int = 20,
) -> dict[str, Any]:
    """Testet ein Variantenraster und rankt es nach Ergebnissen im Prüfzeitraum.

    Ohne Datumsangaben verwendet die Suche die verfügbare Kursgeschichte, legt den
    Prüfzeitraum beim letzten Drittel an und nutzt das jüngste gemeinsame Datenende.
    """
    chosen_universes = universes if universes is not None else ["sp500"]
    try:
        with open_source() as connection:
            calendars = {name: session_dates(connection, name) for name in chosen_universes}
            if not calendars or any(not calendar for calendar in calendars.values()):
                raise ResearchError(
                    "Für mindestens ein ausgewähltes Universum fehlen Handelssitzungen."
                )

            latest_date = min(calendar[-1] for calendar in calendars.values())
            default_calendar = calendars[chosen_universes[0]]
            if len(default_calendar) < 3:
                raise ResearchError(
                    "Für eine zeitlich getrennte Suche reicht die Datenhistorie nicht aus."
                )
            default_validation_index = min(
                len(default_calendar) - 1,
                max(1, int((len(default_calendar) - 1) * 0.70)),
            )
            params = _model_params(
                OptimizationRequest,
                {
                    "universes": chosen_universes,
                    "start_date": start_date or default_calendar[0],
                    "validation_start_date": (
                        validation_start_date or default_calendar[default_validation_index]
                    ),
                    "end_date": end_date or latest_date,
                    "frequencies": (
                        frequencies if frequencies is not None else ["monthly", "weekly"]
                    ),
                    "thresholds": thresholds if thresholds is not None else [1, 3, 5, 10],
                    "horizons": horizons if horizons is not None else [21, 63, 126, 252],
                    "costs_bps": costs_bps if costs_bps is not None else [25],
                    "control_min_pct": control_min_pct,
                    "control_max_pct": control_max_pct,
                    "objective": objective,
                    "min_validation_events": min_validation_events,
                    "min_validation_dates": min_validation_dates,
                    "top_n": top_n,
                },
            )
            result = optimize_event_studies(connection, params)
            end_dates = ", ".join(
                f"{universe}: {as_of}"
                for universe, as_of in sorted(result["source_end_dates"].items())
            )
            marker = f"{result['price_basis']} | Datenstand bis {end_dates or 'unbekannt'}"
        storage.initialize()
        optimization_id = storage.save_optimization(params, result, marker)
    except (ResearchError, SourceError) as exc:
        raise ValueError(str(exc)) from exc
    return {"optimization_id": optimization_id, **result}


@mcp.tool()
def list_optimization_runs(limit: int = 10) -> dict[str, Any]:
    """Listet gespeicherte Parametersuchen mit ihrer jeweils besten Variante."""
    storage.initialize()
    return {"optimizations": storage.list_optimizations(limit)}


@mcp.tool()
def get_optimization_result(optimization_id: str) -> dict[str, Any]:
    """Liefert Rangliste, Suchparameter und Kennzahlen einer gespeicherten Suche."""
    storage.initialize()
    result = storage.get_optimization(optimization_id)
    if result is None:
        raise ValueError("Diese Optimierung ist nicht gespeichert.")
    return result


@mcp.tool()
def list_backtest_runs(limit: int = 10) -> dict[str, Any]:
    """Listet gespeicherte Einzel-Backtests."""
    storage.initialize()
    return {"backtests": storage.list_runs(limit)}


@mcp.tool()
def get_backtest_events(
    run_id: str,
    cohort: Literal["near", "control"] = "near",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Liefert paginierte Einzelereignisse zu einem gespeicherten Backtest."""
    if not 1 <= limit <= 250 or offset < 0:
        raise ValueError("limit muss zwischen 1 und 250 liegen; offset darf nicht negativ sein.")
    storage.initialize()
    if storage.get_run(run_id) is None:
        raise ValueError("Dieser Backtest ist nicht gespeichert.")
    return {
        "run_id": run_id,
        "cohort": cohort,
        "count": storage.event_count(run_id, cohort),
        "offset": offset,
        "events": storage.paged_events(run_id, cohort, limit, offset),
    }


if __name__ == "__main__":
    storage.initialize()
    mcp.run()
