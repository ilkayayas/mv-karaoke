"""Command line for the lyric<->ASR matcher.

  python -m mv_karaoke.sync_cli --lyrics lyrics.txt --asr whisper.json --lang tr --out lyrics.json

--asr is a word-timestamped ASR result: a stable-ts / whisper / faster-whisper result
JSON ({segments:[{words:[...]}]}) or a flat [{word,start,end}] list.
--voiced optionally gives vocal-activity regions as JSON [[start, end], ...].
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .lyrics import parse_lyrics
from .match import load_asr_words, sync_lyrics


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Time trusted lyrics with ASR word timestamps.")
    ap.add_argument("--lyrics", required=True, help="lyrics text or .lrc (the text shown on screen)")
    ap.add_argument("--asr", required=True, help="ASR result JSON with word timestamps")
    ap.add_argument("--lang", default=None, help="ISO code, e.g. tr, en (affects casing rules)")
    ap.add_argument("--voiced", help="JSON [[start, end], ...] of regions where the vocal stem is active")
    ap.add_argument("--duration", type=float, help="song length in seconds")
    ap.add_argument("--no-repeat-repair", action="store_true",
                    help="don't insert lyric lines the page left out but the singer repeated")
    ap.add_argument("--no-expand", action="store_true", help="don't expand (x2) / repeated section labels")
    ap.add_argument("--out", default="lyrics.json")
    a = ap.parse_args(argv)

    lines = parse_lyrics(Path(a.lyrics).read_text(encoding="utf-8-sig"), expand_repeats=not a.no_expand)
    if not lines:
        print("lyrics file has no sung lines", file=sys.stderr)
        return 1
    asr = load_asr_words(a.asr)
    voiced = [tuple(r) for r in json.loads(Path(a.voiced).read_text(encoding="utf-8"))] if a.voiced else None
    doc = sync_lyrics(lines, asr, a.lang, voiced=voiced, duration=a.duration,
                      auto_repeats=not a.no_repeat_repair)
    Path(a.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    s = doc["stats"]
    print(f"{s['lyric_words']} lyric words: {s['anchored']} anchored, {s['soft']} soft, "
          f"{s['interpolated']} interpolated ({s['anchored_ratio']:.0%} anchored); "
          f"{s['inserted_repeats']} repeated lines inserted")
    if doc["review"]:
        print("check these lines:")
        for i in doc["review"]:
            ln = doc["lines"][i]
            print(f"  {ln['start']:7.2f}s  [{ln['origin']}] {ln['text']}")
    print(f"-> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
