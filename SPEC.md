# 52W High Research — Produktspezifikation

## Zweck

Eine eigenständige lokale Research-Anwendung beantwortet eine klar umrissene Frage: Wie haben sich Aktien entwickelt, wenn ihr Schlusskurs nahe am höchsten Tageshoch der vergangenen 52 Wochen lag? Die Anwendung bietet einen aktuellen/historischen Screener und konfigurierbare historische Ereignisstudien.

Das Produkt beginnt unabhängig und frisch. Es importiert keinen Programmcode, keine Berechnungen, keine Oberfläche und keine Laufzeit aus dem Momentum-Research-Projekt. Die einzige gemeinsame Ressource ist die lokal installierte Norgate-Kursdatenbank. Es gibt keinen Momentum-Score, kein Ranking aus einem anderen Projekt und keine Momentum-Strategie.

## Nutzeraufgaben

1. Für Nasdaq 100, S&P 500 oder Dow Jones 30 sehen, welche enthaltenen Aktien am gewählten Datum nahe am 52-Wochen-Hoch standen.
2. Einen beliebigen Stichtag wählen. Ein Nicht-Handelstag wird auf die letzte vorherige Indexsession aufgelöst.
3. Ein Wertpapier öffnen und 252 Sitzungen, Hoch/Tief, Abstand und zurückliegende Renditen ansehen.
4. Backtests mit verschiedenen Abständen, Haltedauern, Scan-Frequenzen, Zeiträumen, Kontrollabständen und Kosten konfigurieren.
5. Studien lokal speichern, Ergebnisse vergleichen und Signalereignisse als CSV exportieren.
6. Über einen lokalen MCP-Server oder die REST-API Parametersuchen an einen Agenten übergeben und Varianten anhand eines zeitlich getrennten Prüfzeitraums ordnen.
7. Den aktuellen Scanner ohne Norgate-Abonnement mit kostenlosen EOD-Daten aktualisieren. Historische Studien bleiben auf der Norgate-Historie.

## Definitionen

- **52-Wochen-Fenster:** 252 Index-Handelssitzungen bis einschließlich Signalstichtag.
- **52-Wochen-Hoch/Tief:** Maximum der adjustierten Tageshochs bzw. Minimum der adjustierten Tagestiefs innerhalb des Fensters.
- **Nähe:** Schlusskurs / 52-Wochen-Hoch − 1. Der Screener und die Studie gruppieren den prozentualen Abstand unter dem Hoch.
- **Historische Mitgliedschaft:** Für jedes Datum werden die zu diesem Datum in Norgate gespeicherten Indexmitglieder verwendet, nicht heutige Mitglieder rückwirkend.
- **Signal:** Schlusskurs liegt höchstens den gewählten Abstand unter dem Hoch.
- **Referenzeinstieg:** Adjustiertes Open der nächsten Indexsession nach dem Signal.
- **Referenzausstieg:** Adjustierter Close nach 21, 63, 126 oder 252 Indexsessions ab dem Signal. Das entspricht bei der festgelegten Einstiegssession einer Haltedauer von 21, 63, 126 bzw. 252 Sitzungen.
- **Nettorendite:** Ausstiegskurs / Einstiegskurs × (1 − Kostenrate) / (1 + Kostenrate) − 1. Dieselben Kosten in Basispunkten werden je Seite angenommen.
- **Kontrollgruppe:** Mitglieder derselben Indexsession, deren Schlusskurs im einstellbaren Abstandsband (Standard 15–25 %) unter ihrem 52-Wochen-Hoch lag.
- **Vergleich je Datum:** Für Signal- und Kontrollgruppe werden zuerst die Ereignisrenditen pro Signaltermin gemittelt; anschließend wird die mittlere Differenz über gemeinsame Termine berechnet. Die Ereignisanzahl zeigt zusätzlich die ungewichteten Einzelereignisse.

## Interpretation und Grenzen

Der Backtest ist eine deskriptive Ereignisstudie, keine zusammengesetzte Depot- oder Portfolio-Simulation. Es gibt keine Positionsgrößen, Kapitalbindung oder Portfolioüberlappungsregeln. Wiederholte Signale einer Aktie und überlappende Haltedauern bleiben in der Ereignismenge und sind statistisch nicht unabhängig. Die Kontrollgruppe ist grob und nicht risikogleich. Aus den Zahlen folgt kein Kausal- oder Out-of-Sample-Nachweis.

