"""The video's own YouTube subtitles as a second source of words and times.

Two kinds, very different:
  manual  uploaded by the channel. Text usually right, times per line (cue) only.
          -> best draft for lyrics.txt; words spread over each cue as timing evidence
  auto    YouTube's speech recognition on the ORIGINAL mix (vocals included). Text as
          error-prone as Whisper, but per-word times, and it hears parts our vocal stem lost.

Only the song's language is used (manual "tr", auto "tr-orig"/"tr"); translations are not.
Subtitle times are checked against Whisper's: a constant offset is measured and removed,
and subtitles that don't agree with the singing (another version, a translation) are
ignored. Nothing is looked up anywhere else: these are the video's own subtitles.
"""
from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from .match import AsrWord
from .textnorm import norm, syllables

NOISE = re.compile(r"\[[^\]]*\]|\([^)]*(müzik|music|alkış|applause)[^)]*\)|[♪♫]", re.I)


@dataclass
class Cue:
    start: float
    end: float
    text: str
    words: list[tuple[str, float, float]] = field(default_factory=list)   # auto captions only


@dataclass
class Subs:
    kind: str            # manual | auto
    lang: str
    cues: list[Cue]


def clean(text: str) -> str:
    text = NOISE.sub(" ", text.replace("\n", " "))
    return re.sub(r"\s+", " ", text).strip(" -–")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_json3(data: str | dict) -> list[Cue]:
    """YouTube's json3: events[{tStartMs, dDurationMs, segs[{utf8, tOffsetMs?}]}]."""
    doc = json.loads(data) if isinstance(data, str) else data
    events = [e for e in doc.get("events", []) if e.get("segs")]
    cues = []
    for k, ev in enumerate(events):
        t0 = ev.get("tStartMs", 0) / 1000
        dur = ev.get("dDurationMs", 0) / 1000
        segs = ev["segs"]
        text = clean("".join(s.get("utf8", "") for s in segs))
        if not text:
            continue
        end = t0 + dur if dur else t0 + 2.0
        words = []
        if any("tOffsetMs" in s for s in segs[1:]) or (len(segs) > 1 and "tOffsetMs" in segs[0]):
            starts = [t0 + s.get("tOffsetMs", 0) / 1000 for s in segs]
            nxt = events[k + 1].get("tStartMs", 0) / 1000 if k + 1 < len(events) else end
            for i, s in enumerate(segs):
                w = clean(s.get("utf8", ""))
                if not w:
                    continue
                we = starts[i + 1] if i + 1 < len(segs) else min(end, nxt)
                words.append((w, starts[i], max(we, starts[i] + 0.08)))
        cues.append(Cue(t0, end, text, words))
    return cues


VTT_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[.,](\d{3})|(\d{2}):(\d{2})[.,](\d{3})")


def _vtt_t(s: str) -> float:
    m = VTT_TIME.match(s.strip())
    if not m:
        raise ValueError(s)
    if m.group(1):
        h, mi, se, ms = (int(x) for x in m.group(1, 2, 3, 4))
    else:
        h, (mi, se, ms) = 0, (int(x) for x in m.group(5, 6, 7))
    return h * 3600 + mi * 60 + se + ms / 1000


def parse_vtt(text: str) -> list[Cue]:
    """WebVTT / SRT cues; inline <00:00:01.500> word times are used when present."""
    cues = []
    blocks = re.split(r"\n\s*\n", text.replace("\r", ""))
    for b in blocks:
        lines = [l for l in b.split("\n") if l.strip()]
        idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if idx is None:
            continue
        a, _, rest = lines[idx].partition("-->")
        t0, t1 = _vtt_t(a), _vtt_t(rest.split()[0])
        raw = " ".join(lines[idx + 1:])
        words = []
        if "<" in raw and VTT_TIME.search(raw):
            parts = re.split(r"<(\d{2}:\d{2}:\d{2}[.,]\d{3})>", raw)
            t = t0
            seq = []
            for i, p in enumerate(parts):
                if i % 2 == 1:
                    t = _vtt_t(p)
                    continue
                for w in clean(re.sub(r"<[^>]+>", "", p)).split():
                    seq.append([w, t])
            for i, (w, ws) in enumerate(seq):
                we = seq[i + 1][1] if i + 1 < len(seq) and seq[i + 1][1] > ws else t1
                words.append((w, ws, max(we, ws + 0.08)))
        txt = clean(re.sub(r"<[^>]+>", "", raw))
        if not txt:
            continue
        if cues and cues[-1].text == txt and abs(cues[-1].end - t0) < 0.05:   # rolling duplicates
            cues[-1].end = t1
            continue
        cues.append(Cue(t0, t1, txt, words))
    return cues


def parse_file(path: Path) -> list[Cue]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return parse_json3(text) if path.suffix == ".json3" else parse_vtt(text)


