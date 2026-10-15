"""Lokaler Notetaker: nimmt Mikrofon und Systemton (was im Kopfhörer läuft) getrennt auf
und transkribiert beide Spuren lokal mit faster-whisper.

Oberfläche:
    python app.py

Kommandozeile (Enter oder Ctrl+C beendet die Aufnahme):
    python notetaker.py                      # aufnehmen, danach transkribieren
    python notetaker.py record --live        # schon während der Aufnahme transkribieren
    python notetaker.py record --mic-only    # nur Mikrofon (Treffen vor Ort)

Eine bestehende Aufnahme erneut oder mit anderem Modell transkribieren:
    python notetaker.py transcribe sessions\\2026-10-15_14-30-00

Sprecher einer transkribierten Sitzung unterscheiden (lädt beim ersten Mal ~47 MB Modelle):
    python notetaker.py diarize sessions\\2026-10-15_14-30-00 --speakers 3

Ergebnis pro Sitzung in sessions\\<zeitstempel>\\: mic.wav, system.wav, transcript.md
(dazu lines.json und speakers.json, aus denen transcript.md erzeugt wird)
Die WAV-Dateien werden während der Aufnahme laufend geschrieben. Nach einem Absturz
bleibt alles bis auf die letzten Sekunden erhalten und lässt sich nachträglich transkribieren.
"""
import argparse
import json
import os
import queue
import shutil
import struct
import sys
import threading
import time
from datetime import datetime
from math import gcd
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

import speakers

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

SESSIONS_DIR = Path(__file__).parent / "sessions"
WHISPER_RATE = 16000
WAV_HEADER = 44
LABELS = {"mic": "Ich", "system": "Andere"}
LABELS_ROOM = {"mic": "Raum"}  # nur Mikrofon: alle sprechen ins selbe Mikrofon


# --- WAV-Dateien --------------------------------------------------------------

class WavWriter:
    """Schreibt Mono-16-Bit-WAV schrittweise. Der Header wird alle paar Sekunden
    nachgeführt, damit die Datei auch nach einem Absturz lesbar bleibt."""

    def __init__(self, path, rate):
        self.f = open(path, "wb")
        self.rate = rate
        self.samples = 0
        self._patched = time.monotonic()
        self._write_header()
        self.f.flush()

    def _write_header(self):
        data = self.samples * 2
        self.f.write(struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + data, b"WAVE", b"fmt ",
                                 16, 1, 1, self.rate, self.rate * 2, 2, 16, b"data", data))

    def _patch(self):
        pos = self.f.tell()
        self.f.seek(0)
        self._write_header()
        self.f.seek(pos)
        self.f.flush()
        self._patched = time.monotonic()

    def write(self, samples):
        self.f.write(samples.astype(np.int16).tobytes())
        self.samples += len(samples)
        self.f.flush()
        if time.monotonic() - self._patched > 5:
            self._patch()

    def close(self):
        self._patch()
        self.f.close()


def wav_rate(path):
    with open(path, "rb") as f:
        f.seek(24)
        return struct.unpack("<I", f.read(4))[0]


