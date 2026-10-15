"""Sprechertrennung (sherpa-onnx, lokal) und Darstellung des Transkripts.

Pro Sitzung liegen im Ordner:
    lines.json     die erkannten Sätze (Start, Ende, Spur, Text)
    speakers.json  Sprecherabschnitte pro Spur ("turns") und vergebene Namen ("names")
    transcript.md  daraus erzeugter Text
Die Namen gelten pro Sitzung und überleben eine erneute Transkription.
"""
import json
import tarfile
import urllib.request
from pathlib import Path

MODELS_DIR = Path(__file__).parent / "models"
RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
SEG_ARCHIVE = "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
SEG_URL = RELEASES + "speaker-segmentation-models/" + SEG_ARCHIVE
SEG_MODEL = MODELS_DIR / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
EMB_FILE = "nemo_en_titanet_small.onnx"
EMB_URL = RELEASES + "speaker-recongition-models/" + EMB_FILE
EMB_MODEL = MODELS_DIR / EMB_FILE

LABEL_ME, LABEL_OTHERS, LABEL_ROOM = "Ich", "Andere", "Raum"


# --- Modelle ------------------------------------------------------------------

def _download(url, target):
    part = target.with_name(target.name + ".part")
    urllib.request.urlretrieve(url, part)
    part.replace(target)


def ensure_models(log=print):
    """Lädt die beiden Modelle (zusammen etwa 47 MB) einmalig von GitHub."""
    MODELS_DIR.mkdir(exist_ok=True)
    if not SEG_MODEL.exists():
        log("Lade Sprecher-Modell 1/2 (7 MB) ...")
        archive = MODELS_DIR / SEG_ARCHIVE
        _download(SEG_URL, archive)
        with tarfile.open(archive, "r:bz2") as tar:
            tar.extractall(MODELS_DIR, filter="data")
        archive.unlink()
    if not EMB_MODEL.exists():
        log("Lade Sprecher-Modell 2/2 (40 MB) ...")
        _download(EMB_URL, EMB_MODEL)


# --- Sprechertrennung ---------------------------------------------------------

def diarize_audio(audio, num_speakers=-1, threshold=0.5, progress=None):
    """audio: float32 mono, 16 kHz. Gibt [(start_s, ende_s, sprecher)] zurück, die
    Sprecher sind nach dem ersten Auftreten ab 0 nummeriert. num_speakers=-1 bestimmt
    die Anzahl automatisch (kleinerer threshold = mehr Sprecher)."""
    import sherpa_onnx
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(SEG_MODEL)),
            num_threads=4),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(EMB_MODEL), num_threads=4),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=num_speakers, threshold=threshold),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        raise RuntimeError("Sprecher-Modelle fehlen oder sind beschädigt (Ordner models/ löschen).")
    sd = sherpa_onnx.OfflineSpeakerDiarization(config)

    def callback(done, total):
        if progress:
            progress(done / max(total, 1))
        return 0

    result = sd.process(audio, callback=callback).sort_by_start_time()
    order = {}
    turns = []
    for r in result:
        spk = order.setdefault(r.speaker, len(order))
        turns.append((float(r.start), float(r.end), spk))
    return turns


# --- Dateien ------------------------------------------------------------------

def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_lines(session_dir):
    return _load(session_dir / "lines.json", [])


def save_lines(session_dir, lines):
    _save(session_dir / "lines.json", lines)


def load_speakers(session_dir):
    data = _load(session_dir / "speakers.json", {})
    return {"turns": data.get("turns", {}), "names": data.get("names", {})}


def save_turns(session_dir, turns):
    """Speichert neue Sprecherabschnitte. Alte Namen werden verworfen, weil sich
    die Nummerierung geändert haben kann."""
    _save(session_dir / "speakers.json", {"turns": turns, "names": {}})


def set_names(session_dir, names):
    data = load_speakers(session_dir)
    data["names"] = {k: v.strip() for k, v in names.items() if v.strip()}
    _save(session_dir / "speakers.json", data)


# --- Zuordnung und Darstellung ------------------------------------------------

def _speaker_at(turns, start, end):
    """Sprecher mit der grössten Überlappung; sonst der nächste Abschnitt (max. 2 s)."""
    overlap = {}
    for ts, te, spk in turns:
        o = min(end, te) - max(start, ts)
        if o > 0:
            overlap[spk] = overlap.get(spk, 0) + o
    if overlap:
        return max(overlap, key=overlap.get)
    mid = (start + end) / 2
    dist, spk = min(((max(ts - mid, mid - te, 0), spk) for ts, te, spk in turns),
                    default=(None, None))
    return spk if dist is not None and dist <= 2 else None


def _is_room(session_dir):
    return (session_dir / "mic.wav").exists() and not (session_dir / "system.wav").exists()


def _label(line, session_dir, data, room):
    track = line["track"]
    if track == "mic" and not room:
        return LABEL_ME
    base = LABEL_ROOM if room else LABEL_OTHERS
    turns = data["turns"].get(track)
    if not turns or len({t[2] for t in turns}) < 2:
        return base
    spk = _speaker_at(turns, line["start"], line["end"])
    if spk is None:
        return base
    return data["names"].get(f"{track}:{spk}") or f"Sprecher {spk + 1}"


def render_transcript(session_dir):
    """Schreibt transcript.md aus lines.json und speakers.json und gibt den Text zurück."""
    data, room = load_speakers(session_dir), _is_room(session_dir)
    out = []
    for line in sorted(load_lines(session_dir), key=lambda l: l["start"]):
        s = int(line["start"])
        who = _label(line, session_dir, data, room)
        out.append(f"[{s // 60:02d}:{s % 60:02d}] **{who}:** {line['text']}\n\n")
    text = "".join(out)
    (session_dir / "transcript.md").write_text(text, encoding="utf-8")
    return text


def speaker_overview(session_dir):
    """Für den Umbenennen-Dialog: [{key, default, name, sample}] aller erkannten Sprecher."""
    data, lines = load_speakers(session_dir), load_lines(session_dir)
    result = []
    for track, turns in data["turns"].items():
        if len({t[2] for t in turns}) < 2:
            continue
        seen = {}
        for line in sorted((l for l in lines if l["track"] == track), key=lambda l: l["start"]):
            spk = _speaker_at(turns, line["start"], line["end"])
            if spk is not None and spk not in seen:
                s = int(line["start"])
                seen[spk] = f"[{s // 60:02d}:{s % 60:02d}] {line['text'][:80]}"
        for spk in sorted({t[2] for t in turns}):
            key = f"{track}:{spk}"
            result.append({"key": key, "default": f"Sprecher {spk + 1}",
                           "name": data["names"].get(key, ""), "sample": seen.get(spk, "")})
    return result
