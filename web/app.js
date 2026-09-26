const state = {
  source: null,
  screener: null,
  selectedRunId: null,
  activeRun: null,
};

const universeNames = { sp500: "S&P 500", nasdaq100: "Nasdaq 100", dow: "Dow Jones 30" };
const horizonNames = { 21: "21 Sitzungen · ~1 Monat", 63: "63 Sitzungen · ~3 Monate", 126: "126 Sitzungen · ~6 Monate", 252: "252 Sitzungen · ~12 Monate" };
const numberFormat = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 0 });
const pctFormat = new Intl.NumberFormat("de-DE", { minimumFractionDigits: 1, maximumFractionDigits: 1 });

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function formatPct(value, digits = 1) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const formatted = new Intl.NumberFormat("de-DE", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(Number(value));
  return `${Number(value) > 0 ? "+" : ""}${formatted} %`;
}

function formatFraction(value, digits = 1) {
  return value === null || value === undefined ? "—" : formatPct(Number(value) * 100, digits);
}

function formatNumber(value, digits = 0) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  return new Intl.NumberFormat("de-DE", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(Number(value));
}

function formatMoney(value, currency = "USD") {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency, maximumFractionDigits: 2 }).format(Number(value));
  } catch {
    return `${formatNumber(value, 2)} ${currency}`;
  }
}

function formatDate(value, style = "medium") {
  if (!value) return "—";
  const date = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("de-DE", { dateStyle: style, timeZone: "UTC" }).format(date);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = payload && typeof payload === "object" ? payload.detail : payload;
    throw new Error(Array.isArray(detail) ? detail.map((entry) => entry.msg).join(" · ") : (detail || `Anfrage fehlgeschlagen (${response.status})`));
  }
  return payload;
}

function setAlert(message, isError = false) {
  const element = document.querySelector("#source-alert");
  if (!message) {
    element.classList.add("hidden");
    element.classList.remove("error");
    element.textContent = "";
    return;
  }
  element.classList.remove("hidden");
  element.classList.toggle("error", isError);
  element.textContent = message;
}

function showToast(message) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.classList.remove("hidden");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.add("hidden"), 3200);
}

function selectedUniverseDates(universe) {
  return state.source?.universes?.find((item) => item.code === universe);
}

function setDefaultDateInputs() {
  if (!state.source) return;
  const universe = document.querySelector("#universe-select").value;
  const sourceDates = selectedUniverseDates(universe);
  const latest = sourceDates?.end_date || state.source.end_date;
  const earliest = sourceDates?.start_date || state.source.start_date;
  const screenDate = document.querySelector("#as-of-date");
  screenDate.min = earliest || "";
  screenDate.max = latest || "";
  if (!screenDate.value || screenDate.value > latest) screenDate.value = latest || "";

  const endInput = document.querySelector("#bt-end-date");
  const startInput = document.querySelector("#bt-start-date");
  endInput.max = latest || "";
  startInput.min = earliest || "";
  startInput.max = latest || "";
  if (!endInput.value || endInput.value > latest) endInput.value = latest || "";
  if (!startInput.value && latest) {
    const end = new Date(`${latest}T00:00:00Z`);
    end.setUTCFullYear(end.getUTCFullYear() - 10);
    const proposed = end.toISOString().slice(0, 10);
    startInput.value = proposed < (earliest || proposed) ? (earliest || proposed) : proposed;
  }
  if (startInput.value && endInput.value && startInput.value > endInput.value) startInput.value = endInput.value;
}

async function loadStatus() {
  const indicator = document.querySelector("#data-indicator");
  try {
    const source = await api("/api/status");
    state.source = source;
    indicator.classList.remove("error");
    indicator.classList.toggle("limited", source.quality_status !== "available");
    indicator.querySelector("span").textContent = source.quality_status === "available" ? `Daten bis ${formatDate(source.end_date)}` : `Daten eingeschränkt · bis ${formatDate(source.end_date)}`;
    document.querySelector("#source-file-label").textContent = `Datenquelle: ${source.database_path} · Anpassung: ${source.price_adjustment} · Export vollständig: ${source.manifest_complete ? "ja" : "nein / unbekannt"}`;
    if (source.warnings?.length) {
      setAlert(source.warnings.join(" "));
    } else {
      setAlert("");
    }
    setDefaultDateInputs();
    return source;
  } catch (error) {
    indicator.classList.add("error");
    indicator.querySelector("span").textContent = "Norgate-Datenquelle nicht erreichbar";
    document.querySelector("#source-file-label").textContent = "Datenquelle konnte nicht gelesen werden.";
    setAlert(error.message, true);
    return null;
  }
}