def wav_samples(path):
    """Anzahl der Samples nach Dateigrösse (auch wenn der Header veraltet ist)."""
    return max(0, (Path(path).stat().st_size - WAV_HEADER) // 2)


def read_samples(path, start, count):
    with open(path, "rb") as f:
        f.seek(WAV_HEADER + 2 * start)
        data = f.read(2 * count)
    return np.frombuffer(data[: len(data) // 2 * 2], dtype=np.int16)


def to_16k(samples, rate):
    audio = samples.astype(np.float32) / 32768
    if rate != WHISPER_RATE:
        g = gcd(rate, WHISPER_RATE)
        audio = resample_poly(audio, WHISPER_RATE // g, rate // g).astype(np.float32)
    return audio


def read_wav_16k(path):
    return to_16k(read_samples(path, 0, wav_samples(path)), wav_rate(path))


# --- Aufnahme -----------------------------------------------------------------

def find_devices(pa, pyaudio, with_system=True):
    """Gibt (Mikrofon-Info, Loopback-Info des Standard-Ausgabegeräts) zurück.
    Ohne with_system ist der zweite Wert None."""
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    mic = pa.get_device_info_by_index(wasapi["defaultInputDevice"])
    if not with_system:
        return mic, None
    speakers = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
    if not speakers.get("isLoopbackDevice"):
        for loopback in pa.get_loopback_device_info_generator():
            if speakers["name"] in loopback["name"]:
                speakers = loopback
                break
        else:
            raise RuntimeError(f"Kein Loopback-Gerät für '{speakers['name']}' gefunden.")
    return mic, speakers


class Source:
    """Eine Audioquelle. Der Audio-Callback legt Blöcke nur in eine Warteschlange,
    ein eigener Thread schreibt sie laufend in die WAV-Datei."""

    def __init__(self, pa, pyaudio, info, wav_path, t0):
        self.rate = int(info["defaultSampleRate"])
        self.channels = int(info["maxInputChannels"])
        self.t0 = t0
        self.peak = 0.0
        self.written = 0
        self.q = queue.SimpleQueue()
        self.wav = WavWriter(wav_path, self.rate)
        self.thread = threading.Thread(target=self._write_loop, daemon=True)
        self.thread.start()
        self.continue_flag = pyaudio.paContinue
        self.stream = pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=info["index"],
            frames_per_buffer=1024,
            stream_callback=self._callback,
        )

    def _callback(self, in_data, frame_count, time_info, status):
        self.q.put((time.monotonic(), in_data))
        samples = np.frombuffer(in_data, dtype=np.int16)
        if samples.size:
            self.peak = max(self.peak, float(np.abs(samples).max()) / 32768)
        return None, self.continue_flag

    def _write_loop(self):
        """Der Loopback liefert bei Stille keine Daten. Lücken werden anhand der
        Ankunftszeiten mit Stille aufgefüllt, damit die Zeitachse stimmt."""
        while True:
            item = self.q.get()
            if item is None:
                return
            t, data = item
            block = np.frombuffer(data, dtype=np.int16).reshape(-1, self.channels)
            block = block.astype(np.float32).mean(axis=1).astype(np.int16)
            gap = int((t - self.t0) * self.rate) - (self.written + len(block))
            if gap > self.rate * 0.3:
                self.wav.write(np.zeros(gap, dtype=np.int16))
                self.written += gap
            self.wav.write(block)
            self.written += len(block)

    def close(self, t_end):
        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception:
            pass
        self.q.put(None)
        self.thread.join()
        total = int((t_end - self.t0) * self.rate)
        if total > self.written:
            self.wav.write(np.zeros(total - self.written, dtype=np.int16))
        self.wav.close()


class Recorder:
    """Nimmt das Mikrofon und, wenn with_system gesetzt ist, auch den Systemton
    (Ausgabe an Lautsprecher/Kopfhörer) auf. Die Aufnahme beginnt mit dem Erzeugen
    des Objekts und wird laufend nach session_dir geschrieben."""

    def __init__(self, with_system=True, session_dir=None):
        import pyaudiowpatch as pyaudio
        self.session_dir = session_dir or new_session_dir()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.tracks = ["mic"] + (["system"] if with_system else [])
        self.pa = pyaudio.PyAudio()
        self.mic = self.system = None
        self.system_name = None
        self.t0 = time.monotonic()
        try:
            mic_info, sys_info = find_devices(self.pa, pyaudio, with_system)
            self.mic_name = mic_info["name"]
            self.mic = Source(self.pa, pyaudio, mic_info, self.session_dir / "mic.wav", self.t0)
            if sys_info:
                self.system_name = sys_info["name"]
                self.system = Source(self.pa, pyaudio, sys_info,
                                     self.session_dir / "system.wav", self.t0)
        except Exception:
            for src in (self.mic, self.system):
                if src:
                    src.close(time.monotonic())
            self.pa.terminate()
            shutil.rmtree(self.session_dir, ignore_errors=True)
            raise

    def elapsed(self):
        return time.monotonic() - self.t0

    def take_levels(self):
        """Spitzenpegel (0..1) seit dem letzten Aufruf: (Mikrofon, Systemton)."""
        levels = (self.mic.peak, self.system.peak if self.system else 0.0)
        self.mic.peak = 0.0
        if self.system:
            self.system.peak = 0.0
        return levels

    def stop(self):
        t_end = time.monotonic()
        for src in (self.mic, self.system):
            if src:
                src.close(t_end)
        self.pa.terminate()
        return self.session_dir


def new_session_dir():
    return SESSIONS_DIR / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def get_title(session_dir):
    try:
        return json.loads((session_dir / "meta.json").read_text(encoding="utf-8")).get("title", "")
    except (OSError, ValueError):
        return ""


def set_title(session_dir, title):
    if session_dir.exists():
        (session_dir / "meta.json").write_text(
            json.dumps({"title": title.strip()}, ensure_ascii=False), encoding="utf-8")


def delete_session(session_dir):
    """Löscht eine Sitzung endgültig (Audio, Transkript, Sprecherdaten). Nur Ordner
    innerhalb von sessions/ werden akzeptiert."""
    session_dir = session_dir.resolve()
    if session_dir.parent != SESSIONS_DIR.resolve():
        raise ValueError(f"Kein Sitzungsordner: {session_dir}")
    shutil.rmtree(session_dir)


# --- Transkription ------------------------------------------------------------

_models = {}


def load_model(name, log=print):
    if name not in _models:
        from faster_whisper import WhisperModel
        log(f"Lade Modell '{name}' (beim ersten Mal wird es heruntergeladen) ...")
        _models[name] = WhisperModel(name, device="auto", compute_type="auto")
    return _models[name]


def transcribe_track(model, audio, language, progress=None):
    """Gibt [(start_s, ende_s, text), ...] zurück. Diese Funktion ersetzen, um ein
    anderes Backend (z.B. Cloud-API) zu verwenden. progress(0..1) meldet den Fortschritt."""
    segments, _ = model.transcribe(
        audio, language=language or None, vad_filter=True, beam_size=5)
    duration = max(len(audio) / WHISPER_RATE, 1e-9)
    result = []
    for s in segments:
        if s.text.strip():
            result.append((s.start, s.end, s.text.strip()))
        if progress:
            progress(min(s.end / duration, 1.0))
    return result


def make_lines(track, offset, segments):
    return [{"start": offset + start, "end": offset + end, "track": track, "text": text}
            for start, end, text in segments]


def is_silent(audio):
    return audio.size == 0 or np.abs(audio).max() < 0.005


def transcribe(session_dir, model_name, language, log=print, progress=None):
    """Transkribiert mic.wav und system.wav einer Sitzung vollständig nach transcript.md
    (genauer als die Live-Variante, weil Whisper die ganze Spur im Zusammenhang sieht).
    Gibt den Pfad des Transkripts zurück."""
    tracks = [n for n in LABELS if (session_dir / f"{n}.wav").exists()]
    if not tracks:
        raise FileNotFoundError(f"Keine mic.wav/system.wav in {session_dir}")
    labels = LABELS_ROOM if tracks == ["mic"] else LABELS

    model = load_model(model_name, log)
    lines = []
    for i, name in enumerate(tracks):
        def track_progress(frac, i=i):
            if progress:
                progress((i + frac) / len(tracks))

        audio = read_wav_16k(session_dir / f"{name}.wav")
        if is_silent(audio):
            log(f"{labels[name]}: Spur ist still.")
        else:
            log(f"Transkribiere {labels[name]} ({len(audio) / WHISPER_RATE / 60:.1f} min) ...")
            lines += make_lines(name, 0, transcribe_track(
                model, audio, language, track_progress))
        track_progress(1.0)

    speakers.save_lines(session_dir, lines)
    speakers.render_transcript(session_dir)
    out = session_dir / "transcript.md"
    log(f"Transkript: {out}")
    return out


def diarize(session_dir, num_speakers=-1, log=print, progress=None):
    """Erkennt die Sprecher der Gegenseite (Remote-Meeting) bzw. im Raum (nur Mikrofon)
    und beschriftet das Transkript neu. Die Spur "Ich" wird nicht getrennt."""
    track = "system" if (session_dir / "system.wav").exists() else "mic"
    path = session_dir / f"{track}.wav"
    if not path.exists():
        raise FileNotFoundError(f"Keine Aufnahme in {session_dir}")
    if not speakers.load_lines(session_dir):
        raise RuntimeError("Noch kein Transkript vorhanden. Zuerst transkribieren.")
    speakers.ensure_models(log)
    audio = read_wav_16k(path)
    turns = {}
    if is_silent(audio):
        log("Spur ist still, keine Sprecher erkannt.")
    else:
        log("Erkenne Sprecher ...")
        turns[track] = speakers.diarize_audio(audio, num_speakers, progress=progress)
        found = len({t[2] for t in turns[track]})
        log(f"{found} Sprecher erkannt." if found != 1 else "Nur 1 Sprecher erkannt.")
    speakers.save_turns(session_dir, turns)
    return speakers.render_transcript(session_dir)


def find_cut(samples, rate, lo, hi):
    """Stelle zwischen lo und hi (Samples) mit dem leisesten 0,2-s-Fenster, damit
    zwischen zwei Abschnitten möglichst kein Wort zerschnitten wird."""
    frame = max(1, int(0.2 * rate))
    best, best_rms = hi, None
    for pos in range(lo, hi - frame + 1, frame // 2 or 1):
        window = samples[pos:pos + frame].astype(np.float32)
        rms = float(np.sqrt(np.mean(window ** 2))) if window.size else 0.0
        if best_rms is None or rms < best_rms:
            best, best_rms = pos + frame // 2, rms
    return best


class LiveTranscriber:
    """Transkribiert die Dateien einer laufenden Aufnahme in Abschnitten von etwa
    25–30 Sekunden. Mit finish() wird nach Ende der Aufnahme der Rest abgearbeitet.
    Bei langsamer CPU entsteht ein Rückstand, der nach dem Meeting aufgeholt wird.

    on_update(text_markdown, transkribiert_bis_sekunden) wird nach jedem Abschnitt
    aufgerufen, on_progress(0..1) beim Abarbeiten des Rests."""

    MIN_CHUNK_S, MAX_CHUNK_S = 25, 30

    def __init__(self, session_dir, tracks, model_name, language,
                 on_update=None, on_progress=None, log=print):
        self.dir, self.tracks = session_dir, tracks
        self.model_name, self.language = model_name, language
        self.on_update, self.on_progress, self.log = on_update, on_progress, log
        self.lines = []
        self.done = {t: 0 for t in tracks}
        self.recording = True
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def finish(self):
        """Wartet, bis alles aufgenommene Audio transkribiert ist."""
        self.recording = False
        self.thread.join()
        if self.error:
            raise self.error

    def _run(self):
        try:
            model = load_model(self.model_name, self.log)
            while True:
                worked = False
                for name in self.tracks:
                    worked |= self._process(name, model)
                if not worked:
                    if not self.recording:
                        break
                    time.sleep(1)
        except Exception as e:
            self.error = e
            self.log(f"Live-Transkription gestoppt: {e}")

    def _process(self, name, model):
        path = self.dir / f"{name}.wav"
        if not path.exists():
            return False
        rate = wav_rate(path)
        pending = wav_samples(path) - self.done[name]
        if pending <= 0 or (self.recording and pending < self.MIN_CHUNK_S * rate):
            return False
        window = min(pending, self.MAX_CHUNK_S * rate)
        samples = read_samples(path, self.done[name], window)
        window = len(samples)
        if window == pending and not self.recording:
            cut = window  # letzter Abschnitt: alles nehmen
        else:
            cut = find_cut(samples, rate, max(0, window - 5 * rate), window)
        offset = self.done[name] / rate
        self.done[name] += cut

        audio = to_16k(samples[:cut], rate)
        if not is_silent(audio):
            self.lines += make_lines(name, offset,
                                     transcribe_track(model, audio, self.language))
        self._publish()
        return True

    def _publish(self):
        speakers.save_lines(self.dir, self.lines)
        text = speakers.render_transcript(self.dir)
        upto = min(self.done[t] / wav_rate(self.dir / f"{t}.wav") for t in self.tracks
                   if (self.dir / f"{t}.wav").exists())
        if self.on_update:
            self.on_update(text, upto)
        if self.on_progress and not self.recording:
            total = sum(wav_samples(self.dir / f"{t}.wav") for t in self.tracks)
            self.on_progress(min(1.0, sum(self.done.values()) / max(total, 1)))


# --- CLI ----------------------------------------------------------------------

def record_cli(with_system=True, live=None):
    """Nimmt auf, bis Enter oder Ctrl+C. live = (modell, sprache) transkribiert mit."""
    rec = Recorder(with_system)
    print(f"Mikrofon:  {rec.mic_name}")
    if rec.system_name:
        print(f"Systemton: {rec.system_name}")
    transcriber = None
    if live:
        transcriber = LiveTranscriber(rec.session_dir, rec.tracks, *live)
        transcriber.start()

    stop = threading.Event()
    threading.Thread(target=lambda: (input(), stop.set()), daemon=True).start()
    print("Aufnahme läuft. Enter drücken zum Beenden.\n")
    try:
        while not stop.is_set():
            elapsed = int(rec.elapsed())
            mic, system = rec.take_levels()
            print(f"\r{elapsed // 60:02d}:{elapsed % 60:02d}  "
                  f"Mikrofon {'#' * int(mic * 20):<20}  "
                  f"Systemton {'#' * int(system * 20):<20}", end="", flush=True)
            stop.wait(0.5)
    except KeyboardInterrupt:
        pass
    print("\nAufnahme beendet.")
    session_dir = rec.stop()
    print(f"Gespeichert in {session_dir}")
    if transcriber:
        print("Transkribiere den Rest ...")
        transcriber.finish()
        print(f"Transkript: {session_dir / 'transcript.md'}")
    return session_dir


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd")

    def add_model_args(p):
        p.add_argument("--model", default="small",
                       help="tiny, base, small, medium, large-v3-turbo, large-v3 (Standard: small)")
        p.add_argument("--language", default="de", help="Sprachcode, leer für Auto (Standard: de)")

    rec = sub.add_parser("record", help="aufnehmen und danach transkribieren (Standard)")
    add_model_args(rec)
    rec.add_argument("--no-transcribe", action="store_true", help="nur aufnehmen")
    rec.add_argument("--live", action="store_true", help="schon während der Aufnahme transkribieren")
    rec.add_argument("--mic-only", action="store_true",
                     help="nur Mikrofon (Treffen vor Ort), ohne Systemton")
    tr = sub.add_parser("transcribe", help="bestehende Sitzung transkribieren")
    tr.add_argument("session", type=Path)
    add_model_args(tr)
    di = sub.add_parser("diarize", help="Sprecher einer transkribierten Sitzung unterscheiden")
    di.add_argument("session", type=Path)
    di.add_argument("--speakers", type=int, default=-1,
                    help="Anzahl der Sprecher, wenn bekannt (Standard: automatisch)")

    args = parser.parse_args(sys.argv[1:] or ["record"])
    if args.cmd == "transcribe":
        transcribe(args.session, args.model, args.language)
    elif args.cmd == "diarize":
        diarize(args.session, args.speakers)
        print(f"Transkript: {args.session / 'transcript.md'}")
    else:
        live = (args.model, args.language) if args.live else None
        session_dir = record_cli(with_system=not args.mic_only, live=live)
        if not args.no_transcribe and not args.live:
            transcribe(session_dir, args.model, args.language)


if __name__ == "__main__":
    main()
