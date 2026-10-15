# Notetaker

Nimmt Meetings auf, transkribiert sie und unterscheidet die Sprecher. Alles läuft lokal auf dem eigenen Rechner, es gibt keine Cloud-Dienste und keine Kosten pro Minute.

Gedacht für Windows 10/11. Der Ton aus dem Kopfhörer wird über WASAPI Loopback aufgenommen, das gibt es nur unter Windows.

## Was es kann

- **Zwei Modi.** Remote-Meeting (Teams, Zoom, Meet): Mikrofon und Systemton werden getrennt aufgenommen, du erscheinst als „Ich", die anderen als „Andere". Vor Ort: nur das Mikrofon, im Text steht „Raum".
- **Titel und Verwaltung.** Jede Aufnahme bekommt einen Titel, du kannst Aufnahmen ansehen, kopieren und löschen.
- **Live-Transkription.** Der Text entsteht schon während des Meetings. Die Aufnahme wird laufend auf die Festplatte geschrieben und übersteht auch einen Absturz bis auf die letzten Sekunden.
- **Sprechertrennung.** Die Stimmen der Gegenseite (bzw. im Raum) werden als „Sprecher 1", „Sprecher 2" unterschieden. Die Namen vergibst du selbst.

## Installation

Voraussetzung ist Python 3.12 (getestet). Falls es fehlt:

```
winget install Python.Python.3.12
```

Dann:

```
git clone https://github.com/smehct/Notetaker.git
cd Notetaker
install.bat
```

`install.bat` legt eine virtuelle Umgebung an, installiert die Pakete aus `requirements.txt` und bietet an, eine Verknüpfung auf dem Desktop anzulegen. Gestartet wird danach mit `start.bat`.

Beim ersten Gebrauch lädt das Programm Modelle herunter:

- Whisper-Modell von Hugging Face (`small`: etwa 0,5 GB)
- Sprechermodelle von GitHub (sherpa-onnx-Releases, zusammen etwa 47 MB) in den Ordner `models\`

## Benutzung

1. Titel eintragen (optional), Modus wählen, „Aufnahme starten".
2. Nach dem Beenden wird der Rest transkribiert und, wenn eingeschaltet, werden die Sprecher erkannt.
3. Mit „Sprecher benennen" ordnest du Namen zu. „Neu transkribieren" erzeugt den Text mit einem anderen Modell oder einer anderen Sprache neu und behält die Namen.

Aufnahmen liegen in `sessions\<Zeitstempel>\`: `mic.wav`, `system.wav`, `transcript.md` sowie `lines.json`, `speakers.json` und `meta.json`. Dieser Ordner und `models\` sind per `.gitignore` vom Repository ausgeschlossen.

Auch ohne Oberfläche:

```
.venv\Scripts\python.exe notetaker.py record --live
.venv\Scripts\python.exe notetaker.py transcribe sessions\2026-10-15_14-30-00
.venv\Scripts\python.exe notetaker.py diarize sessions\2026-10-15_14-30-00 --speakers 3
```

## Grenzen

- **Tempo.** Auf einem Laptop mit Core i5-1235U ohne eigene Grafikkarte lief das Modell `small` im Test zwischen 0,5- und 1,5-facher Echtzeit, die Werte schwankten stark. Ein einstündiges Meeting kann also deutlich länger als eine halbe Stunde Rechenzeit brauchen. Die Live-Transkription verteilt das auf die Dauer des Meetings. Mit `base` oder `tiny` geht es schneller, mit schlechterer Qualität. Eine GPU habe ich nicht getestet.
- **Sprechertrennung.** Getestet nur mit synthetischen Stimmen. Bei ähnlichen Stimmen, überlappender Sprache oder Hall sind Fehler zu erwarten. Das verwendete Embedding-Modell ist nach seinem Namen für Englisch gedacht, auf Deutsch ist es nicht geprüft. Die Anzahl der Sprecher lässt sich in den Einstellungen vorgeben.
- **Mischsprachige Meetings.** Whisper erkennt die Sprache einmal pro Datei. Bei mehreren Sprachen im selben Gespräch können Passagen fehlen.
- **Mikrofon im Raum.** Offene Mikrofone nehmen auch Stimmen und Geräusche aus der Umgebung auf und können sie transkribieren.

## Recht und Datenschutz

Es werden keine Daten an Dritte gesendet. Trotzdem gilt: Gespräche nur mit Wissen und Einverständnis der Beteiligten aufnehmen. Die Regeln dazu unterscheiden sich je nach Land.

## Bausteine

[faster-whisper](https://github.com/SYSTRAN/faster-whisper) für die Transkription, [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) für die Sprechertrennung, [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) für die Aufnahme, [customtkinter](https://github.com/TomSchimansky/CustomTkinter) für die Oberfläche. Die Modelle haben eigene Lizenzen. Sie liegen bei den jeweiligen Anbietern und sind vor einer kommerziellen Nutzung selbst zu prüfen.

Dieses Projekt ist unabhängig und steht in keiner Verbindung zu Wispr.

Der Code steht unter der MIT-Lizenz (siehe `LICENSE`). Das gilt nicht für die heruntergeladenen Modelle und die installierten Pakete.
