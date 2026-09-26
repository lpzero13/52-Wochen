from __future__ import annotations

import bisect
import math
import statistics
from collections import defaultdict, deque
from datetime import date
from typing import Any

from .source import UNIVERSES, SourceError, read_metadata, session_dates, source_status


HORIZON_LABELS = {21: "1 Monat", 63: "3 Monate", 126: "6 Monate", 252: "12 Monate"}
LOOKBACK_SESSIONS = 252
MAX_SAMPLE_DATES = 4000
MAX_STORED_EVENTS = 300_000


class ResearchError(ValueError):
    """Eingaben können auf diesem Datenstand nicht sinnvoll ausgewertet werden."""


def _effective_session(sessions: list[str], requested_date: str | None) -> str:
    if not sessions:
        raise ResearchError("Für das ausgewählte Universum gibt es keine Indexsessions.")
    if not requested_date:
        return sessions[-1]
    index = bisect.bisect_right(sessions, requested_date) - 1
    if index < 0:
        raise ResearchError("Vor dem gewählten Datum gibt es keine Marktsession im Datenstand.")
    return sessions[index]


def _security_record(connection, asset_id: int) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT assetid, current_symbol, security_name, currency, gics_sector, "
        "first_quoted_date, last_quoted_date FROM securities WHERE assetid = ?",
        (asset_id,),
    ).fetchone()
    return dict(row) if row else None


def _price_history(connection, asset_id: int, universe: str, through_date: str, limit: int) -> list:
    return list(
        connection.execute(
            "SELECT assetid, date, open, high, low, close, volume, symbol_at_load_time, security_name "
            "FROM prices WHERE assetid = ? AND date IN ("
            "SELECT date FROM indices WHERE universe = ? AND date <= ? ORDER BY date DESC LIMIT ?"
            ") ORDER BY date DESC",
            (asset_id, universe, through_date, limit),
        ).fetchall()
    )


def _row_number(row, key: str) -> float | None:
    value = row[key]
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _pct_change(later: float | None, earlier: float | None) -> float | None:
    if later is None or earlier is None or earlier <= 0:
        return None
    return later / earlier - 1.0


def _metrics_from_bars(rows: list, universe: str, effective_date: str, threshold_pct: float) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, str]]]:
    metrics: dict[str, Any] = {}
    bars: list[dict[str, Any]] = []
    reasons: list[dict[str, str]] = []
    if len(rows) < LOOKBACK_SESSIONS:
        return metrics, bars, [{"ticker": "—", "reason": f"Nur {len(rows)} Kursbeobachtungen; benötigt werden 252."}]

    all_values = [(_row_number(row, "open"), _row_number(row, "high"), _row_number(row, "low"), _row_number(row, "close")) for row in rows]
    window_values = all_values[-LOOKBACK_SESSIONS:]
    if any(high is None or low is None or close is None or close <= 0 for _, high, low, close in window_values):
        return metrics, bars, [{"ticker": "—", "reason": "Mindestens ein erforderlicher OHLC-Wert fehlt oder ist ungültig."}]

    current = window_values[-1][3]
    high_52w = max(float(value[1]) for value in window_values if value[1] is not None)
    low_52w = min(float(value[2]) for value in window_values if value[2] is not None)
    distance = _pct_change(current, high_52w)
    if distance is None:
        return metrics, bars, [{"ticker": "—", "reason": "52-Wochen-Abstand ist nicht berechenbar."}]

    return_by_sessions: dict[int, float | None] = {}
    for lookback in (21, 63, 126, 252):
        prior_index = len(all_values) - 1 - lookback
        prior = all_values[prior_index][3] if prior_index >= 0 else None
        return_by_sessions[lookback] = _pct_change(current, prior)

    metrics = {
        "current_price": current,
        "high_52w": high_52w,
        "low_52w": low_52w,
        "distance_to_high_pct": distance * 100.0,
        "distance_to_low_pct": _pct_change(current, low_52w) * 100.0 if low_52w else None,
        "return_1m": return_by_sessions[21],
        "return_3m": return_by_sessions[63],
        "return_6m": return_by_sessions[126],
        "return_12m": return_by_sessions[252],
        "near_high": distance >= -(threshold_pct / 100.0),
        "history_sessions": LOOKBACK_SESSIONS,
        "as_of_date": effective_date,
        "universe": universe,
    }
    bars = [
        {
            "date": str(row["date"]),
            "open": _row_number(row, "open"),
            "high": _row_number(row, "high"),
            "low": _row_number(row, "low"),
            "close": _row_number(row, "close"),
        }
        for row in rows[-LOOKBACK_SESSIONS:]
    ]
    return metrics, bars, reasons


