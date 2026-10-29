"""Give every word of the (trusted) lyrics a time, using ASR words only as timing evidence.

The lyrics decide WHAT is on screen; Whisper (or any word-timestamped ASR) only says
WHEN. Nothing Whisper writes is ever shown.

Pipeline
  1. normalise both sides (Turkish-aware, see textnorm)
  2. global alignment of lyric words vs ASR words (Gotoh DP, affine gaps):
       - fuzzy word similarity, short words weigh less (one "ve"/"the" can't anchor a line)
       - 1:2 and 2:1 merges ("gidiyorum" ~ "gidi" + "yorum", "bilmiyorum" ~ "bil miyorum")
       - long gaps are cheap, so whole lines Whisper missed, ad-libs and hallucinations
         don't drag neighbouring words out of place
  3. anchors: strong matches; weak matches (misheard words) only when sandwiched
     between strong ones; lone short-word matches dropped
  4. optional repeat repair: a long run of unmatched ASR words that reads like a lyric
     line means the lyric page left a repeat out (chorus sung twice, written once):
     the line is inserted (origin "inserted-repeat") and alignment re-run
  5. timing for unanchored words: inside the window between anchors, narrowed to where
     singing was heard (unmatched ASR words, else voiced regions of the vocal stem),
     shared in proportion to syllables
  6. confidence per word and line; lines needing a human look are listed in "review"
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher

from .lyrics import LyricLine
from .textnorm import fold, norm, similarity, syllables

HARD = 0.78              # similarity for an anchor
SOFT = 0.45              # below this a pairing is a plain mismatch
GAP_OPEN_L, GAP_EXT_L = -1.4, -0.22   # lyric word with no ASR word
GAP_OPEN_A, GAP_EXT_A = -1.0, -0.12   # ASR word with no lyric word (ad-lib, hallucination)
GAP_OPEN_A_IN, GAP_EXT_A_IN = -3.0, -0.35   # ... inside a lyric line: a line is sung in one go
SEC_PER_SYL = 0.26       # natural singing rate used when a window has no other evidence
MIN_WORD = 0.08

# Things Whisper writes over silence/music. Dropped before alignment.
HALLUCINATIONS = {
    "altyazı mk", "altyazı m k", "abone olmayı unutmayın", "izlediğiniz için teşekkürler",
    "izlediğiniz için teşekkür ederim", "thank you", "thanks for watching", "thank you for watching",
    "subscribe", "please subscribe", "music", "müzik", "applause", "alkış",
}


@dataclass
class AsrWord:
    text: str
    start: float
    end: float
    prob: float = 1.0


@dataclass
class TimedWord:
    text: str
    start: float
    end: float
    conf: float
    how: str            # anchor | soft | split | interp


@dataclass
class TimedLine:
    text: str
    start: float
    end: float
    conf: float
    section: str | None
    stanza: int
    origin: str
    words: list[TimedWord] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# ASR input
# --------------------------------------------------------------------------- #
def load_asr_words(data) -> list[AsrWord]:
    """Accepts a flat [{word,start,end,probability?}] list, a stable-ts / whisper /
    faster-whisper result dict ({segments:[{words:[...]}]}), or a path to such JSON."""
    if isinstance(data, str):
        with open(data, encoding="utf-8") as f:
            data = json.load(f)
    if isinstance(data, dict):
        flat = [w for seg in data.get("segments", []) for w in seg.get("words", [])]
    else:
        flat = data
    out = []
    for w in flat:
        text = (w.get("word") or w.get("text") or "").strip()
        if not text or w.get("start") is None or w.get("end") is None:
            continue
        out.append(AsrWord(text, float(w["start"]), float(w["end"]),
                           float(w.get("probability", w.get("prob", 1.0)) or 1.0)))
    out.sort(key=lambda w: w.start)
    return out


def drop_hallucinations(words: list[AsrWord], lang: str | None) -> list[AsrWord]:
    """Remove known filler phrases (as whole runs of up to 5 words)."""
    keep = [True] * len(words)
    normed = [norm(w.text, lang) for w in words]
    for i in range(len(words)):
        for n in range(5, 0, -1):
            if i + n <= len(words) and " ".join(normed[i:i + n]) in HALLUCINATIONS:
                # only when isolated in time: real lyrics can contain "thank you"
                before = words[i - 1].end if i else -99
                after = words[i + n].start if i + n < len(words) else 1e9
                if words[i].start - before > 1.5 and after - words[i + n - 1].end > 1.5:
                    for k in range(i, i + n):
                        keep[k] = False
                break
    return [w for w, k in zip(words, keep) if k]


# --------------------------------------------------------------------------- #
# Alignment
# --------------------------------------------------------------------------- #
def _weight(a: str) -> float:
    return min(1.0, 0.3 + 0.14 * len(a))


def _pair(sim: float, a: str) -> float:
    w = _weight(a)
    if sim >= HARD:
        return (1.0 + sim) * w
    if sim >= SOFT:
        return 0.1 * w
    return -0.9 * w


NEG = -1e18


def align(lw: list[str], aw: list[str], line_of: list[int] | None = None) -> list[tuple]:
    """Gotoh global alignment with merges. Returns ops in order:
       ("m", i, j, sim)          lyric i  <-> asr j
       ("m12", i, j, sim)        lyric i  <-> asr j, j+1
       ("m21", i, j, sim)        lyric i, i+1 <-> asr j
       ("dl", i)                 lyric i unmatched
       ("da", j)                 asr j unmatched
    Leading/trailing gaps cost only the extension penalty (semi-global). With line_of
    (lyric line index per word), extra ASR words inside a lyric line cost more than
    between lines, so a line is not split across two different singings of it."""
    n, m = len(lw), len(aw)
    M = [[NEG] * (m + 1) for _ in range(n + 1)]
    X = [[NEG] * (m + 1) for _ in range(n + 1)]   # lyric gap (consumes i)
    Y = [[NEG] * (m + 1) for _ in range(n + 1)]   # asr gap   (consumes j)
    bM = [[None] * (m + 1) for _ in range(n + 1)]
    bX = [[0] * (m + 1) for _ in range(n + 1)]
    bY = [[0] * (m + 1) for _ in range(n + 1)]
    M[0][0] = 0.0
    for i in range(1, n + 1):
        X[i][0] = i * GAP_EXT_L
        bX[i][0] = 1
    for j in range(1, m + 1):
        Y[0][j] = j * GAP_EXT_A
        bY[0][j] = 2
    sim = similarity
    cat_a = [aw[j] + aw[j + 1] if j + 1 < m else None for j in range(m)]
    # ASR-gap cost after lyric word i-1 (i.e. before lyric word i)
    in_line_gap = [(GAP_OPEN_A, GAP_EXT_A)] * (n + 1)
    if line_of:
        for i in range(1, n):
            if line_of[i - 1] == line_of[i]:
                in_line_gap[i] = (GAP_OPEN_A_IN, GAP_EXT_A_IN)
    cat_l = [lw[i] + lw[i + 1] if i + 1 < n else None for i in range(n)]

    def best(i, j):
        a, b, c = M[i][j], X[i][j], Y[i][j]
        if a >= b and a >= c:
            return a, 0
        return (b, 1) if b >= c else (c, 2)

    for i in range(1, n + 1):
        li = lw[i - 1]
        Mi, Xi, Yi, Mp, Xp, Yp = M[i], X[i], Y[i], M[i - 1], X[i - 1], Y[i - 1]
        for j in range(1, m + 1):
            # match states
            s = sim(li, aw[j - 1])
            v, st = best(i - 1, j - 1)
            cand, back = v + _pair(s, li), ("m", st, s)
            # merges only when neither piece is already a good match on its own
            # (otherwise "gittiğimi" + "sen" would swallow the next word)
            if j >= 2 and cat_a[j - 2] is not None and sim(li, aw[j - 2]) < HARD:
                s2 = sim(li, cat_a[j - 2])
                if s2 >= HARD and s2 > s + 0.08:
                    v2, st2 = best(i - 1, j - 2)
                    c2 = v2 + _pair(s2, li) * 1.1
                    if c2 > cand:
                        cand, back = c2, ("m12", st2, s2)
            if i >= 2 and cat_l[i - 2] is not None and sim(lw[i - 2], aw[j - 1]) < HARD:
                s3 = sim(cat_l[i - 2], aw[j - 1])
                if s3 >= HARD and s3 > s + 0.08:
                    v3, st3 = best(i - 2, j - 1)
                    c3 = v3 + _pair(s3, cat_l[i - 2]) * 1.1
                    if c3 > cand:
                        cand, back = c3, ("m21", st3, s3)
            Mi[j], bM[i][j] = cand, back
            # lyric gap: trailing column (j == m) is cheap
            o, e = (GAP_EXT_L, GAP_EXT_L) if j == m else (GAP_OPEN_L, GAP_EXT_L)
            c0, c1, c2_ = Mp[j] + o, Xp[j] + e, Yp[j] + o
            if c0 >= c1 and c0 >= c2_:
                Xi[j], bX[i][j] = c0, 0
            elif c1 >= c2_:
                Xi[j], bX[i][j] = c1, 1
            else:
                Xi[j], bX[i][j] = c2_, 2
            # asr gap: trailing row (i == n) is cheap, inside a line expensive
            o, e = (GAP_EXT_A, GAP_EXT_A) if i == n else in_line_gap[i]
            c0, c1, c2_ = Mi[j - 1] + o, Xi[j - 1] + o, Yi[j - 1] + e
            if c0 >= c1 and c0 >= c2_:
                Yi[j], bY[i][j] = c0, 0
            elif c2_ >= c1:
                Yi[j], bY[i][j] = c2_, 2
            else:
                Yi[j], bY[i][j] = c1, 1

    # traceback
    ops = []
    i, j = n, m
    _, state = best(n, m)
    while i > 0 or j > 0:
        if state == 0:
            kind, prev, s = bM[i][j]
            if kind == "m":
                ops.append(("m", i - 1, j - 1, s)); i, j = i - 1, j - 1
            elif kind == "m12":
                ops.append(("m12", i - 1, j - 2, s)); i, j = i - 1, j - 2
            else:
                ops.append(("m21", i - 2, j - 1, s)); i, j = i - 2, j - 1
            state = prev
        elif state == 1:
            ops.append(("dl", i - 1))
            state = bX[i][j]; i -= 1
        else:
            ops.append(("da", j - 1))
            state = bY[i][j]; j -= 1
        if i == 0 and j > 0:
            state = 2
        elif j == 0 and i > 0:
            state = 1
    ops.reverse()
    return ops


# --------------------------------------------------------------------------- #
# Timing
# --------------------------------------------------------------------------- #
def _intersect(window: tuple[float, float], regions: list[tuple[float, float]]) -> list[tuple[float, float]]:
    a, b = window
    out = [(max(a, s), min(b, e)) for s, e in regions if e > a and s < b]
    return [(s, e) for s, e in out if e - s > 0.2]   # slivers are a neighbour line's padding


def _spread(weights: list[int], spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Place len(weights) words across possibly disjoint spans, in proportion to weights."""
    total_w = sum(weights)
    total_t = sum(e - s for s, e in spans)
    out, acc = [], 0.0

    def at(x):  # x in [0, total_t] -> absolute time
        for s, e in spans:
            if x <= e - s + 1e-9:
                return s + x
            x -= e - s
        return spans[-1][1]

    for w in weights:
        a = acc / total_w * total_t
        acc += w
        b = acc / total_w * total_t
        out.append((at(a), at(b)))
    return out


