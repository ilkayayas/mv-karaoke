"""Synthetic tests for lyric parsing and lyric<->ASR timing. No models needed.

The fake 'singer' sings the lyric lines at a known pace; the fake 'ASR' hears them with
mistakes. Every test checks that lyric text is kept verbatim and that word times land
near the truth.
"""
import time
import unittest

from mv_karaoke.lyrics import LyricLine, parse_lyrics
from mv_karaoke.match import AsrWord, align, drop_hallucinations, load_asr_words, sync_lyrics
from mv_karaoke.textnorm import norm, similarity, syllables

SONG = """[Kıta 1]
Gece yarısı yollardayım
IŞIKLARI söndü şehrin
Kimse bilmez nereye gittiğimi

[Nakarat]
Sen gidince bitti her şey
İstanbul'da yağmur yağdı
Kalbim kırık camlar gibi

[Kıta 2]
Sabah olmadan dönerim belki
Bir haber ver bana ne olur
"""


def sing(lines, t0=5.0, line_gap=0.6, extra_gaps=None):
    """Ground truth: [(word, start, end)] per lyric word, plus per-line timing."""
    truth, t = [], t0
    extra_gaps = extra_gaps or {}
    for li, ln in enumerate(lines):
        t += extra_gaps.get(li, 0.0)
        for w in ln.words:
            d = 0.24 * syllables(w, "tr")
            truth.append((li, w, t, t + d))
            t += d + 0.05
        t += line_gap
    return truth


def hear(truth, edits=None, drop_lines=(), extra=None):
    """ASR words from the truth with edits: {global word idx: replacement or None or 'a b'}."""
    edits = edits or {}
    out = []
    for k, (li, w, s, e) in enumerate(truth):
        if li in drop_lines:
            continue
        rep = edits.get(k, w)
        if rep is None:
            continue
        parts = rep.split()
        step = (e - s) / len(parts)
        for p_i, p in enumerate(parts):
            out.append(AsrWord(p.lower(), s + p_i * step, s + (p_i + 1) * step, 0.9))
    for w in extra or []:
        out.append(w)
    out.sort(key=lambda w: w.start)
    return out


def flat_times(doc):
    return [(w["text"], w["start"], w["end"], w["how"]) for ln in doc["lines"] for w in ln["words"]]


class TestTextNorm(unittest.TestCase):
    def test_turkish_casing(self):
        self.assertEqual(norm("IŞIK", "tr"), "ışık")
        self.assertEqual(norm("İstanbul'da", "tr"), "istanbulda")
        self.assertEqual(norm("İstanbul", "en"), "istanbul")
        self.assertGreater(similarity(norm("ISIK", "tr"), norm("ışık", "tr")), 0.9)

    def test_syllables_turkish(self):
        self.assertEqual(syllables("gittiğimi", "tr"), 4)
        self.assertEqual(syllables("şehrin", "tr"), 2)


class TestLyricsParsing(unittest.TestCase):
    def test_sections_and_repeats(self):
        raw = ("[Nakarat]\nSen gidince (x2)\nBitti her şey\n\n[Kıta]\nYağmur var\n(enstrümantal)\n\n"
               "[Nakarat]\n\n[Outro]\nSon söz 2x\n")
        texts = [(l.text, l.origin) for l in parse_lyrics(raw)]
        self.assertEqual([t for t, _ in texts], [
            "Sen gidince", "Sen gidince", "Bitti her şey", "Yağmur var",
            "Sen gidince", "Sen gidince", "Bitti her şey", "Son söz", "Son söz"])
        self.assertEqual(texts[4][1], "section-repeat")

    def test_lrc(self):
        raw = "[ar:Biri]\n[00:12.50]Gece yarısı\n[00:15.00]<00:15.00>Işıklar <00:15.40>söndü\n"
        lines = parse_lyrics(raw)
        self.assertEqual([l.text for l in lines], ["Gece yarısı", "Işıklar söndü"])
        self.assertAlmostEqual(lines[0].lrc_start, 12.5)

    def test_chorus_x2_label(self):
        raw = "[Chorus x2]\nla la\nna na\n[Verse]\nhello\n"
        self.assertEqual([l.text for l in parse_lyrics(raw)], ["la la", "na na", "la la", "na na", "hello"])