def screener(connection, universe: str, requested_date: str | None, threshold_pct: float = 5.0) -> dict[str, Any]:
    if universe not in UNIVERSES:
        raise ResearchError("Unbekanntes Universum.")
    if not 0 <= threshold_pct <= 25:
        raise ResearchError("Der Abstand zum Hoch muss zwischen 0 und 25 Prozent liegen.")

    sessions = session_dates(connection, universe)
    effective_date = _effective_session(sessions, requested_date)
    effective_index = bisect.bisect_left(sessions, effective_date)
    window_sessions = sessions[max(0, effective_index - LOOKBACK_SESSIONS + 1) : effective_index + 1]
    members = connection.execute(
        "SELECT assetid, symbol_at_load_time, security_name FROM index_membership "
        "WHERE universe = ? AND date = ? AND is_member = 1 ORDER BY assetid",
        (universe, effective_date),
    ).fetchall()
    if not members:
        raise ResearchError(
            f"Für {UNIVERSES[universe]['label']} sind am {effective_date} keine historischen Mitglieder markiert."
        )

    rows_out: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for member in members:
        asset_id = int(member["assetid"])
        history = list(reversed(_price_history(connection, asset_id, universe, effective_date, LOOKBACK_SESSIONS + 1)))
        record = _security_record(connection, asset_id)
        ticker = (
            member["symbol_at_load_time"]
            or (history[-1]["symbol_at_load_time"] if history else None)
            or (record["current_symbol"] if record else None)
            or f"Asset {asset_id}"
        )
        history_dates = {str(row["date"]) for row in history}
        missing_window_dates = len(set(window_sessions) - history_dates)
        if len(window_sessions) < LOOKBACK_SESSIONS:
            excluded.append({"ticker": str(ticker), "reason": f"Nur {len(window_sessions)} Indexsessions bis zum Stichtag; benötigt werden 252."})
            continue
        if missing_window_dates:
            excluded.append({"ticker": str(ticker), "reason": f"Kursdaten für {missing_window_dates} der letzten 252 Indexsessions fehlen."})
            continue

        metrics, _, bar_issues = _metrics_from_bars(history, universe, effective_date, threshold_pct)
        if not metrics:
            for issue in bar_issues:
                excluded.append({"ticker": str(ticker), "reason": issue["reason"]})
            continue

        latest = history[-1]
        sector = record.get("gics_sector") if record else None
        rows_out.append(
            {
                "security_id": asset_id,
                "ticker": str(ticker),
                "name": str(member["security_name"] or latest["security_name"] or (record["security_name"] if record else "Unbekannt")),
                "sector": sector or "—",
                "currency": (record.get("currency") if record else None) or "USD",
                "volume": _row_number(latest, "volume"),
                **metrics,
            }
        )

    rows_out.sort(key=lambda row: (-row["distance_to_high_pct"], row["ticker"]))
    count_near = sum(1 for row in rows_out if row["near_high"])
    warnings = list(source_status(connection)["warnings"])
    if excluded:
        warnings.append(
            f"{len(excluded)} markierte Mitglieder wurden wegen fehlender 252-Session-Historie "
            "oder ungültiger Kurse nicht in die Rangliste aufgenommen."
        )
    return {
        "universe": universe,
        "universe_label": str(UNIVERSES[universe]["label"]),
        "requested_date": requested_date,
        "as_of_date": effective_date,
        "source_end_date": sessions[-1],
        "threshold_pct": threshold_pct,
        "member_count": len(members),
        "eligible_count": len(rows_out),
        "near_count": count_near,
        "excluded_count": len(excluded),
        "rows": rows_out,
        "excluded": excluded[:100],
        "warnings": warnings,
        "methodology": "52-Wochen-Hoch = höchster Total-Return-OHLC-Hochwert aus den letzten 252 Sessions einschließlich Stichtag.",
    }