async function loadScreener() {
  const button = document.querySelector("#screener-form button[type=submit]");
  const tbody = document.querySelector("#screener-rows");
  button.disabled = true;
  tbody.innerHTML = '<tr><td colspan="9" class="empty-state"><span class="loader"></span> 252 Handelssitzungen werden ausgewertet …</td></tr>';
  try {
    const params = new URLSearchParams({
      universe: document.querySelector("#universe-select").value,
      as_of_date: document.querySelector("#as-of-date").value,
      threshold_pct: document.querySelector("#threshold-select").value,
    });
    state.screener = await api(`/api/screener?${params}`);
    renderScreener();
    const requested = state.screener.requested_date;
    const effective = state.screener.as_of_date;
    const note = document.querySelector("#effective-date-note");
    if (requested && requested !== effective) {
      note.textContent = `Für den gewählten Stichtag ${formatDate(requested)} gab es keine Indexsession. Angezeigt wird die letzte vorherige Session: ${formatDate(effective)}.`;
      note.classList.remove("hidden");
    } else {
      note.classList.add("hidden");
    }
    document.querySelector("#snapshot-stamp").textContent = formatDate(effective);
    if (state.screener.warnings?.length) setAlert(state.screener.warnings.join(" "));
  } catch (error) {
    tbody.innerHTML = `<tr><td colspan="9" class="empty-state">${esc(error.message)}</td></tr>`;
    ["#stat-members", "#stat-eligible", "#stat-near", "#stat-excluded"].forEach((selector) => { document.querySelector(selector).textContent = "—"; });
    document.querySelector("#table-count").textContent = "Keine Daten verfügbar";
    setAlert(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function visibleScreenerRows() {
  if (!state.screener) return [];
  const term = document.querySelector("#ticker-search").value.trim().toLocaleLowerCase("de-DE");
  const nearOnly = document.querySelector("#near-only").checked;
  return state.screener.rows.filter((row) => {
    const matchesSearch = !term || `${row.ticker} ${row.name}`.toLocaleLowerCase("de-DE").includes(term);
    return matchesSearch && (!nearOnly || row.near_high);
  });
}

function renderScreener() {
  const result = state.screener;
  if (!result) return;
  document.querySelector("#stat-members").textContent = numberFormat.format(result.member_count);
  document.querySelector("#stat-eligible").textContent = numberFormat.format(result.eligible_count);
  document.querySelector("#stat-near").textContent = numberFormat.format(result.near_count);
  document.querySelector("#stat-excluded").textContent = numberFormat.format(result.excluded_count);
  const rows = visibleScreenerRows();
  const tbody = document.querySelector("#screener-rows");
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="empty-state">Keine Aktien passen zu diesen Filtern.</td></tr>';
  } else {
    tbody.innerHTML = rows.map((row) => {
      const initials = esc(String(row.ticker).slice(0, 3));
      const dist = Number(row.distance_to_high_pct);
      const currency = esc(row.currency || "USD");
      const distanceLabel = dist > -0.05 ? "Am Hoch" : `${pctFormat.format(Math.abs(dist))} %`;
      return `<tr data-asset-id="${Number(row.security_id)}" class="${row.near_high ? "near-row" : ""}" tabindex="0" aria-label="${esc(row.ticker)} Details öffnen">
        <td><div class="security-cell"><span class="security-logo">${initials}</span><span class="security-info"><strong>${esc(row.ticker)}</strong><small>${esc(row.name)}</small></span></div></td>
        <td class="num">${formatMoney(row.current_price, currency)}</td><td class="num">${formatMoney(row.high_52w, currency)}</td>
        <td class="num"><span class="distance-pill ${row.near_high ? "near" : ""}">${distanceLabel}</span></td>
        <td class="num">${formatMoney(row.low_52w, currency)}</td>
        <td class="num ${Number(row.return_3m) >= 0 ? "positive" : "negative"}">${formatFraction(row.return_3m)}</td>
        <td class="num ${Number(row.return_6m) >= 0 ? "positive" : "negative"}">${formatFraction(row.return_6m)}</td>
        <td class="num ${Number(row.return_12m) >= 0 ? "positive" : "negative"}">${formatFraction(row.return_12m)}</td><td class="row-arrow">↗</td>
      </tr>`;
    }).join("");
  }
  document.querySelector("#table-count").textContent = `${numberFormat.format(rows.length)} angezeigt · ${numberFormat.format(result.eligible_count)} auswertbar`;
}

function changeView(view) {
  if (!document.querySelector(`#view-${view}`)) return;
  document.querySelectorAll(".view").forEach((section) => section.classList.toggle("active", section.id === `view-${view}`));
  document.querySelectorAll(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  document.querySelectorAll(".mobile-nav [data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  const names = { screener: "SCREENER", backtests: "BACKTESTS", methodik: "METHODIK & DATEN" };
  document.querySelector("#crumb-current").textContent = names[view] || "SCREENER";
  if (window.location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
  if (view === "backtests") loadHistory();
}

function makeChart(bars, high52) {
  const width = 540;
  const height = 220;
  const left = 42;
  const right = 8;
  const top = 13;
  const bottom = 28;
  const usableW = width - left - right;
  const usableH = height - top - bottom;
  const lows = bars.map((bar) => Number(bar.low)).filter(Number.isFinite);
  const highs = bars.map((bar) => Number(bar.high)).filter(Number.isFinite);
  const min = Math.min(...lows);
  const max = Math.max(high52, ...highs);
  const range = max - min || 1;
  const y = (value) => top + ((max - value) / range) * usableH;
  const x = (index) => left + (index / Math.max(1, bars.length - 1)) * usableW;
  const closeLine = bars.map((bar, index) => `${x(index).toFixed(1)},${y(Number(bar.close)).toFixed(1)}`).join(" ");
  const lineY = y(high52);
  const yTicks = [0, 1, 2, 3].map((step) => {
    const value = max - range * (step / 3);
    const pos = top + usableH * (step / 3);
    return `<line x1="${left}" x2="${width-right}" y1="${pos}" y2="${pos}" stroke="#eef1eb"/><text x="${left-7}" y="${pos+3}" fill="#9ba49d" font-size="8" text-anchor="end">${formatNumber(value, 0)}</text>`;
  }).join("");
  const area = `${left},${top+usableH} ${closeLine} ${width-right},${top+usableH}`;
  return `<svg class="chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="252 Sitzungen Schlusskursverlauf mit 52-Wochen-Hoch">
    ${yTicks}<line x1="${left}" x2="${width-right}" y1="${lineY}" y2="${lineY}" stroke="#cf9962" stroke-width="1.5" stroke-dasharray="5 4"/>
    <polygon points="${area}" fill="rgba(149,184,110,.1)"/><polyline points="${closeLine}" fill="none" stroke="#547c45" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    <text x="${width-right}" y="${Math.max(10,lineY-5)}" fill="#ae7c49" font-size="8" text-anchor="end">52W-Hoch</text>
    <text x="${left}" y="${height-7}" fill="#9ba49d" font-size="8">${esc(formatDate(bars[0]?.date, "short"))}</text><text x="${width-right}" y="${height-7}" fill="#9ba49d" font-size="8" text-anchor="end">${esc(formatDate(bars.at(-1)?.date, "short"))}</text>
  </svg>`;
}

async function openDetail(assetId) {
  const backdrop = document.querySelector("#detail-backdrop");
  const container = document.querySelector("#detail-content");
  const result = state.screener;
  if (!result) return;
  backdrop.classList.remove("hidden");
  document.body.style.overflow = "hidden";
  container.className = "drawer-loading";
  container.innerHTML = '<span class="loader"></span> Kursverlauf wird geladen …';
  try {
    const params = new URLSearchParams({ universe: result.universe, as_of_date: result.as_of_date, threshold_pct: String(result.threshold_pct) });
    const detail = await api(`/api/securities/${assetId}?${params}`);
    container.className = "";
    const metrics = detail.metrics;
    const distance = Number(metrics.distance_to_high_pct);
    container.innerHTML = `<div class="detail-top"><div class="eyebrow"><span class="eyebrow-line"></span> HISTORISCHES WERTPAPIERPROFIL</div>
      <div class="detail-ticker" style="margin-top:13px"><span class="security-logo">${esc(String(detail.ticker).slice(0,3))}</span><div><h2 id="detail-title">${esc(detail.ticker)}</h2><p>${esc(detail.name)} · ${esc(detail.sector || "—")}</p></div></div>
      <div class="detail-price"><div><small>SCHLUSSKURS · ${esc(formatDate(metrics.as_of_date))}</small><strong>${formatMoney(metrics.current_price, detail.currency)}</strong></div><div class="detail-distance"><small>ABSTAND ZUM HOCH</small><b>${distance > -0.05 ? "Am Hoch" : `${pctFormat.format(Math.abs(distance))} %`}</b></div></div>
      </div>
      <div class="chart-card"><div class="chart-head"><strong>252-Sitzungen-Kursverlauf</strong><span>Schlusskurs · gestrichelt = Hoch</span></div>${makeChart(detail.bars, metrics.high_52w)}</div>
      <div class="detail-metric-grid"><div class="detail-metric"><small>52W-HOCH</small><strong>${formatMoney(metrics.high_52w, detail.currency)}</strong></div><div class="detail-metric"><small>52W-TIEF</small><strong>${formatMoney(metrics.low_52w, detail.currency)}</strong></div><div class="detail-metric"><small>ABSTAND ZUM TIEF</small><strong>${formatPct(metrics.distance_to_low_pct)}</strong></div><div class="detail-metric"><small>3 MONATE</small><strong class="${metrics.return_3m >= 0 ? "positive" : "negative"}">${formatFraction(metrics.return_3m)}</strong></div><div class="detail-metric"><small>6 MONATE</small><strong class="${metrics.return_6m >= 0 ? "positive" : "negative"}">${formatFraction(metrics.return_6m)}</strong></div><div class="detail-metric"><small>12 MONATE</small><strong class="${metrics.return_12m >= 0 ? "positive" : "negative"}">${formatFraction(metrics.return_12m)}</strong></div></div>
      <div class="detail-warnings">${(detail.warnings || []).map((warning) => `<div>${esc(warning)}</div>`).join("")}</div>`;
  } catch (error) {
    container.className = "drawer-loading";
    container.innerHTML = `<div>${esc(error.message)}</div>`;
  }
}

function updateHistoryDates() {
  const universe = document.querySelector("#bt-universe").value;
  const dates = selectedUniverseDates(universe);
  const latest = dates?.end_date || state.source?.end_date;
  const earliest = dates?.start_date || state.source?.start_date;
  const start = document.querySelector("#bt-start-date");
  const end = document.querySelector("#bt-end-date");
  end.max = latest || "";
  start.min = earliest || "";
  start.max = latest || "";
  if (latest && (!end.value || end.value > latest)) end.value = latest;
  if (latest && (!start.value || start.value > end.value)) {
    const date = new Date(`${latest}T00:00:00Z`);
    date.setUTCFullYear(date.getUTCFullYear() - 10);
    const proposed = date.toISOString().slice(0,10);
    start.value = proposed < (earliest || proposed) ? (earliest || proposed) : proposed;
  }
}

function setCheckedValues(containerSelector) {
  return [...document.querySelectorAll(`${containerSelector} input:checked`)].map((input) => Number(input.value));
}

async function runBacktest(event) {
  event.preventDefault();
  const errorBox = document.querySelector("#backtest-error");
  const submit = document.querySelector("#backtest-form .run-button");
  errorBox.classList.add("hidden");
  const thresholds = setCheckedValues("#threshold-choices");
  const horizons = setCheckedValues("#horizon-choices");
  if (!thresholds.length || !horizons.length) {
    errorBox.textContent = "Bitte wähle mindestens einen Abstand und eine Haltedauer.";
    errorBox.classList.remove("hidden");
    return;
  }
  const payload = {
    universe: document.querySelector("#bt-universe").value,
    start_date: document.querySelector("#bt-start-date").value,
    end_date: document.querySelector("#bt-end-date").value,
    frequency: document.querySelector("#bt-frequency").value,
    thresholds,
    horizons,
    control_min_pct: Number(document.querySelector("#control-min").value),
    control_max_pct: Number(document.querySelector("#control-max").value),
    cost_bps: Number(document.querySelector("#cost-bps").value),
  };
  if (!payload.start_date || !payload.end_date || payload.start_date > payload.end_date) {
    errorBox.textContent = "Bitte wähle einen gültigen Zeitraum mit Start vor Ende.";
    errorBox.classList.remove("hidden");
    return;
  }
  submit.disabled = true;
  submit.querySelector("span").textContent = "Studie wird berechnet …";
  document.querySelector("#backtest-results").innerHTML = '<div class="results-placeholder"><span class="loader"></span><h2>Historische Signale werden ausgewertet</h2><p>Die App untersucht die Punkt-in-Zeit-Indexmitglieder und berechnet die Vergleichsgruppen für jeden Signaltermin. Lass dieses Fenster geöffnet.</p></div>';
  try {
    const run = await api("/api/backtests", { method: "POST", body: JSON.stringify(payload) });
    state.selectedRunId = run.run_id;
    state.activeRun = { ...run, created_at: new Date().toISOString() };
    await renderBacktest(state.activeRun);
    await loadHistory();
    showToast("Backtest berechnet und lokal gespeichert.");
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.classList.remove("hidden");
    document.querySelector("#backtest-results").innerHTML = `<div class="results-placeholder"><span class="placeholder-graphic">!</span><h2>Backtest nicht abgeschlossen</h2><p>${esc(error.message)}</p></div>`;
  } finally {
    submit.disabled = false;
    submit.querySelector("span").textContent = "Backtest berechnen";
  }
}

function renderSummaryTable(result, horizon) {
  const thresholds = result.thresholds;
  return `<section class="horizon-section"><div class="horizon-title">${esc(result.horizon_labels[String(horizon)] || horizonNames[horizon] || `${horizon} Sitzungen`)}</div>
    <div class="result-table-wrap"><table class="result-table"><thead><tr><th>NÄHE ZUM HOCH</th><th>EINZELEREIGNISSE</th><th>Ø NETTO (EVENT)</th><th>MEDIAN (EVENT)</th><th>GEWINNQUOTE</th><th>VERGLEICH Ø (EVENT)</th><th>MEHRERTRAG (JE DATUM)</th><th>DATUMSANTEIL MIT PLUS</th></tr></thead><tbody>
    ${thresholds.map((threshold) => {
      const thresholdKey = Object.keys(result.results || {}).find((key) => Number(key) === Number(threshold));
      const summary = result.results?.[thresholdKey]?.[String(horizon)] || {};
      return `<tr><td><span class="threshold-tag">≤ ${formatNumber(threshold, 1)} %</span></td><td>${numberFormat.format(summary.event_count || 0)}</td><td class="${(summary.mean_net || 0) >= 0 ? "positive" : "negative"}">${formatFraction(summary.mean_net)}</td><td>${formatFraction(summary.median_net)}</td><td>${formatFraction(summary.win_rate)}</td><td>${formatFraction(summary.control_mean_net)}</td><td class="${(summary.date_balanced_excess_vs_control || 0) >= 0 ? "positive" : "negative"}">${formatFraction(summary.date_balanced_excess_vs_control)}</td><td>${formatFraction(summary.outperforming_date_rate)}</td></tr>`;
    }).join("")}
    </tbody></table></div></section>`;
}

function renderEventsTable(payload, result) {
  const events = payload.events || [];
  const horizonSet = result.horizons || [];
  return `<details class="events-details"><summary>${numberFormat.format(payload.count || 0)} Treffer im Detail ansehen <span>↓</span></summary>
    ${events.length ? `<div class="result-table-wrap"><table class="result-table event-table"><thead><tr><th>SIGNAL</th><th>TICKER</th><th>ABSTAND</th>${horizonSet.map((h) => `<th>${h}S NETTO</th>`).join("")}</tr></thead><tbody>${events.map((event) => `<tr><td>${esc(formatDate(event.market_date, "short"))}</td><td>${esc(event.ticker)}</td><td>${formatNumber(Math.abs(event.distance_pct), 1)} %</td>${horizonSet.map((h) => `<td>${formatFraction(event[`ret_${h}_net`])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : `<p class="result-bottom-note">Für diese Studie liegen keine gespeicherten Signalereignisse vor.</p>`}
  </details>`;
}

function applyParamsToForm(params) {
  if (!params?.start_date || !params?.end_date) return;
  document.querySelector("#bt-universe").value = params.universe;
  updateHistoryDates();
  document.querySelector("#bt-start-date").value = params.start_date;
  document.querySelector("#bt-end-date").value = params.end_date;
  document.querySelector("#bt-frequency").value = params.frequency;
  document.querySelector("#control-min").value = params.control_min_pct;
  document.querySelector("#control-max").value = params.control_max_pct;
  document.querySelector("#cost-bps").value = params.cost_bps;
  const thresholds = new Set((params.thresholds || []).map(Number));
  const horizons = new Set((params.horizons || []).map(Number));
  document.querySelectorAll("#threshold-choices input").forEach((input) => { input.checked = thresholds.has(Number(input.value)); });
  document.querySelectorAll("#horizon-choices input").forEach((input) => { input.checked = horizons.has(Number(input.value)); });
}

async function renderBacktest(run) {
  state.activeRun = run;
  state.selectedRunId = run.run_id;
  const result = run.result;
  const params = run.params || {};
  applyParamsToForm(params);
  const nearLink = `/api/backtests/${encodeURIComponent(run.run_id)}/events.csv?cohort=near`;
  const controlLink = `/api/backtests/${encodeURIComponent(run.run_id)}/events.csv?cohort=control`;
  const warnings = (result.warnings || []).slice(0, 7).map((warning) => `<div>${esc(warning)}</div>`).join("");
  document.querySelector("#backtest-results").innerHTML = `<div class="result-content">
    <div class="result-head"><div><div class="eyebrow"><span class="eyebrow-line"></span> ERGEBNIS · ${esc(universeNames[result.universe] || result.universe)}</div><h2>${esc(formatDate(result.effective_start_date))} – ${esc(formatDate(result.effective_end_date))}</h2>
    <div class="result-meta">${esc(({daily:"Täglich",weekly:"Wöchentlich",monthly:"Monatlich"})[result.frequency] || result.frequency)} · ${numberFormat.format(result.signal_date_count)} Signaltermine · ${numberFormat.format(result.event_count)} Ereignisse · ${formatNumber(result.cost_bps_per_side, 0)} bps je Seite</div></div>
    <div style="display:flex; gap:6px; flex-wrap:wrap"><a class="download-link" href="${nearLink}">↓ Nähe-Events CSV</a><a class="download-link" href="${controlLink}">↓ Vergleich CSV</a></div></div>
    ${warnings ? `<div class="result-warnings">${warnings}</div>` : ""}
    ${(result.horizons || []).map((horizon) => renderSummaryTable(result, horizon)).join("")}
    <div class="result-bottom-note">Die Abstände sind verschachtelt: ≤ 5 % enthält auch Signale innerhalb von ≤ 1 % und ≤ 3 %. Ø, Median, Gewinnquote und Vergleichs-Ø gewichten jedes einzelne Ereignis gleich. Der Mehrertrag je Datum mittelt erst die Renditen pro Signaltermin und vergleicht beide Gruppen an gemeinsamen Terminen; deshalb kann er von der Differenz der Ereignis-Ø abweichen. Renditen sind nach den eingestellten Kauf- und Verkaufskosten.</div>
    <div id="event-detail-table" class="result-bottom-note">Signalereignisse werden geladen …</div>
  </div>`;
  document.querySelectorAll(".history-item").forEach((button) => button.classList.toggle("selected", button.dataset.runId === run.run_id));
  try {
    const eventPayload = await api(`/api/backtests/${encodeURIComponent(run.run_id)}/events?cohort=near&limit=25&offset=0`);
    const target = document.querySelector("#event-detail-table");
    if (target && state.selectedRunId === run.run_id) target.innerHTML = renderEventsTable(eventPayload, result);
  } catch (error) {
    const target = document.querySelector("#event-detail-table");
    if (target) target.textContent = error.message;
  }
}

async function loadHistory() {
  const container = document.querySelector("#backtest-history");
  try {
    const { runs } = await api("/api/backtests?limit=30");
    document.querySelector("#history-count").textContent = numberFormat.format(runs.length);
    if (!runs.length) {
      container.innerHTML = '<div class="history-empty">Noch keine Backtests gespeichert.</div>';
      return;
    }
    container.innerHTML = runs.map((run) => {
      const summary = Object.values(run.result?.results || {})[0] || {};
      const firstHorizon = Object.values(summary)[0] || {};
      return `<button class="history-item ${run.run_id === state.selectedRunId ? "selected" : ""}" data-run-id="${esc(run.run_id)}"><span><span class="history-name">${esc(universeNames[run.universe] || run.universe)} · ${esc((run.params?.thresholds || []).map((value) => `${value}%`).join(", "))}</span><span class="history-meta">${esc(formatDate(run.result?.effective_start_date, "short"))} – ${esc(formatDate(run.result?.effective_end_date, "short"))} · ${esc(({daily:"täglich",weekly:"wöchentlich",monthly:"monatlich"})[run.params?.frequency] || "")}</span></span><span class="history-score">${firstHorizon.mean_net === null || firstHorizon.mean_net === undefined ? "—" : formatFraction(firstHorizon.mean_net)}</span></button>`;
    }).join("");
  } catch (error) {
    container.innerHTML = `<div class="history-empty">${esc(error.message)}</div>`;
  }
}

async function selectSavedRun(runId) {
  document.querySelector("#backtest-results").innerHTML = '<div class="results-placeholder"><span class="loader"></span><h2>Gespeicherte Studie wird geöffnet</h2></div>';
  try {
    const run = await api(`/api/backtests/${encodeURIComponent(runId)}`);
    await renderBacktest(run);
  } catch (error) {
    document.querySelector("#backtest-results").innerHTML = `<div class="results-placeholder"><h2>Studie nicht verfügbar</h2><p>${esc(error.message)}</p></div>`;
  }
}

function attachListeners() {
  document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => changeView(button.dataset.view)));
  window.addEventListener("hashchange", () => changeView(location.hash.replace("#", "") || "screener"));
  document.querySelector("#screener-form").addEventListener("submit", (event) => { event.preventDefault(); loadScreener(); });
  document.querySelector("#universe-select").addEventListener("change", () => { setDefaultDateInputs(); loadScreener(); });
  document.querySelector("#ticker-search").addEventListener("input", renderScreener);
  document.querySelector("#near-only").addEventListener("change", renderScreener);
  document.querySelector("#backtest-form").addEventListener("submit", runBacktest);
  document.querySelector("#bt-universe").addEventListener("change", updateHistoryDates);
  document.querySelector("#backtest-history").addEventListener("click", (event) => {
    const button = event.target.closest("[data-run-id]");
    if (button) selectSavedRun(button.dataset.runId);
  });
  document.querySelector("#screener-rows").addEventListener("click", (event) => {
    const row = event.target.closest("[data-asset-id]");
    if (row) openDetail(row.dataset.assetId);
  });
  document.querySelector("#screener-rows").addEventListener("keydown", (event) => {
    if ((event.key === "Enter" || event.key === " ") && event.target.closest("[data-asset-id]")) {
      event.preventDefault();
      openDetail(event.target.closest("[data-asset-id]").dataset.assetId);
    }
  });
  const close = () => { document.querySelector("#detail-backdrop").classList.add("hidden"); document.body.style.overflow = ""; };
  document.querySelector("#detail-close").addEventListener("click", close);
  document.querySelector("#detail-backdrop").addEventListener("click", (event) => { if (event.target.id === "detail-backdrop") close(); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") close(); });
  document.querySelector("#refresh-button").addEventListener("click", async () => { await loadStatus(); await loadScreener(); showToast("Datenansicht aktualisiert."); });
}

async function init() {
  attachListeners();
  const view = location.hash.replace("#", "");
  changeView(["screener", "backtests", "methodik"].includes(view) ? view : "screener");
  await loadStatus();
  setDefaultDateInputs();
  if (state.source) {
    await loadScreener();
    await loadHistory();
  }
}

init();
