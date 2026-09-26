from __future__ import annotations

import bisect
import math
from typing import Any

from .research import HORIZON_LABELS, ResearchError, run_event_study
from .source import UNIVERSES, session_dates, source_status


OBJECTIVES = {
    "date_balanced_excess_vs_control": "Mehrertrag gegenüber Kontrolle je Signaltermin",
    "mean_net": "mittlere Nettorendite je Ereignis",
    "median_net": "mediane Nettorendite je Ereignis",
    "win_rate": "Anteil positiver Ereignisse",
    "outperforming_date_rate": "Anteil der Signaltermine mit Mehrertrag",
}
MAX_SEARCH_GROUPS = 12
MAX_CANDIDATES = 384


def _period_split(
    calendar: list[str],
    start_date: str,
    validation_start_date: str,
    end_date: str,
    max_horizon: int,
) -> dict[str, str]:
    """Create disjoint signal windows and keep each period's exits inside that period."""
    if not calendar:
        raise ResearchError("Für das gewählte Universum sind keine Handelssitzungen verfügbar.")

    start_index = bisect.bisect_right(calendar, start_date) - 1
    end_index = bisect.bisect_right(calendar, end_date) - 1
    validation_index = bisect.bisect_left(calendar, validation_start_date)
    if start_index < 0 or end_index < 0:
        raise ResearchError("Der gewählte Zeitraum liegt vor der verfügbaren Kursgeschichte.")
    if start_index >= validation_index or validation_index > end_index:
        raise ResearchError(
            "Der Prüfzeitraum muss nach dem Start und spätestens am Ende des Zeitraums beginnen."
        )

    train_end_index = validation_index - max_horizon - 1
    validation_end_index = end_index - max_horizon
    if train_end_index < start_index:
        raise ResearchError(
            "Vor dem Prüfzeitraum bleibt kein Trainingsfenster mit ausreichend Abstand "
            "für die längste Haltedauer."
        )
    if validation_end_index < validation_index:
        raise ResearchError(
            "Der Prüfzeitraum ist kürzer als die längste Haltedauer; wähle ein späteres Ende "
            "oder einen früheren Prüfstart."
        )

    return {
        "training_start_date": calendar[start_index],
        "training_end_date": calendar[train_end_index],
        "validation_start_date": calendar[validation_index],
        "validation_end_date": calendar[validation_end_index],
        "requested_end_date": calendar[end_index],
    }


def _metric_summary(result: dict[str, Any], threshold: float, horizon: int) -> dict[str, Any]:
    threshold_key = next(
        (
            key
            for key in result.get("results", {})
            if math.isclose(float(key), threshold, abs_tol=1e-8)
        ),
        None,
    )
    summary = result.get("results", {}).get(threshold_key, {}).get(str(horizon), {})
    fields = (
        "event_count",
        "signal_date_count",
        "comparison_date_count",
        "mean_net",
        "median_net",
        "win_rate",
        "control_mean_net",
        "date_balanced_excess_vs_control",
        "outperforming_date_rate",
    )
    return {field: summary.get(field) for field in fields}