# --------------------------------------------------------------------------- #
# Download (yt-dlp, no video)
# --------------------------------------------------------------------------- #
def pick_track(info: dict, lang: str) -> tuple[str, str, dict] | None:
    """(kind, lang key, format) for the song's language: manual first, then auto-captions."""
    def best(formats):
        for ext in ("json3", "vtt", "srv3"):
            for f in formats:
                if f.get("ext") == ext and f.get("url"):
                    return f
        return None

    manual = info.get("subtitles") or {}
    for key in sorted(manual):
        if key == lang or key.startswith(lang + "-"):
            f = best(manual[key])
            if f:
                return "manual", key, f
    auto = info.get("automatic_captions") or {}
    for key in (f"{lang}-orig", lang):        # the original-language track, not a translation
        if key in auto:
            f = best(auto[key])
            if f:
                return "auto", key, f
    return None


def fetch(url: str, work: Path, lang: str, log=print) -> Subs | None:
    """Download the video's subtitles in `lang` into work/subs/ (cached). None when there are none."""
    d = work / "subs"
    meta_file = d / "subs.json"
    if meta_file.exists():
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        if not meta.get("file"):
            return None
        return Subs(meta["kind"], meta["lang"], parse_file(d / meta["file"]))
    d.mkdir(parents=True, exist_ok=True)
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                               "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            pick = pick_track(info, lang)
            if not pick:
                meta_file.write_text(json.dumps({"file": None}), encoding="utf-8")
                log(f"no '{lang}' subtitles on this video")
                return None
            kind, key, fmt = pick
            data = ydl.urlopen(fmt["url"]).read().decode("utf-8", errors="replace")
    except Exception as e:  # network trouble or rate limits: subtitles are optional
        log(f"subtitles skipped: {str(e).strip()[:200]}")
        return None
    fname = f"{kind}.{key}.{fmt['ext']}"
    (d / fname).write_text(data, encoding="utf-8")
    meta_file.write_text(json.dumps({"file": fname, "kind": kind, "lang": key}), encoding="utf-8")
    subs = Subs(kind, key, parse_file(d / fname))
    log(f"subtitles: {'uploaded' if kind == 'manual' else 'YouTube automatic'} ({key}), {len(subs.cues)} lines")
    return subs


# --------------------------------------------------------------------------- #
# Using them
# --------------------------------------------------------------------------- #
def to_words(subs: Subs, lang: str | None) -> list[AsrWord]:
    """Timed words. Auto captions carry their own word times; manual cues are shared out
    over their words by syllables, with a low probability (line-level evidence only)."""
    out = []
    for c in subs.cues:
        if c.words:
            out += [AsrWord(w, s, e, 0.6) for w, s, e in c.words]
            continue
        ws = c.text.split()
        if not ws:
            continue
        span = max(0.1, c.end - c.start - 0.15)
        weights = [syllables(w, lang) for w in ws]
        total, acc = sum(weights), 0.0
        for w, k in zip(ws, weights):
            s = c.start + acc / total * span
            acc += k
            out.append(AsrWord(w, s, c.start + acc / total * span, 0.4))
    return out


def measure_offset(sub_words: list[AsrWord], asr: list[AsrWord], lang: str | None,
                   window: float = 6.0) -> tuple[float | None, int]:
    """Median (asr - subtitle) start difference over words that read the same, near in time.
    Returns (offset, support); offset None when they don't agree enough to be trusted."""
    by_word: dict[str, list[float]] = {}
    for a in asr:
        k = norm(a.text, lang)
        if len(k) >= 3:
            by_word.setdefault(k, []).append(a.start)
    diffs = []
    for w in sub_words:
        k = norm(w.text, lang)
        cands = [t - w.start for t in by_word.get(k, []) if abs(t - w.start) <= window]
        if cands:
            diffs.append(min(cands, key=abs))
    need = max(5, int(0.15 * len(sub_words)))
    if len(diffs) < need:
        return None, len(diffs)
    off = statistics.median(diffs)
    spread = statistics.median(abs(d - off) for d in diffs)
    if spread > 1.0:                      # no consistent shift: different version or edit
        return None, len(diffs)
    return round(off, 3), len(diffs)


def shift(words: list[AsrWord], off: float) -> list[AsrWord]:
    return [AsrWord(w.text, w.start + off, w.end + off, w.prob) for w in words]


def draft_from_manual(subs: Subs, stanza_pause: float = 4.0) -> str:
    """lyrics.txt draft from uploaded subtitles: one cue per line, blank line at long pauses."""
    out, prev_end = [], None
    for c in subs.cues:
        if prev_end is not None and c.start - prev_end >= stanza_pause:
            out.append("")
        if not out or out[-1] != c.text:
            out.append(c.text)
        prev_end = c.end
    return "\n".join(out).strip() + "\n"
