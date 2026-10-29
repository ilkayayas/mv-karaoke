"""Make a Whisper-like word list from tests/make_test_clip.py's truth.json, with the kind of
mistakes Whisper makes on sung Turkish: lower case, lost diacritics, a misheard word, a split
word, a dropped word, timing jitter. Used where no Whisper model can be downloaded.

  python tests/sim_asr.py test_out/truth.json test_out/sim_asr.json [SONG.json]

With a song file, its "asr_mistakes" replace the default list below.
"""
import json
import random
import sys
from pathlib import Path

EDITS = {
    "ışıklar": "isiklar",      # lost diacritics
    "Rüzgâr": "ruzgar",
    "söyle": "soyle",
    "kalır": "kaldı",          # misheard
    "yapraklar": "yap raklar",  # split
    "bir": None,               # dropped
}


def main(src: str, dst: str, song: str | None = None, seed: int = 3):
    rng = random.Random(seed)
    edits = EDITS
    if song:
        edits = json.loads(Path(song).read_text(encoding="utf-8")).get("asr_mistakes", EDITS)
    truth = json.loads(Path(src).read_text(encoding="utf-8"))
    out = []
    used = set()
    for line in truth["lines"]:
        for w in line["words"]:
            key = w["text"]
            rep = edits.get(key, key.lower()) if key not in used else key.lower()
            if key in edits:
                used.add(key)          # each mistake once; later repeats are heard right
            if rep is None:
                continue
            parts = rep.split()
            step = (w["end"] - w["start"]) / len(parts)
            for i, p in enumerate(parts):
                j = rng.uniform(-0.05, 0.05)
                out.append({"word": p, "start": round(w["start"] + i * step + j, 3),
                            "end": round(w["start"] + (i + 1) * step + j, 3), "probability": 0.85})
    Path(dst).write_text(json.dumps({"language": "tr", "words": out}, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    print(f"{len(out)} simulated ASR words -> {dst}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