def optimize_event_studies(connection, params: dict[str, Any]) -> dict[str, Any]:
    universes = list(dict.fromkeys(params["universes"]))
    frequencies = list(dict.fromkeys(params["frequencies"]))
    thresholds = sorted({round(float(value), 2) for value in params["thresholds"]})
    horizons = sorted({int(value) for value in params["horizons"]})
    costs = sorted({round(float(value), 2) for value in params["costs_bps"]})
    control_min = float(params["control_min_pct"])
    control_max = float(params["control_max_pct"])

    if not universes or any(universe not in UNIVERSES for universe in universes):
        raise ResearchError("Wähle mindestens ein bekanntes Indexuniversum.")
    if not frequencies or any(value not in {"daily", "weekly", "monthly"} for value in frequencies):
        raise ResearchError("Erlaubte Scan-Frequenzen sind täglich, wöchentlich und monatlich.")
    if not thresholds or any(value <= 0 or value > 25 for value in thresholds):
        raise ResearchError(
            "Die getesteten Abstände müssen größer als 0 und höchstens 25 Prozent sein."
        )
    if not horizons or any(value not in HORIZON_LABELS for value in horizons):
        raise ResearchError("Erlaubte Haltedauern sind 21, 63, 126 oder 252 Handelssitzungen.")
    if not costs or any(value < 0 or value > 1000 for value in costs):
        raise ResearchError("Kosten je Seite müssen zwischen 0 und 1.000 Basispunkten liegen.")
    if not 0 < control_min < control_max <= 50 or control_min <= max(thresholds):
        raise ResearchError(
            "Das Kontrollband muss gültig sein und außerhalb aller getesteten "
            "Hoch-Abstände liegen."
        )

    group_count = len(universes) * len(frequencies) * len(costs)
    candidate_count = group_count * len(thresholds) * len(horizons)
    if group_count > MAX_SEARCH_GROUPS or candidate_count > MAX_CANDIDATES:
        raise ResearchError(
            f"Die Suche umfasst {group_count} Backtest-Gruppen und {candidate_count} Varianten. "
            f"Begrenzt auf {MAX_SEARCH_GROUPS} Gruppen und {MAX_CANDIDATES} Varianten pro Anfrage."
        )

    objective = str(params["objective"])
    if objective not in OBJECTIVES:
        raise ResearchError("Das Optimierungsziel ist nicht bekannt.")
    minimum_events = int(params["min_validation_events"])
    minimum_dates = int(params["min_validation_dates"])
    top_n = int(params["top_n"])
    if minimum_events < 1 or minimum_dates < 1 or top_n < 1:
        raise ResearchError(
            "Mindest-Stichproben und Anzahl der Spitzenergebnisse müssen positiv sein."
        )

    grouped_candidates: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    warnings: list[str] = []
    periods: dict[str, dict[str, str]] = {}
    source_end_dates: dict[str, str] = {}

    for universe in universes:
        calendar = session_dates(connection, universe)
        period = _period_split(
            calendar,
            str(params["start_date"]),
            str(params["validation_start_date"]),
            str(params["end_date"]),
            max(horizons),
        )
        periods[universe] = period

        for frequency in frequencies:
            for cost_bps in costs:
                group = {
                    "universe": universe,
                    "frequency": frequency,
                    "thresholds": thresholds,
                    "horizons": horizons,
                    "control_min_pct": control_min,
                    "control_max_pct": control_max,
                    "cost_bps": cost_bps,
                }
                try:
                    training_params = {
                        **group,
                        "start_date": period["training_start_date"],
                        "end_date": period["training_end_date"],
                    }
                    validation_params = {
                        **group,
                        "start_date": period["validation_start_date"],
                        "end_date": period["validation_end_date"],
                    }
                    training_result, _ = run_event_study(connection, training_params)
                    validation_result, _ = run_event_study(connection, validation_params)
                except ResearchError as exc:
                    failures.append(
                        {
                            "universe": universe,
                            "frequency": frequency,
                            "cost_bps_per_side": str(cost_bps),
                            "reason": str(exc),
                        }
                    )
                    continue

                source_end_dates[universe] = validation_result["source_end_date"]
                warnings.extend(training_result.get("warnings", []))
                warnings.extend(validation_result.get("warnings", []))
                for threshold in thresholds:
                    for horizon in horizons:
                        training = _metric_summary(training_result, threshold, horizon)
                        validation = _metric_summary(validation_result, threshold, horizon)
                        score = validation.get(objective)
                        reasons = []
                        if int(validation.get("event_count") or 0) < minimum_events:
                            reasons.append(
                                f"weniger als {minimum_events} auswertbare Ereignisse "
                                "im Prüfzeitraum"
                            )
                        date_metric = (
                            "comparison_date_count"
                            if objective
                            in {"date_balanced_excess_vs_control", "outperforming_date_rate"}
                            else "signal_date_count"
                        )
                        actual_dates = int(validation.get(date_metric) or 0)
                        if actual_dates < minimum_dates:
                            date_kind = (
                                "gemeinsame Vergleichstermine"
                                if date_metric == "comparison_date_count"
                                else "Signaltermine"
                            )
                            reasons.append(
                                f"weniger als {minimum_dates} {date_kind} im Prüfzeitraum"
                            )
                        if score is None:
                            reasons.append(
                                "Optimierungskennzahl im Prüfzeitraum nicht berechenbar"
                            )
                        train_score = training.get(objective)
                        grouped_candidates.append(
                            {
                                "universe": universe,
                                "frequency": frequency,
                                "threshold_pct": threshold,
                                "horizon_sessions": horizon,
                                "cost_bps_per_side": cost_bps,
                                "training": training,
                                "validation": validation,
                                "objective": objective,
                                "validation_score": score,
                                "score_change_from_training": (
                                    score - train_score
                                    if score is not None and train_score is not None
                                    else None
                                ),
                                "eligible": not reasons,
                                "exclusion_reasons": reasons,
                            }
                        )

    eligible = [candidate for candidate in grouped_candidates if candidate["eligible"]]
    eligible.sort(
        key=lambda candidate: (
            -float(candidate["validation_score"]),
            -int(candidate["validation"]["signal_date_count"] or 0),
            -int(candidate["validation"]["event_count"] or 0),
            -float(
                candidate["training"][objective]
                if candidate["training"].get(objective) is not None
                else -math.inf
            ),
        )
    )
    for rank, candidate in enumerate(eligible, start=1):
        candidate["rank"] = rank

    status = source_status(connection)
    warnings.extend(status.get("warnings", []))
    warnings.extend(
        [
            "Die Rangliste basiert ausschließlich auf dem zeitlich späteren Prüfzeitraum. "
            "Trainingssignale enden mindestens um die längste Haltedauer vor dessen Beginn.",
            "Die Suche kann historische Daten überanpassen. Spitzenergebnisse sind "
            "beschreibend und kein Nachweis zukünftiger Renditen.",
            "Wiederholte Parametersuchen auf demselben Prüfzeitraum machen diesen "
            "schrittweise zu Trainingsdaten; bestätige die endgültige Auswahl auf "
            "einem unberührten Zeitraum.",
            "Wiederholte Signale, überlappende Haltedauern und die grobe Kontrollgruppe "
            "sind nicht unabhängig oder risikogleich.",
        ]
    )
    unique_warnings = list(dict.fromkeys(warnings))

    return {
        "status": "completed_with_failures" if failures else "completed",
        "objective": objective,
        "objective_label": OBJECTIVES[objective],
        "candidate_count": candidate_count,
        "ranked_candidate_count": len(eligible),
        "excluded_candidate_count": len(grouped_candidates) - len(eligible),
        "failed_group_count": len(failures),
        "minimum_validation_sample": {
            "events": minimum_events,
            "signal_dates": minimum_dates,
        },
        "search": {
            "universes": universes,
            "frequencies": frequencies,
            "thresholds_pct": thresholds,
            "horizons_sessions": horizons,
            "costs_bps_per_side": costs,
            "control_band_pct": {"min": control_min, "max": control_max},
            "start_date": str(params["start_date"]),
            "validation_start_date": str(params["validation_start_date"]),
            "end_date": str(params["end_date"]),
            "embargo_sessions": max(horizons),
            "periods_by_universe": periods,
        },
        "source_end_dates": source_end_dates,
        "top_candidates": eligible[:top_n],
        "excluded_candidates_sample": [
            candidate for candidate in grouped_candidates if not candidate["eligible"]
        ][:20],
        "failed_groups": failures,
        "warnings": unique_warnings,
        "price_basis": status.get("price_adjustment", "unbekannt"),
    }
