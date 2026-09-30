# 52W High Research

Ein eigenständiges lokales Research-Werkzeug zur Frage, wie Aktien nach einem Signal nahe ihrem 52-Wochen-Hoch abgeschnitten haben. Historische Studien lesen die vorhandene Norgate-SQLite-Datenbank. Der aktuelle Scanner lädt kostenlose Schlusskurse und aktuelle Mitgliederlisten in einen eigenen Cache; Studien bleiben in einer separaten lokalen Datenbank.

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

## Kostenlos aktuell halten

Die App prüft beim Start und danach stündlich, ob eine neue abgeschlossene US-Handelssession vorliegt (`LIVE_AUTO_UPDATE=true`). Sie lädt dann Kurse über [yfinance](https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html), Nasdaq-100-Mitglieder von [Nasdaq](https://www.nasdaq.com/products/global-indexes/nasdaq-100/companies) sowie die datierten [SPY](https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx)- und [DIA](https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-dia.xlsx)-Bestände von State Street. SPY/DIA dienen als Mitglieder-Proxy, keine offizielle historische Indexdatei. Kein API-Schlüssel erforderlich. Yahoo ist eine inoffizielle Quelle ohne garantierte Verfügbarkeit; yfinance ist für persönliche Research-Nutzung vorgesehen.

Mit **Kurse aktualisieren** wird ein erneuter Abruf gestartet. Alternativ in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m backend.live
# Bestehenden Tagesstand erneut abrufen, etwa nach einer Datenlücke:
.\.venv\Scripts\python.exe -m backend.live --force
```

- `LIVE_DATA_DIR` bestimmt den Speicherort (Standard `var/live`). `current.sqlite` enthält das aktuelle Scannerfenster. `archive/<run_id>/` enthält Originalmitgliederlisten, Abrufzeitpunkte, Hashes, komprimierte Yahoo-Kurse und das Prüfmanifest. Das Archiv wächst mit den Abrufen.
- Etwa zwei Jahre Yahoo-Kurse werden pro Aktualisierung als vollständiges Fenster neu geladen. OHLC werden gemeinsam mit `Adj Close / Close` bereinigt; Yahoo-Splits werden nicht doppelt angewendet. Das verhindert gemischte Bereinigungsstände bei späteren Dividenden oder Splits. Norgate- und Yahoo-Preisreihen werden nicht aneinandergehängt.
- Der NYSE-Kalender berücksichtigt Feiertage und verkürzte Handelstage. Es werden nur Sessions verwendet, deren Börsenschluss mindestens zwei Stunden zurückliegt. State-Street-Holdings müssen denselben Stichtag haben. Wenn der Anbieter noch nicht aktuell ist, bleibt der bisherige Stand erhalten und die App versucht es bei der nächsten stündlichen Prüfung erneut.
- Mindestens 95 % der Mitglieder je Universum müssen aktuelle gültige Kurse haben. Fehlende Reihen bleiben als gezählte Ausschlüsse sichtbar. Für 52W-Kennzahlen sind weiterhin vollständige 252 Indexsessions erforderlich; junge Börsenneulinge sind häufig noch nicht auswertbar.
- Ein veröffentlichter Tagesstand wird automatisch nur einmal abgerufen. Auch ein veröffentlichter Stand mit kleinen Kurslücken wird erst auf Benutzeranforderung (`--force` bzw. Update-Knopf) erneut geladen. Größere Fehler verhindern die Veröffentlichung; der letzte brauchbare Cache bleibt erhalten. Veröffentlichung erfolgt in einer SQLite-Transaktion; parallele Updates sind durch eine Betriebssystem-Sperre ausgeschlossen.
- Automatische Updates laufen **nur während die App läuft und der Rechner wach ist**. Der nächste App-Start holt einen fehlenden aktuellen Stand nach. Für einen dauerhaft laufenden Server genügt dieser eingebaute Mechanismus; ein Windows-Aufgabenplaner könnte später den CLI-Aufruf auch bei geschlossener App ausführen.

**Survivorship Bias:** Historische Screens, Backtests und Optimierungen verwenden weiterhin nur die historischen Norgate-Mitglieder und -Kurse. Der kostenlose Cache bietet ausschließlich den aktuellen Snapshot. Heutige Nasdaq-Mitglieder werden nicht für frühere Sessions eingetragen. Zwischen Norgate-Ende und aktuellem Snapshot werden historische Screens ohne verifizierte Mitgliederliste abgelehnt. Die Originalbeobachtungen werden ab jetzt archiviert, machen aber fehlende Vergangenheit, Delisting-Renditen oder historische Tickerwechsel nicht automatisch vollständig. Der Zustand der eingebundenen Norgate-Datei wird aus ihrem Exportmanifest angezeigt; etwaige historische Lücken müssen im Norgate-Import repariert werden.

REST: `POST /api/data/update?force=true` startet einen Abruf, `GET /api/data/update` zeigt den Fortschritt. `/api/status` zeigt getrennte Felder `historical`, `live`, `screen_end_date` und `update`; das bisherige `end_date` bleibt der historische Datenstand. Screener und Wertpapierdetails akzeptieren `data_source=auto|historical|live`; `auto` wählt aktuelle kostenlose Daten für den neuesten Stand und Norgate für historische Stichtage. Hermes kann mit `update_free_market_data` einen Abruf starten und ihn mit `get_data_status` verfolgen. Die REST-/MCP-Backtest-Werkzeuge verwenden ausschließlich Norgate.

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

Norgate liefert für historische Studien Total-Return-adjustierte OHLC-Preise. Der aktuelle Scanner verwendet separat bereinigte Yahoo-OHLC-Preise. Die Norgate-Datenbank wird nicht verändert. Das Verzeichnis `var` enthält den getrennten Zustand und kostenlosen Cache dieses Projekts.

Weitere Spezifikation: [SPEC.md](SPEC.md).
