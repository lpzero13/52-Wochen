# 52W High Research

Ein eigenständiges lokales Research-Werkzeug zur Frage, wie Aktien nach einem Signal nahe ihrem 52-Wochen-Hoch abgeschnitten haben. Es greift lesend auf die vorhandene Norgate-SQLite-Datenbank zu und speichert Studien in einer separaten lokalen Datenbank.

## Start

1. Öffne PowerShell in diesem Ordner.
2. Führe `.\start.ps1` aus. Beim ersten Start wird eine lokale Python-Umgebung eingerichtet und die benötigten Pakete werden installiert.
3. Der Browser öffnet `http://127.0.0.1:8020`.
4. Beende die Anwendung mit `.\stop.ps1`.

Setze `NORGATE_DB_PATH` in `.env` auf den Speicherort deiner Norgate-Datei. Ohne Einstellung erwartet die App `data/norgate.sqlite` im Projektordner. Beim ersten Start wird `.env` aus `.env.example` angelegt. `SCANNER_DB_PATH` und `PORT` können dort ebenfalls eingestellt werden. Der Webserver bindet nur an den lokalen Rechner.

## Was die App enthält

- **Screener:** heutiger oder historischer Stand je Nasdaq 100, S&P 500 und Dow Jones 30; nicht verfügbare Stichtage werden auf die letzte vorherige Indexsession gesetzt.
- **Wertpapierdetails:** Preisverlauf über 252 Sitzungen, rollierendes Hoch/Tief, Abstand und 1/3/6/12-Monatsrenditen.
- **Backtests:** einstellbare Signalabstände, tägliche/wöchentliche/monatliche Termine, 21/63/126/252 Sitzungen Haltedauer, Vergleichsgruppe und Kosten; Studien bleiben lokal gespeichert und Ereignisse können als CSV geladen werden.
- **Methodik & Daten:** Definitionen, Quelldatenstatus und Grenzen der Aussagekraft.

## Nutzung durch Hermes Agent

Das Projekt stellt einen nativen lokalen MCP-Server bereit. Hermes kann damit Werkzeuge direkt aufrufen; die Web-App muss dafür nicht laufen. Beim Start über `.\start.ps1` wird auch das MCP-Paket installiert. In Hermes' `~/.hermes/config.yaml` kann dieser Eintrag ergänzt werden:

```yaml
mcp_servers:
  52w-high-research:
    command: 'C:\path\to\52W Scanner\.venv\Scripts\python.exe'
    args: ['-m', 'backend.mcp_server']
    cwd: 'C:\path\to\52W Scanner'
    timeout: 900
```

Ersetze die Beispielpfade durch den Installationsort auf deinem Rechner. Falls das Projekt oder Hermes auf einem anderen Rechner bzw. in WSL läuft, müssen `command`, `cwd` und der in `.env` gesetzte Norgate-Pfad auf Pfade zeigen, die dieser Python-Prozess tatsächlich lesen kann. Nach dem Eintrag muss Hermes die MCP-Verbindung neu laden oder eine neue Sitzung starten.

Hermes erhält die Werkzeuge `get_data_status`, `scan_near_52w_high`, `get_security_detail`, `run_backtest`, `optimize_backtests`, `list_optimization_runs`, `get_optimization_result`, `list_backtest_runs` und `get_backtest_events`. Damit kann es Daten prüfen, Aktien an einem frei gewählten Stichtag suchen, einzelne Backtests rechnen und Ergebnisereignisse einsehen.

`optimize_backtests` testet Kombinationen aus Universum, täglicher/wöchentlicher/monatlicher Scan-Frequenz, Abstand zum 52-Wochen-Hoch, Haltedauer und Kosten. Ohne Datumsangaben verwendet es die vorhandene Kursgeschichte und setzt den Prüfzeitraum beim letzten Drittel der Daten an. Die Rangliste basiert standardmäßig auf dem datumsgewichteten Mehrertrag gegenüber der Kontrollgruppe im späteren Prüfzeitraum; Trainings- und Prüfzeitraum sind durch mindestens die längste getestete Haltedauer getrennt. Zu kleine Stichproben werden ausgeschlossen. Wiederholte Suchen auf demselben Prüfzeitraum können ihn schrittweise zu Trainingsdaten machen; die endgültige Auswahl sollte deshalb auf einem unberührten Zeitraum bestätigt werden.

Der MCP-Server und die REST-Schnittstelle teilen dieselbe Auswertungslogik. Die REST-API bleibt für andere Clients verfügbar; ihre interaktive Beschreibung steht unter `http://127.0.0.1:8020/docs`, das OpenAPI-Schema unter `http://127.0.0.1:8020/openapi.json`.

- `POST /api/backtests` berechnet einen einzelnen konfigurierten Backtest.
- `POST /api/optimizations` führt dieselbe Parametersuche wie das MCP-Werkzeug aus.
- `GET /api/optimizations` listet gespeicherte Suchen; `GET /api/optimizations/{optimization_id}` liefert die vollständige Rangliste.

Beispiel für den JSON-Body von `POST /api/optimizations`:

```json
{
  "universes": ["sp500"],
  "start_date": "2016-01-01",
  "validation_start_date": "2021-01-01",
  "end_date": "2026-09-25",
  "frequencies": ["monthly", "weekly"],
  "thresholds": [1, 3, 5, 10],
  "horizons": [21, 63, 126, 252],
  "costs_bps": [25],
  "objective": "date_balanced_excess_vs_control",
  "min_validation_events": 20,
  "min_validation_dates": 8,
  "top_n": 20
}
```

Das Optimierungsziel kann auch auf mittlere oder mediane Nettorendite, Anteil positiver Ereignisse oder Anteil der Termine mit Mehrertrag gesetzt werden. Varianten mit zu wenigen Prüfereignissen oder Signalterminen werden nicht gerankt; bei Vergleichskennzahlen zählt dafür die Zahl der Termine, an denen sowohl Signal- als auch Kontrollgruppe vorhanden sind. Die Suche ist auf 12 Backtest-Gruppen und 384 Varianten je Anfrage begrenzt. Parameter und Ergebnisrangliste werden in der separaten Research-Datenbank gespeichert; die Norgate-Datenbank bleibt schreibgeschützt. Die bestplatzierte Variante ist nur die beste aus den jeweils getesteten Einstellungen und kein Nachweis zukünftiger Renditen.

## Auswertung lesen

Der Backtest ist eine Ereignisstudie. Das Signal wird zum Tagesschluss erkannt, der Referenzeinstieg erfolgt am nächsten Open und der spätere Referenzausstieg am Close. Die Anzeige zeigt Ereignisrenditen und vergleicht sie mit einer groben Gruppe derselben Indextermine, die weiter unter dem Hoch lag. Sie simuliert kein zusammengesetztes Depot. Überlappende und wiederholte Signale sind nicht unabhängig.

Norgate liefert hier Total-Return-adjustierte OHLC-Preise. Die Norgate-Datenbank wird nicht verändert. Das Ergebnisverzeichnis `var` enthält ausschließlich den getrennten Zustand dieses Projekts.

Weitere Spezifikation: [SPEC.md](SPEC.md).
