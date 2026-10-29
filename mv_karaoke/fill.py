"""Find the parts of the song where singing is heard but no words are known, and fill them.

  uncovered(voiced, words)   voiced regions with (almost) no word in them
  gap_pass(...)              Whisper again on each such region on its own (short, padded):
                             Whisper often skips isolated phrases and the last repeats of a
                             chorus when they sit inside a longer chunk
  fill_gaps(asr, extra, ...) add words from a second source (YouTube subtitles) only where
                             the first source has nothing, so no part is counted twice
  missing_report(...)        after timing: where singing is heard but no lyric is placed
"""
from __future__ import annotations

from .match import AsrWord

Region = tuple[float, float]


def coverage(t0: float, t1: float, spans: list[Region]) -> float:
    return sum(max(0.0, min(t1, e) - max(t0, s)) for s, e in spans)


def uncovered(voiced: list[Region], word_spans: list[Region], min_len: float = 1.0,
              max_cover: float = 0.25) -> list[Region]:
    """Voiced regions (at least min_len s) of which at most max_cover is covered by words."""
    spans = sorted(word_spans)
    out = []
    for s, e in voiced:
        if e - s >= min_len and coverage(s, e, spans) <= max_cover * (e - s):
            out.append((s, e))
    return out


def inside(w: AsrWord, regions: list[Region], slack: float = 0.3) -> bool:
    mid = (w.start + w.end) / 2
    return any(s - slack <= mid <= e + slack for s, e in regions)


def merge_words(base: list[AsrWord], extra: list[AsrWord], regions: list[Region]) -> tuple[list[AsrWord], int]:
    """base + the words of extra that fall in regions (and don't overlap a base word)."""
    spans = [(w.start, w.end) for w in base]
    add = [w for w in extra if inside(w, regions) and coverage(w.start, w.end, spans) < 0.5 * (w.end - w.start)]
    return sorted(base + add, key=lambda w: w.start), len(add)


def gap_pass(vocal_wav, words: list[AsrWord], voiced: list[Region], transcribe_fn, log=print,
             pad: float = 0.6) -> tuple[list[AsrWord], list[Region], int]:
    """Re-run recognition on each uncovered region separately. transcribe_fn(regions) -> words."""
    gaps = uncovered(voiced, [(w.start, w.end) for w in words])
    if not gaps:
        return words, [], 0
    log(f"recognition left {len(gaps)} sung parts empty ({sum(e - s for s, e in gaps):.0f} s): "
        "listening to each of them again")
    padded = [(max(0.0, s - pad), e + pad) for s, e in gaps]
    new = transcribe_fn(padded)
    merged, n = merge_words(words, new, gaps)
    return merged, gaps, n


def fill_gaps(words: list[AsrWord], extra: list[AsrWord], voiced: list[Region]) -> tuple[list[AsrWord], int]:
    gaps = uncovered(voiced, [(w.start, w.end) for w in words])
    if not gaps or not extra:
        return words, 0
    return merge_words(words, extra, gaps)


def missing_report(doc: dict, voiced: list[Region], words: list[AsrWord], min_len: float = 1.5) -> list[dict]:
    """Sung parts with no lyric placed, with what recognition heard there (may be empty)."""
    spans = [(w["start"], w["end"]) for ln in doc["lines"] for w in ln["words"]]
    out = []
    for s, e in uncovered(voiced, spans, min_len=min_len):
        heard = " ".join(w.text for w in words if s - 0.2 <= (w.start + w.end) / 2 <= e + 0.2)
        out.append({"start": round(s, 2), "end": round(e, 2), "heard": heard})
    return out


def clock(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:04.1f}"
