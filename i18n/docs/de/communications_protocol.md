# Kommunikationsprotokoll zur Ideenbewertung

Alle nachfolgenden Agenten, die im Ökosystem der Windows-Kontextmenü-Anpassung arbeiten, müssen dieses strukturierte Protokoll befolgen, wenn sie vorgeschlagene Ideen anfechten, bewerten oder verfeinern.

## 1. Die "Steel-Man"-Doktrin
Bevor eine Idee angefochten wird, muss der bewertende Agent die stärkste mögliche Version des ursprünglichen Vorschlags konstruieren.
- Das zentrale Wertversprechen klarer formulieren als der ursprüngliche Autor.
- Mindestens einen ungenannten Nutzen des Ansatzes identifizieren.

## 2. Red-Teaming (Schwachstellenbewertung)
Sobald die Idee stark gemacht wurde, müssen Agenten sie entlang der folgenden Vektoren herausfordern:
- **Ökosystem-Fit:** Fühlt sich das wie ein natives Kontextmenü-Tool an oder versucht es, eine vollwertige Anwendung zu sein?
- **Leistung:** Was passiert, wenn dies versehentlich auf einem Verzeichnis mit 100.000 Dateien ausgeführt wird?
- **Destruktivität:** Besteht ein Risiko unwiederbringlichen Datenverlusts?
- **Abhängigkeitsaufwand:** Erfordert dies übermäßige externe Abhängigkeiten (z. B. große Python-Bibliotheken oder nicht installierte Binärdateien)?

## 3. Das Widerlegungsformat
Jede Kritik muss wie folgt strukturiert sein:
- **Hypothese:** Was die Idee lösen will.
- **Schwachstelle:** Der spezifische festgestellte Fehler oder das Risiko.
- **Alternative Formulierung:** Eine vorgeschlagene Wendung, die den Wert erhält und das Risiko mindert.

## 4. Mechanismus für das endgültige Urteil
Ideen dürfen nicht ohne Vorschlag einer Wendung rundweg abgelehnt werden, es sei denn, sie stellen ein katastrophales Risiko für das Dateisystem dar (z. B. unverfolgtes rekursives Löschen).

<!-- source-digest: communications_protocol.md sha256:5ac43f32d3432e2d -->