def security_detail(connection, universe: str, asset_id: int, requested_date: str | None, threshold_pct: float = 5.0) -> dict[str, Any]:
    if universe not in UNIVERSES:
        raise ResearchError("Unbekanntes Universum.")
    sessions = session_dates(connection, universe)
    effective_date = _effective_session(sessions, requested_date)
    effective_index = bisect.bisect_left(sessions, effective_date)
    window_sessions = sessions[max(0, effective_index - LOOKBACK_SESSIONS + 1) : effective_index + 1]
    member = connection.execute(
        "SELECT symbol_at_load_time, security_name FROM index_membership "
        "WHERE universe = ? AND assetid = ? AND date = ? AND is_member = 1",
        (universe, asset_id, effective_date),
    ).fetchone()
    if not member:
        raise ResearchError("Dieses Wertpapier war am gewählten Stichtag nicht als Mitglied des Universums markiert.")
    history = list(reversed(_price_history(connection, asset_id, universe, effective_date, LOOKBACK_SESSIONS + 1)))
    if len(window_sessions) < LOOKBACK_SESSIONS:
        raise ResearchError("Bis zu diesem Stichtag liegen noch keine 252 Indexsessions vor.")
    history_dates = {str(row["date"]) for row in history}
    missing_window_dates = len(set(window_sessions) - history_dates)
    if missing_window_dates:
        raise ResearchError(f"Für {missing_window_dates} der letzten 252 Indexsessions fehlen Kursdaten.")
    metrics, bars, issues = _metrics_from_bars(history, universe, effective_date, threshold_pct)
    if not metrics:
        raise ResearchError(issues[0]["reason"] if issues else "Die Kursreihe ist unvollständig.")
    record = _security_record(connection, asset_id)
    return {
        "security_id": asset_id,
        "ticker": member["symbol_at_load_time"] or (record["current_symbol"] if record else f"Asset {asset_id}"),
        "name": member["security_name"] or (record["security_name"] if record else "Unbekannt"),
        "sector": (record.get("gics_sector") if record else None) or "—",
        "currency": (record.get("currency") if record else None) or "USD",
        "metrics": metrics,
        "bars": bars,
        "source_end_date": sessions[-1],
        "warnings": source_status(connection)["warnings"],
    }


def _sample_session_indices(calendar: list[str], start: int, end: int, frequency: str) -> list[int]:
    if frequency not in {"daily", "weekly", "monthly"}:
        raise ResearchError("Die Scan-Frequenz muss täglich, wöchentlich oder monatlich sein.")
    if frequency == "daily":
        return list(range(start, end + 1))
    selected: dict[tuple[int, int], int] = {}
    for index in range(start, end + 1):
        session = date.fromisoformat(calendar[index])
        key = (session.isocalendar().year, session.isocalendar().week) if frequency == "weekly" else (session.year, session.month)
        selected[key] = index
    return list(selected.values())


def _thresholds(raw: list[float]) -> list[float]:
    if not raw or len(raw) > 8:
        raise ResearchError("Bitte wähle mindestens einen und höchstens acht Hoch-Abstände aus.")
    unique = sorted({round(float(value), 2) for value in raw})
    if any(value <= 0 or value > 25 for value in unique):
        raise ResearchError("Die getesteten Abstände müssen größer als 0 und höchstens 25 Prozent sein.")
    return unique


def _event_summary(events: list[dict[str, Any]], horizon: int) -> dict[str, Any]:
    usable = [event for event in events if event["returns"].get(str(horizon)) is not None]
    if not usable:
        return {
            "event_count": 0,
            "signal_date_count": 0,
            "mean_gross": None,
            "median_gross": None,
            "mean_net": None,
            "median_net": None,
            "win_rate": None,
            "date_mean_net": None,
            "comparison_date_count": 0,
            "outperforming_date_rate": None,
        }
    gross = [float(event["returns"][str(horizon)]["gross"]) for event in usable]
    net = [float(event["returns"][str(horizon)]["net"]) for event in usable]
    by_date: dict[str, list[float]] = defaultdict(list)
    for event in usable:
        by_date[str(event["market_date"])].append(float(event["returns"][str(horizon)]["net"]))
    daily_means = [statistics.fmean(values) for values in by_date.values()]
    return {
        "event_count": len(usable),
        "signal_date_count": len(by_date),
        "mean_gross": statistics.fmean(gross),
        "median_gross": statistics.median(gross),
        "mean_net": statistics.fmean(net),
        "median_net": statistics.median(net),
        "win_rate": sum(value > 0 for value in net) / len(net),
        "date_mean_net": statistics.fmean(daily_means),
        "outperforming_date_rate": None,
    }