class TestSync(unittest.TestCase):
    def setUp(self):
        self.lines = parse_lyrics(SONG)
        self.truth = sing(self.lines, extra_gaps={3: 8.0, 6: 10.0})   # instrumental breaks

    def assert_close(self, doc, tol=0.15, skip=()):
        got = flat_times(doc)
        self.assertEqual([g[0] for g in got], [t[1] for t in self.truth], "lyric text must be verbatim")
        for k, ((_, w, s, _e), (gw, gs, _ge, h)) in enumerate(zip(self.truth, got)):
            if k in skip:
                continue
            self.assertLess(abs(gs - s), tol, f"word {k} '{w}' ({h}): {gs:.2f} vs {s:.2f}")

    def test_perfect(self):
        doc = sync_lyrics(self.lines, hear(self.truth), "tr")
        self.assert_close(doc, tol=0.02)
        self.assertEqual(doc["stats"]["anchored_ratio"], 1.0)

    def test_misheard_words_and_casing(self):
        # ASR writes lower-case "ışıkları" for IŞIKLARI; mishears 3 words
        edits = {3: "işıkları", 8: "nerde", 12: "bidi", 22: "sabahı"}
        doc = sync_lyrics(self.lines, hear(self.truth, edits), "tr")
        self.assert_close(doc, tol=0.3)
        self.assertEqual(flat_times(doc)[3][0], "IŞIKLARI")

    def test_missing_line_is_interpolated_and_flagged(self):
        asr = hear(self.truth, drop_lines={4})            # ASR missed "İstanbul'da yağmur yağdı"
        doc = sync_lyrics(self.lines, asr, "tr", auto_repeats=False)
        line4 = doc["lines"][4]
        self.assertTrue(all(w["how"] == "interp" for w in line4["words"]))
        self.assertIn(4, doc["review"])
        # stays between its neighbours
        self.assertGreater(line4["start"], doc["lines"][3]["end"])
        self.assertLess(line4["end"], doc["lines"][5]["start"])

    def test_missing_line_with_voiced_regions(self):
        k0 = next(k for k, t in enumerate(self.truth) if t[0] == 4)
        k1 = max(k for k, t in enumerate(self.truth) if t[0] == 4)
        # a vocal-activity detector on the stem gives roughly one region per sung line
        voiced = []
        for li in range(len(self.lines)):
            ws = [t for t in self.truth if t[0] == li]
            voiced.append((ws[0][2] - 0.1, ws[-1][3] + 0.1))
        asr = hear(self.truth, drop_lines={4})
        doc = sync_lyrics(self.lines, asr, "tr", voiced=voiced, auto_repeats=False)
        self.assertLess(abs(doc["lines"][4]["start"] - self.truth[k0][2]), 0.2)
        self.assertLess(abs(doc["lines"][4]["end"] - self.truth[k1][3]), 0.2)

    def test_adlibs_and_hallucinations(self):
        extra = [AsrWord("altyazı", 0.5, 1.0), AsrWord("m.k.", 1.0, 1.4),       # intro hallucination
                 AsrWord("yeah", 13.2, 13.5), AsrWord("hey", 13.6, 13.9)]       # ad-libs in the gap
        doc = sync_lyrics(self.lines, hear(self.truth, extra=extra), "tr")
        self.assert_close(doc, tol=0.05)

    def test_split_where_one_piece_looks_like_the_word(self):
        # "yap raklar": the second piece alone is similar enough to anchor; the pair must win
        k = next(k for k, t in enumerate(self.truth) if t[1] == "yollardayım")
        doc = sync_lyrics(self.lines, hear(self.truth, {k: "yol lardayım"}), "tr")
        self.assert_close(doc, tol=0.05)

    def test_split_and_merged_words(self):
        k = next(k for k, t in enumerate(self.truth) if t[1] == "gittiğimi")
        m = next(k for k, t in enumerate(self.truth) if t[1] == "dönerim")
        doc = sync_lyrics(self.lines, hear(self.truth, {k: "gittiği mi", m: "döne rim"}), "tr")
        self.assert_close(doc, tol=0.05)

    def test_omitted_chorus_repeat_is_inserted(self):
        # lyrics page writes the chorus once; the singer sings it twice in a row
        ch = [l for l in self.lines if l.section == "Nakarat"]
        sung = self.lines[:6] + [LyricLine(l.text, l.section) for l in ch] + self.lines[6:]
        truth = sing(sung, extra_gaps={3: 8.0, 9: 10.0})
        doc = sync_lyrics(self.lines, hear(truth), "tr")
        self.assertEqual(doc["stats"]["inserted_repeats"], 3)
        got = [w for ln in doc["lines"] for w in ln["words"]]
        self.assertEqual([w["text"] for w in got], [t[1] for t in truth])
        for w, t in zip(got, truth):
            self.assertLess(abs(w["start"] - t[2]), 0.05)
        self.assertTrue(set(range(6, 9)) <= set(doc["review"]))

    def test_without_repair_repeat_does_not_shift_later_lines(self):
        ch = [l for l in self.lines if l.section == "Nakarat"]
        sung = self.lines[:6] + [LyricLine(l.text, l.section) for l in ch] + self.lines[6:]
        truth = sing(sung, extra_gaps={3: 8.0, 9: 10.0})
        doc = sync_lyrics(self.lines, hear(truth), "tr", auto_repeats=False)
        # verse 2 must still land on the second (later) singing, not get pulled back
        tv2 = next(t for t in truth if t[1] == "Sabah")[2]
        self.assertLess(abs(doc["lines"][6]["start"] - tv2), 0.05)

    def test_load_stable_ts_json(self):
        data = {"segments": [{"words": [{"word": " Gece", "start": 1.0, "end": 1.3, "probability": 0.8}]}]}
        w = load_asr_words(data)
        self.assertEqual((w[0].text, w[0].prob), ("Gece", 0.8))

    def test_speed(self):
        lines = [LyricLine(l.text) for l in self.lines] * 12            # ~400 words
        truth = sing(lines)
        t = time.time()
        doc = sync_lyrics(lines, hear(truth), "tr", auto_repeats=False)
        self.assertLess(time.time() - t, 15)
        self.assertEqual(doc["stats"]["anchored_ratio"], 1.0)


if __name__ == "__main__":
    unittest.main()