def time_words(lines: list[LyricLine], asr: list[AsrWord], lang: str | None = None,
               voiced: list[tuple[float, float]] | None = None, duration: float | None = None,
               ops: list[tuple] | None = None) -> tuple[list[TimedLine], dict]:
    flat = [(li, w) for li, ln in enumerate(lines) for w in ln.words]
    lw = [norm(w, lang) for _, w in flat]
    aw = [norm(w.text, lang) for w in asr]
    if ops is None:
        ops = align(lw, aw, [li for li, _ in flat])
    n = len(flat)
    times: list[tuple[float, float] | None] = [None] * n
    conf = [0.0] * n
    how = ["interp"] * n
    strong = [False] * n
    soft: dict[int, tuple[float, float, float]] = {}
    asr_used = [False] * len(asr)

    for op in ops:
        k = op[0]
        if k == "m":
            _, i, j, s = op
            a = asr[j]
            if s >= HARD:
                times[i], conf[i], how[i], strong[i] = (a.start, a.end), s * a.prob, "anchor", True
                asr_used[j] = True
            elif s >= SOFT:
                soft[i] = (a.start, a.end, s * a.prob)
                asr_used[j] = True
        elif k == "m12":
            _, i, j, s = op
            times[i] = (asr[j].start, asr[j + 1].end)
            conf[i], how[i], strong[i] = s * min(asr[j].prob, asr[j + 1].prob), "anchor", True
            asr_used[j] = asr_used[j + 1] = True
        elif k == "m21":
            _, i, j, s = op
            a = asr[j]
            w1, w2 = syllables(flat[i][1], lang), syllables(flat[i + 1][1], lang)
            mid = a.start + (a.end - a.start) * w1 / (w1 + w2)
            times[i], times[i + 1] = (a.start, mid), (mid, a.end)
            for x in (i, i + 1):
                conf[x], how[x], strong[x] = s * a.prob * 0.9, "split", True
            asr_used[j] = True

    # lone short anchors (a single "ve", "bir", "the") are not trusted
    for i in range(n):
        if strong[i] and len(lw[i]) <= 3:
            nb = any(strong[k] for k in range(max(0, i - 2), min(n, i + 3)) if k != i)
            if not nb:
                times[i], strong[i], how[i], conf[i] = None, False, "interp", 0.0
    # weak matches only between strong anchors close on both sides
    for i, (s0, e0, c) in soft.items():
        if times[i] is not None:
            continue
        left = any(strong[k] for k in range(max(0, i - 3), i))
        right = any(strong[k] for k in range(i + 1, min(n, i + 4)))
        if left and right:
            times[i], conf[i], how[i] = (s0, e0), 0.5 * c, "soft"

    # monotonic anchors: drop any anchor that goes back in time
    last = -1.0
    for i in range(n):
        if times[i] is not None:
            if times[i][0] < last - 0.05:
                times[i], how[i], conf[i], strong[i] = None, "interp", 0.0, False
            else:
                last = times[i][1]

    # windows for unanchored runs
    song_end = duration or (asr[-1].end + 2.0 if asr else 0.0)
    i = 0
    while i < n:
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < n and times[j] is None:
            j += 1
        t0 = times[i - 1][1] if i > 0 else 0.0
        t1 = times[j][0] if j < n else song_end
        weights = [syllables(flat[k][1], lang) for k in range(i, j)]
        need = sum(weights) * SEC_PER_SYL
        evidence = [a for a, used in zip(asr, asr_used) if not used and a.start >= t0 - 0.05 and a.end <= t1 + 0.05]
        spans: list[tuple[float, float]] = []
        if evidence:
            spans = [(max(t0, evidence[0].start), min(t1, evidence[-1].end))]
        elif voiced:
            spans = _intersect((t0, t1), voiced)
        if not spans or sum(e - s for s, e in spans) < 0.3 * need:
            # no evidence: sit next to the anchor on the same line, else after the previous line
            starts_line = i == 0 or flat[i][0] != flat[i - 1][0]
            ends_into_anchor = j < n and flat[j][0] == flat[j - 1][0]
            span_len = min(t1 - t0, need)
            if starts_line and ends_into_anchor and i > 0:
                spans = [(max(t0, t1 - span_len - 0.05), t1 - 0.05)]
            elif i == 0:
                spans = [(max(0.0, t1 - need - 0.3), max(0.05, t1 - 0.3))]
            else:
                gap = 0.4 if starts_line else 0.05
                a = min(t0 + gap, t1)
                spans = [(a, min(t1, a + need))]
        spans = [(s, e) for s, e in spans if e > s] or [(t0, max(t1, t0 + MIN_WORD * len(weights)))]
        for k, (s0, e0) in zip(range(i, j), _spread(weights, spans)):
            times[k] = (s0, e0)
            conf[k] = 0.25 if evidence or voiced else 0.1
        i = j

    # tidy: strictly increasing, minimum length, no overlap
    for k in range(n):
        s0, e0 = times[k]
        if k:
            ps, pe = times[k - 1]
            s0 = max(s0, ps + 0.02)
            if pe > s0:
                times[k - 1] = (ps, max(ps + 0.02, s0))
        times[k] = (s0, max(e0, s0 + MIN_WORD))

    # assemble lines
    out: list[TimedLine] = []
    for li, ln in enumerate(lines):
        idx = [k for k in range(n) if flat[k][0] == li]
        if not idx:
            continue
        ws = [TimedWord(flat[k][1], round(times[k][0], 3), round(times[k][1], 3), round(conf[k], 3), how[k])
              for k in idx]
        lc = sum(w.conf for w in ws) / len(ws)
        out.append(TimedLine(ln.text, ws[0].start, ws[-1].end, round(lc, 3), ln.section, ln.stanza, ln.origin, ws))

    anchored = sum(1 for h in how if h in ("anchor", "split"))
    stats = {
        "lyric_words": n,
        "asr_words": len(asr),
        "anchored": anchored,
        "soft": sum(1 for h in how if h == "soft"),
        "interpolated": sum(1 for h in how if h == "interp"),
        "anchored_ratio": round(anchored / n, 3) if n else 0.0,
        "asr_used_ratio": round(sum(asr_used) / len(asr), 3) if asr else 0.0,
        "unmatched_asr": " ".join(a.text for a, u in zip(asr, asr_used) if not u)[:2000],
    }
    return out, stats