Die Norgate-Preise sind Total-Return-adjustierte OHLC-Referenzdaten. Die Anwendung rekonstruiert keine tatsächlichen Aktien-Stückzahlen, Dividendenausschüttungen, Steuern, Finanzierung, Geld-Brief-Spannen oder Slippage. Backtest-Ausgaben sind deshalb Referenzrenditen und keine realisierbaren Handelsergebnisse.

## Quelle und Datenqualität

- Norgate SQLite wird mit `mode=ro` und `PRAGMA query_only=ON` geöffnet.
- Export, Schema, Aktualisierungszeitpunkt, Abdeckung und aktuelle Indexgrößen werden in der App offengelegt.
- Ein als unvollständig markiertes Exportmanifest bleibt als Warnung sichtbar. Die App behauptet keine vollständige oder überlebensfehlerfreie Grundgesamtheit.
- Aktien ohne 252 gültige tägliche OHLC-Beobachtungen werden aus der jeweiligen Screener-Zeile ausgeschlossen und gezählt.
- Für eine einzelne Studie werden fehlende OHLC-Werte in der Signalhistorie ausgeschlossen; fehlende Zukunftskurse machen nur den jeweiligen Horizont nicht verfügbar.
- Das beim Export gespeicherte Point-in-Time-Universum wird verwendet. Nachträgliche Norgate-Datenrevisionen sind nicht archiviert.
- Für den aktuellen Stand existiert ein getrennter Yahoo-Cache mit etwa zwei Jahren vollständig neu bereinigter OHLC-Historie. Heutige Nasdaq-Mitglieder und datierte SPY-/DIA-Holdings-Proxies werden nur dem aktuellen Snapshot zugeordnet, nicht rückwirkend in die Vergangenheit geschrieben. Backtests und Optimierungen verwenden diesen Cache nicht.
- Kostenlose Originaldaten, Quellstichtage und Beobachtungszeitpunkte werden je Abruf archiviert. Ein fehlender historischer Mitgliederstand nach dem Norgate-Ende wird im historischen Screener abgelehnt.
- Neue EOD-Stände werden erst zwei Stunden nach Börsenschluss laut NYSE-Kalender akzeptiert. Pro Universum sind mindestens 95 % aktuelle gültige Kursreihen erforderlich. Fehlgeschlagene Updates lassen den vorherigen Cache unverändert.

## Speicherung und Betrieb

Die Anwendung speichert Backtestparameter, Zusammenfassungen und Einzelereignisse separat in `var/52w_scanner.sqlite`. Agenten-Optimierungen speichern Anfrage, Rangliste und Datenstand in derselben getrennten Datenbank. Die Norgate-Quelldatei wird nicht verändert. Der lokale REST-Server bindet standardmäßig nur an `127.0.0.1:8020`; zusätzlich stellt `backend.mcp_server` Werkzeuge über MCP-Stdio bereit, sodass Hermes den Server lokal als Unterprozess starten kann. Beide Schnittstellen greifen auf dieselbe Auswertungslogik und getrennte Research-Datenbank zu. Die API-Dokumentation ist unter `/docs` verfügbar. `.env` kann den Norgate-Pfad, den getrennten Speicherort und den Port überschreiben.

Eine Parametersuche vergleicht die getesteten Abstände und Haltedauern sowie ausgewählte Universen, Scan-Frequenzen und Kosten. Der Standard-Rangwert ist der nach gemeinsamen Signal- und Kontrollterminen gewichtete Mehrertrag gegenüber der Kontrollgruppe. Der Nutzer kann alternativ Ereignis-Durchschnitt, Ereignis-Median, Gewinnanteil oder Anteil der Termine mit Mehrertrag wählen. Ergebnisse aus zu kleinen Prüf-Stichproben werden nicht gerankt; für Vergleichskennzahlen wird dabei die Zahl gemeinsamer Termine verwendet. Zwischen Trainings- und Prüfzeitraum liegt mindestens die längste ausgewertete Haltedauer; auch Ausstiege im Prüfzeitraum bleiben innerhalb seines Enddatums. Die Suche ist pro Anfrage begrenzt, damit lokale Berechnungszeit und Speicherbedarf kontrollierbar bleiben.

## Umfang außerhalb der Version 1

Keine Verbindung zu Momentum- oder sonstiger Projektruntime, keine Brokerorders, keine rückwirkende Ersetzung historischer Indexmitglieder durch heutige Listen, keine Portfolio-/Kapital-Simulation und keine Behauptung, die Nähe zum Hoch sei allein eine Kaufempfehlung. Kostenlose Updates ersetzen keine vollständige delisting- und corporate-action-bereinigte Point-in-Time-Datenbank.
