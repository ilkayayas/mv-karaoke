"""Language-aware word normalisation used to compare lyric words with ASR words.

Two levels:
  norm(word)  - case-folded (Turkish-aware: I->ı, İ->i), punctuation and
                apostrophes removed. "İstanbul'da" -> "istanbulda", "IŞIK" -> "ışık".
  fold(word)  - norm() plus diacritics folded to ASCII-ish letters, so ASR
                spelling slips still compare: "ışık" -> "isik", "güneş" -> "gunes".
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache

# Languages whose dotted/dotless i casing differs from the Unicode default.
DOTLESS_I_LANGS = {"tr", "az", "crh", "tt", "kk"}

_TR_UPPER_TO_LOWER = str.maketrans({"I": "ı", "İ": "i"})
_FOLD = str.maketrans({
    "ı": "i", "ş": "s", "ğ": "g", "ç": "c", "ö": "o", "ü": "u",
    "â": "a", "î": "i", "û": "u", "ß": "ss", "æ": "ae", "ø": "o", "œ": "oe",
})
_APOSTROPHES = "'’‘`´ʼ"
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)
VOWELS = set("aeıioöuüâîûyàáäèéëìíïòóôõùúåæøœ")


def casefold(text: str, lang: str | None = None) -> str:
    text = unicodedata.normalize("NFC", text)
    if lang and lang.lower().split("-")[0] in DOTLESS_I_LANGS:
        text = text.translate(_TR_UPPER_TO_LOWER)
    else:
        # Outside Turkic languages "İ" still means "i" (common in pasted lyrics)
        text = text.replace("İ", "i")
    return text.lower().replace("i̇", "i")  # drop a stray combining dot


def norm(word: str, lang: str | None = None) -> str:
    w = casefold(word, lang)
    for a in _APOSTROPHES:
        w = w.replace(a, "")
    return _NON_WORD.sub("", w).replace("_", "")


@lru_cache(maxsize=65536)
def fold(normed: str) -> str:
    w = normed.translate(_FOLD)
    w = unicodedata.normalize("NFKD", w)
    return "".join(ch for ch in w if not unicodedata.combining(ch))


def syllables(word: str, lang: str | None = None) -> int:
    """Vowel-group count. Exact for Turkish (one vowel per syllable), close enough
    elsewhere; only used to share time between words in proportion."""
    w = norm(word, lang)
    if not w:
        return 1
    count, prev = 0, False
    for ch in w:
        v = ch in VOWELS
        if v and not (prev and lang and lang.split("-")[0] not in DOTLESS_I_LANGS):
            count += 1
        prev = v
    return max(1, count)


@lru_cache(maxsize=262144)
def similarity(a: str, b: str) -> float:
    """Similarity of two norm()-ed words in [0, 1]."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    fa, fb = fold(a), fold(b)
    if fa == fb:
        return 0.96
    r = SequenceMatcher(None, fa, fb, autojunk=False).ratio()
    # one-letter-off long words ("seviyorum" vs "sevi̇yorum", "gönlüm" vs "gönlün")
    return r