# --------------------------------------------------------------------------- #
# Repeat repair
# --------------------------------------------------------------------------- #
def _unmatched_runs(ops: list[tuple], min_words: int) -> list[tuple[int, int, int]]:
    """(lyric index where the run sits, asr start, asr end exclusive) for runs of 'da'."""
    runs, cur, li = [], None, 0
    for op in ops:
        if op[0] == "da":
            if cur is None:
                cur = [li, op[1], op[1] + 1]
            else:
                cur[2] = op[1] + 1
            continue
        if cur and cur[2] - cur[1] >= min_words:
            runs.append(tuple(cur))
        cur = None
        if op[0] in ("m", "m12"):
            li = op[1] + 1
        elif op[0] == "m21":
            li = op[1] + 2
        elif op[0] == "dl":
            li = op[1] + 1
    if cur and cur[2] - cur[1] >= min_words:
        runs.append(tuple(cur))
    return runs


def repair_repeats(lines: list[LyricLine], asr: list[AsrWord], lang: str | None,
                   min_words: int = 4, threshold: float = 0.72) -> tuple[list[LyricLine], int]:
    flat_line_of = [li for li, ln in enumerate(lines) for _ in ln.words]
    lw = [norm(w, lang) for ln in lines for w in ln.words]
    aw = [norm(w.text, lang) for w in asr]
    ops = align(lw, aw, flat_line_of)
    runs = _unmatched_runs(ops, min_words)
    if not runs:
        return lines, 0
    line_keys = [fold("".join(norm(w, lang) for w in ln.words)) for ln in lines]
    inserts: dict[int, list[LyricLine]] = {}     # insert before line index
    for li_pos, a0, a1 in runs:
        p = a0
        found = []
        while p < a1:
            best, best_r, best_len = None, 0.0, 1
            for k, ln in enumerate(lines):
                L = len(ln.words)
                if L < 2:
                    continue
                for span in (L, L - 1, L + 1):          # ASR may split/merge or drop a word
                    if span < 1 or p + span > a1:
                        continue
                    cand = fold("".join(aw[p:p + span]))
                    r = SequenceMatcher(None, cand, line_keys[k], autojunk=False).ratio()
                    if r > best_r + 1e-9:
                        best, best_r, best_len = k, r, span
            if best is not None and best_r >= threshold:
                found.append(lines[best])
                p += best_len
            else:
                p += 1
        if found:
            # insert at the start of the line that follows the run
            line_idx = flat_line_of[li_pos] if li_pos < len(flat_line_of) else len(lines)
            if 0 < li_pos < len(flat_line_of) and flat_line_of[li_pos - 1] == line_idx:
                line_idx += 1   # run sits inside a line: put the repeat after that line
            inserts.setdefault(line_idx, []).extend(found)
    if not inserts:
        return lines, 0
    out, count = [], 0
    for k, ln in enumerate(lines + [None]):
        for src in inserts.get(k, []):
            out.append(LyricLine(src.text, src.section, src.stanza, None, "inserted-repeat"))
            count += 1
        if ln is not None:
            out.append(ln)
    return out, count


# --------------------------------------------------------------------------- #
def sync_lyrics(lines: list[LyricLine], asr: list[AsrWord], lang: str | None = None,
                voiced: list[tuple[float, float]] | None = None, duration: float | None = None,
                auto_repeats: bool = True, review_below: float = 0.45) -> dict:
    """Main entry: trusted lyric lines + ASR words -> timed lyrics document."""
    asr = drop_hallucinations(asr, lang)
    inserted = 0
    if auto_repeats and asr:
        lines, inserted = repair_repeats(lines, asr, lang)
    timed, stats = time_words(lines, asr, lang, voiced, duration)
    stats["inserted_repeats"] = inserted
    review = [i for i, ln in enumerate(timed)
              if ln.conf < review_below or ln.origin == "inserted-repeat"]
    return {
        "language": lang,
        "lines": [asdict(ln) for ln in timed],
        "stats": stats,
        "review": review,
    }
