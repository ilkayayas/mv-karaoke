"""Gap pass, gap filling, missing-part report, YouTube subtitle parsing/selection/offset and the
lyrics draft refresh. All text here is made up for the tests; no network, no models."""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

from mv_karaoke.fill import fill_gaps, gap_pass, merge_words, missing_report, uncovered
from mv_karaoke.match import AsrWord
from mv_karaoke.subs import (Cue, Subs, draft_from_manual, fetch, measure_offset, parse_json3, parse_vtt,
                             pick_track, shift, to_words)


def W(text, s, e, p=0.9):
    return AsrWord(text, s, e, p)


class TestFill(unittest.TestCase):
    def setUp(self):
        self.voiced = [(1.0, 5.0), (10.0, 14.0), (20.0, 23.0)]
        self.words = [W("bir", 1.0, 1.5), W("iki", 1.6, 2.4), W("üç", 2.5, 4.8),
                      W("dört", 20.1, 21.0), W("beş", 21.1, 22.9)]

    def test_uncovered(self):
        self.assertEqual(uncovered(self.voiced, [(w.start, w.end) for w in self.words]), [(10.0, 14.0)])

    def test_merge_only_inside_gaps(self):
        extra = [W("altı", 10.2, 11.0), W("yedi", 11.2, 12.0), W("iki", 1.6, 2.4), W("dışarı", 16.0, 17.0)]
        merged, n = merge_words(self.words, extra, [(10.0, 14.0)])
        self.assertEqual(n, 2)
        self.assertEqual([w.text for w in merged], ["bir", "iki", "üç", "altı", "yedi", "dört", "beş"])

    def test_gap_pass_relistens_each_gap(self):
        asked = []

        def fake(regions):
            asked.append(regions)
            return [W("sekiz", 10.5, 11.5), W("dokuz", 11.6, 12.8)]
        merged, gaps, n = gap_pass(None, self.words, self.voiced, fake, log=lambda *_: None)
        self.assertEqual(gaps, [(10.0, 14.0)])
        self.assertEqual(asked, [[(9.4, 14.6)]])          # padded
        self.assertEqual(n, 2)
        self.assertEqual(len(merged), 7)

    def test_gap_pass_nothing_to_do(self):
        full = self.words + [W("on", 10.0, 13.9)]
        merged, gaps, n = gap_pass(None, full, self.voiced, lambda r: self.fail("called"), log=lambda *_: None)
        self.assertEqual((gaps, n), ([], 0))

    def test_fill_gaps(self):
        merged, n = fill_gaps(self.words, [W("on", 10.3, 11.0), W("bir", 1.0, 1.4)], self.voiced)
        self.assertEqual(n, 1)

    def test_missing_report(self):
        doc = {"lines": [{"words": [{"start": w.start, "end": w.end} for w in self.words]}]}
        rep = missing_report(doc, self.voiced, self.words + [W("duyulan", 11.0, 12.0)])
        self.assertEqual(len(rep), 1)
        self.assertEqual((rep[0]["start"], rep[0]["end"], rep[0]["heard"]), (10.0, 14.0, "duyulan"))


AUTO_JSON3 = {"events": [
    {"tStartMs": 0, "dDurationMs": 4000, "segs": [{"utf8": "[Müzik]"}]},
    {"tStartMs": 5000, "dDurationMs": 3000, "segs": [
        {"utf8": "deneme"}, {"utf8": " satırı", "tOffsetMs": 600}, {"utf8": " burada", "tOffsetMs": 1300}]},
    {"tStartMs": 6500, "dDurationMs": 10, "aAppend": 1, "segs": [{"utf8": "\n"}]},
    {"tStartMs": 9000, "dDurationMs": 2000, "segs": [{"utf8": "ikinci"}, {"utf8": " satır", "tOffsetMs": 700}]},
]}
MANUAL_JSON3 = {"events": [
    {"tStartMs": 5000, "dDurationMs": 2500, "segs": [{"utf8": "♪ Deneme satırı burada ♪"}]},
    {"tStartMs": 9000, "dDurationMs": 2000, "segs": [{"utf8": "İkinci satır"}]},
    {"tStartMs": 16000, "dDurationMs": 2000, "segs": [{"utf8": "Yeni kıta başlar"}]},
]}
AUTO_VTT = """WEBVTT
Kind: captions

00:00:05.000 --> 00:00:07.000 align:start
deneme<00:00:05.600><c> satırı</c><00:00:06.300><c> burada</c>

00:00:07.000 --> 00:00:07.010
deneme satırı burada

00:00:09.000 --> 00:00:11.000
ikinci<00:00:09.700><c> satır</c>
"""


class TestSubsParsing(unittest.TestCase):
    def test_json3_auto_words(self):
        cues = parse_json3(AUTO_JSON3)
        self.assertEqual([c.text for c in cues], ["deneme satırı burada", "ikinci satır"])
        self.assertEqual([w[0] for w in cues[0].words], ["deneme", "satırı", "burada"])
        self.assertAlmostEqual(cues[0].words[1][1], 5.6)
        self.assertAlmostEqual(cues[0].words[0][2], 5.6)

    def test_json3_manual_lines(self):
        cues = parse_json3(MANUAL_JSON3)
        self.assertEqual(cues[0].text, "Deneme satırı burada")
        self.assertEqual(cues[0].words, [])
        self.assertEqual((cues[0].start, cues[0].end), (5.0, 7.5))

    def test_vtt_inline_times(self):
        cues = parse_vtt(AUTO_VTT)
        self.assertEqual(cues[0].text, "deneme satırı burada")
        self.assertAlmostEqual(cues[0].words[2][1], 6.3)
        self.assertEqual(len(cues), 2)                    # the rolling repeat is merged
        self.assertAlmostEqual(cues[0].end, 7.01)

    def test_manual_words_spread_over_cue(self):
        ws = to_words(Subs("manual", "tr", parse_json3(MANUAL_JSON3)), "tr")
        first = [w for w in ws if w.start < 8]
        self.assertEqual([w.text for w in first], ["Deneme", "satırı", "burada"])
        self.assertTrue(5.0 <= first[0].start < first[1].start < first[2].start < 7.5)
        self.assertTrue(all(w.prob < 0.5 for w in ws))

    def test_draft_from_manual(self):
        d = draft_from_manual(Subs("manual", "tr", parse_json3(MANUAL_JSON3)))
        self.assertEqual(d, "Deneme satırı burada\nİkinci satır\n\nYeni kıta başlar\n")


