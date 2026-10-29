"""Speech recognition on the isolated vocal: word timestamps (always) and a lyrics draft
(when the user has no lyrics file).

Backends, picked automatically (stable-ts drives all of them):
  mlx             Apple Silicon (mlx-whisper)          pip install "stable-ts[mlx]"
  faster-whisper  Windows / Linux CPU or CUDA (int8)   pip install "stable-ts[fw]"
  whisper         reference PyTorch implementation      pip install stable-ts

The vocal is cut along its own pauses into chunks of at most ~26 s and each chunk is
transcribed on its own (no conditioning on earlier text). Whisper works in 30 s windows and
otherwise drops or repeats whole phrases across long instrumental gaps.
"""
from __future__ import annotations

import json
import platform
from pathlib import Path

from .match import AsrWord, drop_hallucinations

DEFAULT_MODEL = "large-v3-turbo"


# --------------------------------------------------------------------------- #
# Where is someone singing? (numpy only)
# --------------------------------------------------------------------------- #
def voiced_regions(vocal_wav: Path, frame: float = 0.02, rel_db: float = -32.0,
                   min_on: float = 0.25, min_off: float = 0.35) -> list[tuple[float, float]]:
    """Regions where the vocal stem is active. Threshold relative to the loud part of the
    stem (95th percentile), so it adapts to quiet and loud mixes. Separation residue
    (bleed) sits well below it."""
    import numpy as np
    import soundfile as sf

    x, sr = sf.read(str(vocal_wav), dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    hop = max(1, int(sr * frame))
    n = len(x) // hop
    if n == 0:
        return []
    rms = np.sqrt(np.mean(x[: n * hop].reshape(n, hop) ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    ref = float(np.percentile(db, 95))
    on = db > max(ref + rel_db, -60.0)
    # close short gaps, then drop short blips
    regions, start = [], None
    for i, v in enumerate(on):
        t = i * frame
        if v and start is None:
            start = t
        elif not v and start is not None:
            regions.append([start, t])
            start = None
    if start is not None:
        regions.append([start, n * frame])
    merged = []
    for s, e in regions:
        if merged and s - merged[-1][1] < min_off:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(round(s, 3), round(e, 3)) for s, e in merged if e - s >= min_on]


def chunk_regions(regions: list[tuple[float, float]], max_len: float = 26.0, pad: float = 0.4):
    chunks: list[list[float]] = []
    for s, e in regions:
        if chunks and e - chunks[-1][0] <= max_len:
            chunks[-1][1] = e
        else:
            chunks.append([s, e])
    out = []
    for s, e in chunks:
        s, e = max(0.0, s - pad), e + pad
        while e - s > max_len * 1.3:
            out.append((s, s + max_len))
            s += max_len
        out.append((s, e))
    return out


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
def pick_backend(prefer: str = "auto") -> str:
    if prefer != "auto":
        return prefer
    import importlib.util as u
    if platform.system() == "Darwin" and platform.machine() == "arm64" and u.find_spec("mlx_whisper"):
        return "mlx"
    if u.find_spec("faster_whisper"):
        return "faster-whisper"
    return "whisper"


def load_model(name: str = DEFAULT_MODEL, backend: str = "auto", device: str = "auto"):
    import stable_whisper
    backend = pick_backend(backend)
    if backend == "mlx":
        return stable_whisper.load_mlx_whisper(name), backend
    if backend == "faster-whisper":
        dev = "cuda" if device in ("auto", "cuda") and _has_cuda() else "cpu"
        return stable_whisper.load_faster_whisper(name, device=dev,
                                                  compute_type="float16" if dev == "cuda" else "int8"), backend
    dev = "cuda" if device in ("auto", "cuda") and _has_cuda() else "cpu"
    return stable_whisper.load_model(name, device=dev), backend


def _has_cuda() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


def _segments(result) -> list:
    return result.segments if hasattr(result, "segments") else result.get("segments", [])


def transcribe(vocal_wav: Path, language: str | None, model_name: str = DEFAULT_MODEL,
               backend: str = "auto", device: str = "auto", log=print,
               regions: list[tuple[float, float]] | None = None,
               separate_regions: bool = False) -> tuple[list[AsrWord], str | None]:
    """Word-timestamped transcript of the vocal stem. Returns (words, detected language).
    separate_regions: every region is its own chunk (used to re-listen to short parts)."""
    import numpy as np
    import soundfile as sf

    regions = regions if regions is not None else voiced_regions(vocal_wav)
    if not regions:
        return [], language
    chunks = list(regions) if separate_regions else chunk_regions(regions)
    model, backend = load_model(model_name, backend, device)
    log(f"speech recognition: Whisper {model_name} ({backend}), {len(chunks)} chunks")

    x, sr = sf.read(str(vocal_wav), dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    if sr != 16000:
        # simple polyphase resample to 16 kHz without extra dependencies
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(sr, 16000)
        x = resample_poly(x, 16000 // g, sr // g).astype(np.float32)
        sr = 16000
    words: list[AsrWord] = []
    lang = language
    for cs, ce in chunks:
        piece = x[int(cs * sr): int(ce * sr)]
        if len(piece) < sr // 2:
            continue
        peak = float(np.abs(piece).max()) or 1.0
        piece = (piece * (0.9 / peak)).astype(np.float32)
        res = model.transcribe(piece, language=lang, word_timestamps=True, vad=False, regroup=True,
                               condition_on_previous_text=False, verbose=None)
        if lang is None:
            lang = getattr(res, "language", None) or (res.get("language") if isinstance(res, dict) else None)
        for seg in _segments(res):
            for w in (seg.words if hasattr(seg, "words") else seg.get("words", [])):
                text = (w.word if hasattr(w, "word") else w["word"]).strip()
                if not text:
                    continue
                s = float(w.start if hasattr(w, "start") else w["start"]) + cs
                e = float(w.end if hasattr(w, "end") else w["end"]) + cs
                p = float(getattr(w, "probability", None) or (w.get("probability", 1.0) if isinstance(w, dict) else 1.0))
                words.append(AsrWord(text, s, e, p))
    words.sort(key=lambda w: w.start)
    return drop_hallucinations(words, lang), lang


def save_words(words: list[AsrWord], lang: str | None, path: Path, meta: dict | None = None):
    doc = {"language": lang, "meta": meta or {},
           "words": [{"word": w.text, "start": round(w.start, 3), "end": round(w.end, 3),
                      "probability": round(w.prob, 3)} for w in words]}
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def load_meta(path: Path) -> dict:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        return doc.get("meta", {}) if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def load_words(path: Path) -> tuple[list[AsrWord], str | None]:
    from .match import load_asr_words
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return load_asr_words(doc.get("words", doc)), doc.get("language") if isinstance(doc, dict) else None


# --------------------------------------------------------------------------- #
# Lyrics draft from the transcript
# --------------------------------------------------------------------------- #
def draft_lyrics(words: list[AsrWord], max_chars: int = 42, pause: float = 0.7, stanza_pause: float = 4.0) -> str:
    """Group transcript words into sung lines (break on pauses and length), blank line between
    stanzas (long pauses). Short fragments are glued to a neighbour so lines read like lyrics."""
    lines: list[list[AsrWord]] = []
    for w in words:
        if lines:
            cur = lines[-1]
            gap = w.start - cur[-1].end
            length = len(" ".join(x.text for x in cur)) + 1 + len(w.text)
            if gap <= pause and length <= max_chars:
                cur.append(w)
                continue
        lines.append([w])
    merged: list[list[AsrWord]] = []
    for ln in lines:
        if merged:
            prev = merged[-1]
            gap = ln[0].start - prev[-1].end
            text_len = len(" ".join(x.text for x in prev + ln))
            if (len(ln) <= 2 or len(prev) <= 2) and gap <= 1.5 and text_len <= max_chars + 8:
                prev.extend(ln)
                continue
        merged.append(list(ln))
    out = []
    for i, ln in enumerate(merged):
        if i and ln[0].start - merged[i - 1][-1].end >= stanza_pause:
            out.append("")
        out.append(" ".join(w.text for w in ln))
    return "\n".join(out) + "\n"
