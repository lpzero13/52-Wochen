from __future__ import annotations

import csv
import io
from contextlib import asynccontextmanager
from datetime import date as Date
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import live, storage
from .models import BacktestRequest, OptimizationRequest
from .optimizer import optimize_event_studies
from .research import ResearchError, run_event_study, screener, security_detail
from .settings import LIVE_AUTO_UPDATE, WEB_ROOT
from .source import SnapshotDateError, SourceError, data_status, open_screen_source, open_source


@asynccontextmanager
async def lifespan(_: FastAPI):
    storage.initialize()
    stop = live.start_auto_updates() if LIVE_AUTO_UPDATE else None
    try:
        yield
    finally:
        if stop:
            stop.set()


app = FastAPI(
    title="52W High Research",
    description="Eigenständiges Research zu Aktien nahe ihrem 52-Wochen-Hoch.",
    version="0.2.0",
    lifespan=lifespan,
)


@app.get("/api/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/status")
def status() -> dict:
    try:
        return data_status()
    except SourceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/data/update", status_code=202)
def update_free_data(force: bool = False) -> dict:
    return live.start_refresh(force=force)


@app.get("/api/data/update")
def free_update_status() -> dict:
    return live.update_status()


@app.get("/api/screener")
def get_screener(
    universe: Literal["nasdaq100", "sp500", "dow"] = "sp500",
    as_of_date: Date | None = None,
    threshold_pct: float = Query(default=5, ge=0, le=25),
    data_source: Literal["auto", "historical", "live"] = "auto",
) -> dict:
    try:
        with open_screen_source(universe, as_of_date.isoformat() if as_of_date else None, data_source) as connection:
            return screener(
                connection,
                universe,
                as_of_date.isoformat() if as_of_date else None,
                threshold_pct,
            )
    except (ResearchError, SnapshotDateError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SourceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/securities/{asset_id}")
def get_security(
    asset_id: int,
    universe: Literal["nasdaq100", "sp500", "dow"] = "sp500",
    as_of_date: Date | None = None,
    threshold_pct: float = Query(default=5, ge=0, le=25),
    data_source: Literal["auto", "historical", "live"] = "auto",
) -> dict:
    try:
        with open_screen_source(universe, as_of_date.isoformat() if as_of_date else None, data_source) as connection:
            return security_detail(
                connection,
                universe,
                asset_id,
                as_of_date.isoformat() if as_of_date else None,
                threshold_pct,
            )
    except (ResearchError, SnapshotDateError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SourceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/backtests")
def create_backtest(request: BacktestRequest) -> dict:
    params = request.model_dump(mode="json")
    try:
        with open_source() as connection:
            result, events = run_event_study(connection, params)
            marker = f"{result['price_basis']} | Datenstand bis {result['source_end_date']}"
        run_id = storage.save_run(params, result, events, marker)
        return {"run_id": run_id, "params": params, "result": result}
    except ResearchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SourceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/backtests")
def get_backtests(limit: int = Query(default=30, ge=1, le=100)) -> dict:
    return {"runs": storage.list_runs(limit)}


@app.get("/api/backtests/{run_id}")
def get_backtest(run_id: str) -> dict:
    run = storage.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Dieser Backtest ist nicht gespeichert.")
    return run


@app.post("/api/optimizations", summary="Search and rank backtest variants")
def create_optimization(request: OptimizationRequest) -> dict:
    params = request.model_dump(mode="json")
    try:
        with open_source() as connection:
            result = optimize_event_studies(connection, params)
            end_dates = ", ".join(
                f"{universe}: {end_date}"
                for universe, end_date in sorted(result["source_end_dates"].items())
            )
            marker = f"{result['price_basis']} | Datenstand bis {end_dates or 'unbekannt'}"
        optimization_id = storage.save_optimization(params, result, marker)
        return {"optimization_id": optimization_id, **result}
    except ResearchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SourceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/optimizations")
def get_optimizations(limit: int = Query(default=30, ge=1, le=100)) -> dict:
    return {"optimizations": storage.list_optimizations(limit)}


@app.get("/api/optimizations/{optimization_id}")
def get_optimization(optimization_id: str) -> dict:
    optimization = storage.get_optimization(optimization_id)
    if optimization is None:
        raise HTTPException(status_code=404, detail="Diese Optimierung ist nicht gespeichert.")
    return optimization


@app.get("/api/backtests/{run_id}/events")
def get_backtest_events(
    run_id: str,
    cohort: Literal["near", "control"] = "near",
    limit: int = Query(default=50, ge=1, le=250),
    offset: int = Query(default=0, ge=0),
) -> dict:
    if storage.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Dieser Backtest ist nicht gespeichert.")
    return {
        "cohort": cohort,
        "count": storage.event_count(run_id, cohort),
        "offset": offset,
        "events": storage.paged_events(run_id, cohort, limit, offset),
    }


def _safe_csv_cell(value):
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


@app.get("/api/backtests/{run_id}/events.csv")
def download_backtest_events(
    run_id: str,
    cohort: Literal["near", "control", "all"] = "near",
) -> StreamingResponse:
    if storage.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Dieser Backtest ist nicht gespeichert.")
    selected_cohort = None if cohort == "all" else cohort
    headers = [
        "run_id", "cohort", "market_date", "execution_date", "security_id", "ticker", "name",
        "distance_pct", "high_52w", "signal_close", "entry_open",
        "ret_21_gross", "ret_21_net", "ret_63_gross", "ret_63_net",
        "ret_126_gross", "ret_126_net", "ret_252_gross", "ret_252_net",
    ]

    def generate():
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(headers)
        yield "\ufeff" + buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        for event in storage.event_rows(run_id, selected_cohort):
            writer.writerow([_safe_csv_cell(run_id), *(_safe_csv_cell(event[key]) for key in headers[1:])])
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="52w-events-{run_id[:8]}-{cohort}.csv"'},
    )


if WEB_ROOT.is_dir():
    app.mount("/", StaticFiles(directory=WEB_ROOT, html=True), name="web")