class TestTrackChoice(unittest.TestCase):
    def info(self, manual=(), auto=()):
        f = lambda: [{"ext": "vtt", "url": "u.vtt"}, {"ext": "json3", "url": "u.json3"}]
        return {"subtitles": {k: f() for k in manual}, "automatic_captions": {k: f() for k in auto}}

    def test_manual_first(self):
        self.assertEqual(pick_track(self.info(["tr"], ["tr-orig"]), "tr")[:2], ("manual", "tr"))

    def test_auto_original_language(self):
        self.assertEqual(pick_track(self.info([], ["en", "tr", "tr-orig"]), "tr")[:2], ("auto", "tr-orig"))
        self.assertEqual(pick_track(self.info([], ["tr"]), "tr")[2]["ext"], "json3")

    def test_other_language_only(self):
        self.assertIsNone(pick_track(self.info(["en", "de"], ["en-orig"]), "tr"))


class TestOffset(unittest.TestCase):
    def setUp(self):
        vocab = ["kelime", "deneme", "satırı", "burada", "sabah", "akşam", "yollar", "deniz", "dağlar", "rüzgar"]
        self.asr = [W(vocab[i % 10], 3 + i * 0.8, 3.6 + i * 0.8) for i in range(60)]

    def test_constant_shift_found(self):
        subs = [W(w.text, w.start - 0.7, w.end - 0.7) for w in self.asr]
        off, n = measure_offset(subs, self.asr, "tr")
        self.assertAlmostEqual(off, 0.7, places=2)
        fixed = shift(subs, off)
        self.assertAlmostEqual(fixed[10].start, self.asr[10].start, places=3)

    def test_unrelated_text_rejected(self):
        subs = [W(f"other{i}", 3 + i, 3.5 + i) for i in range(40)]
        self.assertIsNone(measure_offset(subs, self.asr, "tr")[0])


class TestFetch(unittest.TestCase):
    def setUp(self):
        test = self
        self.urls = []

        class YDL:
            def __init__(self, opts):
                test.assertTrue(opts.get("skip_download"))

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                return {"subtitles": {}, "automatic_captions": {
                    "tr-orig": [{"ext": "json3", "url": "https://x/tr.json3"}],
                    "de": [{"ext": "json3", "url": "https://x/de.json3"}]}}

            def urlopen(self, url):
                test.urls.append(url)
                return types.SimpleNamespace(read=lambda: json.dumps(AUTO_JSON3).encode())

        fake = types.ModuleType("yt_dlp")
        fake.YoutubeDL = YDL
        self._saved = sys.modules.get("yt_dlp")
        sys.modules["yt_dlp"] = fake

    def tearDown(self):
        if self._saved is not None:
            sys.modules["yt_dlp"] = self._saved
        else:
            sys.modules.pop("yt_dlp", None)

    def test_fetch_and_cache(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            s = fetch("https://www.youtube.com/watch?v=abcdefghijk", work, "tr", log=lambda *_: None)
            self.assertEqual((s.kind, s.lang, len(s.cues)), ("auto", "tr-orig", 2))
            self.assertEqual(self.urls, ["https://x/tr.json3"])
            s2 = fetch("https://www.youtube.com/watch?v=abcdefghijk", work, "tr", log=lambda *_: None)
            self.assertEqual(len(s2.cues), 2)
            self.assertEqual(len(self.urls), 1)                 # cached


class TestLyricsDraftRefresh(unittest.TestCase):
    def test_untouched_draft_is_refreshed_edited_file_is_kept(self):
        from mv_karaoke.cli import prepare_lyrics_file
        q = lambda *_: None
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            f = work / "lyrics.txt"
            self.assertTrue(prepare_lyrics_file(f, "a b\n", "a b\n", work, log=q))
            # recognition found more: the untouched draft is replaced, the old one kept aside
            self.assertTrue(prepare_lyrics_file(f, "a b\nc d\n", "a b\n", work, log=q))
            self.assertEqual(f.read_text(encoding="utf-8"), "a b\nc d\n")
            self.assertEqual((work / "lyrics.prev.txt").read_text(encoding="utf-8"), "a b\n")
            # the user edits it: never overwritten again
            f.write_text("A B\nc d\n", encoding="utf-8")
            self.assertFalse(prepare_lyrics_file(f, "x y\n", "a b\nc d\n", work, log=q))
            self.assertEqual(f.read_text(encoding="utf-8"), "A B\nc d\n")

    def test_old_draft_without_stamp_is_recognised(self):
        from mv_karaoke.cli import prepare_lyrics_file
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            f = work / "lyrics.txt"
            f.write_text("eski taslak\n", encoding="utf-8")       # written by an older version
            self.assertTrue(prepare_lyrics_file(f, "yeni taslak\n", "eski taslak\n", work, log=lambda *_: None))
            self.assertEqual(f.read_text(encoding="utf-8"), "yeni taslak\n")


if __name__ == "__main__":
    unittest.main()
