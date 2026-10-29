"""Randomised stress benchmark for the lyric<->ASR matcher (not a unit test).

  python tests/bench_match.py

For each noise level: N random songs; the fake ASR mishears, drops, splits words,
drops whole lines, adds ad-libs and jitters times. Reports how many lyric words
start within 0.15 s / 0.30 s of the truth, and how often a word lands in the wrong line.
"""
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mv_karaoke.lyrics import LyricLine          # noqa: E402
from mv_karaoke.match import AsrWord, sync_lyrics  # noqa: E402
from test_match import sing                       # noqa: E402

VOCAB = ("gece yarısı yol ışık şehir kimse bilmez nereye sen gidince bitti her şey yağmur kalbim kırık "
         "cam gibi sabah olmadan dönerim belki haber ver bana ne olur aşk sevda gözlerin uzak yakın "
         "deniz rüzgar yıldız hatıra sessiz bekle git gel unutma ağla gül söyle duy").split()


def mishear(w, rng):
    ops = [lambda s: s[:-1] or s, lambda s: s + rng.choice("ıiea"), lambda s: rng.choice(VOCAB),
           lambda s: s.replace("ı", "i").replace("ş", "s"), lambda s: s[: max(1, len(s) // 2)]]
    return rng.choice(ops)(w)


def make_song(rng, n_lines=24):
    lines = [LyricLine(" ".join(rng.choice(VOCAB) for _ in range(rng.randint(3, 7)))) for _ in range(6)]
    chorus = lines[2:4]
    song = []
    for k in range(n_lines // 2):
        song.append(LyricLine(" ".join(rng.choice(VOCAB) for _ in range(rng.randint(3, 7)))))
        if k % 3 == 2:
            song.extend(LyricLine(c.text) for c in chorus)
    return song


def run(level, songs=30, seed=1):
    rng = random.Random(seed)
    errs, wrong_line, total = [], 0, 0
    for _ in range(songs):
        lines = make_song(rng)
        gaps = {i: rng.choice([0, 0, 0, 6.0, 12.0]) for i in range(len(lines))}
        truth = sing(lines, extra_gaps=gaps)
        drop_lines = {i for i in range(len(lines)) if rng.random() < level * 0.3}
        asr = []
        for li, w, s, e in truth:
            if li in drop_lines or rng.random() < level * 0.3:
                continue
            j = lambda: rng.uniform(-0.1, 0.1)
            if rng.random() < level:
                w = mishear(w, rng)
            if rng.random() < level * 0.2 and len(w) > 5:
                h = len(w) // 2
                asr += [AsrWord(w[:h], s + j(), (s + e) / 2), AsrWord(w[h:], (s + e) / 2, e + j())]
            else:
                asr.append(AsrWord(w, s + j(), e + j()))
            if rng.random() < level * 0.1:
                asr.append(AsrWord(rng.choice(["yeah", "hey", "oh", "baby"]), e + 0.05, e + 0.3))
        asr.sort(key=lambda a: a.start)
        doc = sync_lyrics(lines, asr, "tr")
        got = [w for ln in doc["lines"] for w in ln["words"]]
        # inserted repeats change the word list: compare only when counts agree
        if len(got) != len(truth):
            wrong_line += len(truth)
            total += len(truth)
            continue
        for g, (li, _w, s, _e) in zip(got, truth):
            errs.append(abs(g["start"] - s))
            total += 1
        line_start = {}
        for li, _w, s, _e in truth:
            line_start.setdefault(li, s)
        for li, ln in enumerate(doc["lines"]):
            nxt = line_start.get(li + 1, 1e9)
            if ln["start"] >= nxt:
                wrong_line += len(ln["words"])
    within = lambda t: sum(e <= t for e in errs) / total
    return {
        "noise": level, "words": total,
        "<=0.15s": f"{within(0.15):.1%}", "<=0.30s": f"{within(0.30):.1%}",
        "median_ms": round(1000 * statistics.median(errs)) if errs else None,
        "wrong_line_or_count": f"{wrong_line / total:.1%}",
    }


if __name__ == "__main__":
    for level in (0.0, 0.1, 0.2, 0.3):
        print(run(level))