def _compare_by_date(
    near: list[dict[str, Any]], control: list[dict[str, Any]], horizon: int
) -> tuple[float | None, float | None, int]:
    near_by_date: dict[str, list[float]] = defaultdict(list)
    control_by_date: dict[str, list[float]] = defaultdict(list)
    for event in near:
        outcome = event["returns"].get(str(horizon))
        if outcome is not None:
            near_by_date[str(event["market_date"])].append(float(outcome["net"]))
    for event in control:
        outcome = event["returns"].get(str(horizon))
        if outcome is not None:
            control_by_date[str(event["market_date"])].append(float(outcome["net"]))
    common_dates = sorted(set(near_by_date).intersection(control_by_date))
    if not common_dates:
        return None, None, 0
    diffs = [statistics.fmean(near_by_date[d]) - statistics.fmean(control_by_date[d]) for d in common_dates]
    return statistics.fmean(diffs), sum(diff > 0 for diff in diffs) / len(diffs), len(common_dates)


def _select_summary_events(events: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    return [event for event in events if event["cohort"] == "near" and event["away_pct"] <= threshold + 1e-9]


def run_event_study(connection, params: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    universe = str(params["universe"])
    if universe not in UNIVERSES:
        raise ResearchError("Unbekanntes Universum.")
    thresholds = _thresholds(params.get("thresholds", [1, 3, 5, 10]))
    horizons = sorted({int(value) for value in params.get("horizons", [21, 63, 126, 252])})
    if not horizons or any(value not in HORIZON_LABELS for value in horizons):
        raise ResearchError("Erlaubte Haltedauern sind 21, 63, 126 oder 252 Handelssitzungen.")
    control_min = float(params.get("control_min_pct", 15))
    control_max = float(params.get("control_max_pct", 25))
    if not 0 < control_min < control_max <= 50:
        raise ResearchError("Die Vergleichsgruppe benötigt einen gültigen Abstand zwischen 0 und 50 Prozent.")
    if control_min <= max(thresholds):
        raise ResearchError("Der Beginn der Vergleichsgruppe muss über dem größten getesteten Hoch-Abstand liegen.")
    frequency = str(params.get("frequency", "monthly"))
    cost_bps = float(params.get("cost_bps", 25))
    if not 0 <= cost_bps <= 1000:
        raise ResearchError("Kosten je Seite müssen zwischen 0 und 1.000 Basispunkten liegen.")

    calendar = session_dates(connection, universe)
    if not calendar:
        raise ResearchError("Für das ausgewählte Universum sind keine Handelssitzungen verfügbar.")
    requested_start = str(params["start_date"])
    requested_end = str(params["end_date"])
    start_index = bisect.bisect_right(calendar, requested_start) - 1
    end_index = bisect.bisect_right(calendar, requested_end) - 1
    if start_index < 0 or end_index < 0:
        raise ResearchError("Der ausgewählte Zeitraum liegt vor der verfügbaren Kursgeschichte.")
    if start_index > end_index:
        raise ResearchError("Der Start des Backtests liegt nach seinem Ende.")
    sample_indices = _sample_session_indices(calendar, start_index, end_index, frequency)
    if len(sample_indices) > MAX_SAMPLE_DATES:
        raise ResearchError(
            f"Der Zeitraum enthält {len(sample_indices):,} Signaltermine. "
            f"Diese Studie ist auf {MAX_SAMPLE_DATES:,} begrenzt; wähle eine längere Scan-Frequenz oder einen kürzeren Zeitraum."
        )
    if control_max <= max(thresholds):
        raise ResearchError("Die Vergleichsgruppe muss außerhalb der getesteten Nähe zum Hoch liegen.")

    sample_dates = [calendar[index] for index in sample_indices]
    members_by_date: dict[str, dict[int, tuple[str | None, str | None]]] = {}
    for offset in range(0, len(sample_dates), 400):
        chunk = sample_dates[offset : offset + 400]
        placeholders = ",".join("?" for _ in chunk)
        sql = (
            "SELECT date, assetid, symbol_at_load_time, security_name FROM index_membership "
            f"WHERE universe = ? AND is_member = 1 AND date IN ({placeholders})"
        )
        for row in connection.execute(sql, (universe, *chunk)):
            members_by_date.setdefault(str(row["date"]), {})[int(row["assetid"])] = (
                row["symbol_at_load_time"], row["security_name"]
            )
    asset_ids = sorted({asset_id for members in members_by_date.values() for asset_id in members})
    if not asset_ids:
        raise ResearchError("Für die ausgewählten Signaltermine sind keine historischen Mitglieder vorhanden.")

    first_history_index = max(0, start_index - (LOOKBACK_SESSIONS - 1))
    max_horizon = max(horizons)
    history_end_index = min(len(calendar) - 1, end_index + max_horizon)
    history_start_date = calendar[first_history_index]
    history_end_date = calendar[history_end_index]
    sample_index_set = set(sample_indices)
    cost_rate = cost_bps / 10_000.0
    max_threshold = max(thresholds)
    events: list[dict[str, Any]] = []
    counters = {
        "point_in_time_members": sum(len(members) for members in members_by_date.values()),
        "insufficient_lookback": 0,
        "missing_signal_price": 0,
        "missing_next_open": 0,
        "included_near": 0,
        "included_control": 0,
        "unavailable_forward_horizons": {str(horizon): 0 for horizon in horizons},
    }

    for asset_id in asset_ids:
        price_rows = connection.execute(
            "SELECT date, open, high, low, close, symbol_at_load_time, security_name "
            "FROM prices WHERE assetid = ? AND date BETWEEN ? AND ? ORDER BY date",
            (asset_id, history_start_date, history_end_date),
        ).fetchall()
        price_by_date = {str(row["date"]): row for row in price_rows}
        if not price_by_date:
            continue
        record = _security_record(connection, asset_id)
        queue: deque[tuple[int, float]] = deque()
        missing_prefix = [0] * (history_end_index + 2)
        for position in range(first_history_index, end_index + 1):
            market_date = calendar[position]
            price = price_by_date.get(market_date)
            open_value = _row_number(price, "open") if price is not None else None
            high_value = _row_number(price, "high") if price is not None else None
            low_value = _row_number(price, "low") if price is not None else None
            close_value = _row_number(price, "close") if price is not None else None
            invalid = int(
                open_value is None or high_value is None or low_value is None or close_value is None
                or open_value <= 0 or high_value <= 0 or low_value <= 0 or close_value <= 0
            )
            missing_prefix[position + 1] = missing_prefix[position] + invalid
            if high_value is not None and high_value > 0:
                while queue and queue[-1][1] <= high_value:
                    queue.pop()
                queue.append((position, high_value))
            lower = position - (LOOKBACK_SESSIONS - 1)
            while queue and queue[0][0] < lower:
                queue.popleft()

            if position not in sample_index_set:
                continue
            member = members_by_date.get(market_date, {}).get(asset_id)
            if member is None:
                continue
            if position < LOOKBACK_SESSIONS - 1:
                counters["insufficient_lookback"] += 1
                continue
            if missing_prefix[position + 1] - missing_prefix[position - LOOKBACK_SESSIONS + 1] > 0:
                counters["insufficient_lookback"] += 1
                continue
            if price is None or not queue or close_value is None or close_value <= 0:
                counters["missing_signal_price"] += 1
                continue

            high_52w = queue[0][1]
            distance_pct = (close_value / high_52w - 1.0) * 100.0
            away_pct = max(0.0, -distance_pct)
            cohort = "near" if away_pct <= max_threshold else (
                "control" if control_min <= away_pct <= control_max else None
            )
            if cohort is None:
                continue
            if position + 1 >= len(calendar):
                counters["missing_next_open"] += 1
                continue
            entry_date = calendar[position + 1]
            entry_row = price_by_date.get(entry_date)
            entry_open = _row_number(entry_row, "open") if entry_row is not None else None
            if entry_open is None or entry_open <= 0:
                counters["missing_next_open"] += 1
                continue

            ticker = (
                member[0]
                or price["symbol_at_load_time"]
                or (record["current_symbol"] if record else None)
                or f"Asset {asset_id}"
            )
            name = (
                member[1]
                or price["security_name"]
                or (record["security_name"] if record else None)
                or "Unbekannt"
            )
            returns: dict[str, dict[str, float] | None] = {}
            for horizon in horizons:
                exit_index = position + horizon
                exit_row = price_by_date.get(calendar[exit_index]) if exit_index < len(calendar) else None
                exit_close = _row_number(exit_row, "close") if exit_row is not None else None
                if exit_close is None or exit_close <= 0:
                    returns[str(horizon)] = None
                    counters["unavailable_forward_horizons"][str(horizon)] += 1
                    continue
                gross_return = exit_close / entry_open - 1.0
                net_return = (exit_close / entry_open) * ((1.0 - cost_rate) / (1.0 + cost_rate)) - 1.0
                returns[str(horizon)] = {"gross": gross_return, "net": net_return}

            events.append(
                {
                    "cohort": cohort,
                    "market_date": market_date,
                    "execution_date": entry_date,
                    "security_id": asset_id,
                    "ticker": str(ticker),
                    "name": str(name),
                    "distance_pct": distance_pct,
                    "away_pct": away_pct,
                    "high_52w": high_52w,
                    "signal_close": close_value,
                    "entry_open": entry_open,
                    "returns": returns,
                }
            )
            if cohort == "near":
                counters["included_near"] += 1
            else:
                counters["included_control"] += 1
            if len(events) > MAX_STORED_EVENTS:
                raise ResearchError(
                    f"Die Studie erzeugt mehr als {MAX_STORED_EVENTS:,} Ereignisse. "
                    "Wähle Monatsfrequenz, einen kürzeren Zeitraum oder engere Abstandsgruppen."
                )

    control_events = [event for event in events if event["cohort"] == "control"]
    summaries: dict[str, dict[str, Any]] = {}
    control_summaries: dict[str, dict[str, Any]] = {}
    for horizon in horizons:
        control_summaries[str(horizon)] = _event_summary(control_events, horizon)
    for threshold in thresholds:
        near = _select_summary_events(events, threshold)
        threshold_result: dict[str, Any] = {}
        for horizon in horizons:
            summary = _event_summary(near, horizon)
            date_excess, outperforming_dates, comparison_date_count = _compare_by_date(
                near, control_events, horizon
            )
            summary["control_mean_net"] = control_summaries[str(horizon)]["mean_net"]
            summary["date_balanced_excess_vs_control"] = date_excess
            summary["comparison_date_count"] = comparison_date_count
            summary["outperforming_date_rate"] = outperforming_dates
            threshold_result[str(horizon)] = summary
        summaries[str(threshold)] = threshold_result

    status = source_status(connection)
    warnings = list(status["warnings"])
    warnings.extend(
        [
            "Point-in-Time bezieht sich auf die in dieser Norgate-Datei gespeicherten historischen Indexmitgliedschaften; frühere Datenrevisionen sind nicht archiviert.",
            "Die Vergleichsgruppe besteht aus Mitgliedern derselben Indexsessions mit 15–25 % Abstand zum 52-Wochen-Hoch. Sie ist keine risikogleiche Kontrollgruppe.",
            "Überlappende Ereignisse und wiederholte Signale derselben Aktie sind nicht unabhängig; die Kennzahlen sind deskriptiv und kein Out-of-Sample-Nachweis.",
            "Die Total-Return-OHLC-Simulation ist eine synthetische Referenz in USD. Reale Stücke, Steuern, Slippage und Dividendenzahlungen werden nicht rekonstruiert.",
        ]
    )
    if frequency != "monthly":
        warnings.append("Bei täglicher oder wöchentlicher Frequenz überlappen besonders viele Signale und Haltedauern.")

    result = {
        "universe": universe,
        "universe_label": str(UNIVERSES[universe]["label"]),
        "requested_start_date": requested_start,
        "requested_end_date": requested_end,
        "effective_start_date": calendar[start_index],
        "effective_end_date": calendar[end_index],
        "source_end_date": calendar[-1],
        "frequency": frequency,
        "thresholds": thresholds,
        "horizons": horizons,
        "horizon_labels": {str(value): HORIZON_LABELS[value] for value in horizons},
        "control_band": {"min_pct": control_min, "max_pct": control_max},
        "cost_bps_per_side": cost_bps,
        "signal_date_count": len(sample_indices),
        "event_count": len(events),
        "results": summaries,
        "control_results": control_summaries,
        "quality": counters,
        "warnings": warnings,
        "price_basis": status.get("price_adjustment", "unbekannt"),
        "methodology_version": "52w-event-study-v1",
    }
    return result, events
