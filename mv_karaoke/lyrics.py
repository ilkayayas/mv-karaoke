"""Parse user/database lyrics into sung lines. The lyrics are treated as the truth:
their words, spelling and line breaks are what appears on screen.

Handles what lyric pages usually contain:
  * LRC timestamps ([01:23.45], <01:23.45>) - kept as line hints, removed from text
  * section labels: [Chorus], [Nakarat], (Verse 2), "Nakarat:" ...
  * a label repeated without its lines ("[Nakarat]" alone the second time)
    -> the earlier block of that section is copied in
  * repeat marks: "(x2)", "x2", "2x", "(2 kez)" after a line; "[Nakarat x2]" on a section
  * other bracketed notes ("(instrumental)", "[Solo]") are dropped
  * blank lines -> stanza breaks (kept for layout)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

LRC_TAG = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")
LRC_WORD_TAG = re.compile(r"<\d{1,2}:\d{2}(?:[.:]\d{1,3})?>")
LRC_META = re.compile(r"^\[[a-zA-Z#]+:[^\]]*\]$")
REPEAT_TAIL = re.compile(
    r"\s*[\(\[]?\s*(?:[x×]\s*(\d)|(\d)\s*[x×]|(\d)\s*(?:kez|kere|defa|times))\s*[\)\]]?\s*$", re.I)
SECTION_NAMES = [
    "pre-chorus", "prechorus", "post-chorus", "chorus", "verse", "bridge", "hook", "intro", "outro",
    "refrain", "interlude", "ön nakarat", "nakarat", "kıta", "köprü", "giriş", "çıkış", "bölüm", "rap",
]
SECTION_ALIASES = {"chorus": "chorus", "nakarat": "chorus", "refrain": "chorus", "hook": "chorus",
                   "kıta": "verse", "köprü": "bridge", "ön nakarat": "pre-chorus", "prechorus": "pre-chorus"}
BRACKETED = re.compile(r"^[\[\(\{](.{0,48})[\]\)\}]$")


@dataclass
class LyricLine:
    text: str
    section: str | None = None
    stanza: int = 0
    lrc_start: float | None = None
    origin: str = "lyrics"   # lyrics | repeat-mark | section-repeat | inserted-repeat
    words: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.words:
            self.words = self.text.split()


def _repeat(text: str) -> tuple[str, int]:
    m = REPEAT_TAIL.search(text)
    if not m or m.start() == 0:
        return text, 1
    n = int(next(g for g in m.groups() if g))
    return text[: m.start()].rstrip(" -–,"), max(1, min(n, 8))


def _section_label(body: str) -> tuple[str, str, int] | None:
    """('Nakarat 2', 'chorus', times) for a section-label line, else None.
    Accepts "[Nakarat]", "(Chorus x2)", "Nakarat:", "[Verse 2: Artist]"."""
    m = BRACKETED.match(body)
    inner = m.group(1).strip() if m else body.rstrip(":").strip() if body.endswith(":") else None
    if inner is None:
        return None
    inner, times = _repeat(inner)
    low = inner.lower()
    for name in SECTION_NAMES:
        if low.startswith(name):
            rest = low[len(name):]
            if not rest or not rest[0].isalpha():   # "verse 2", "chorus: artist", not "versed"
                key = SECTION_ALIASES.get(name, name)
                return inner.split(":")[0].strip(), key, times
    return None


def parse_lyrics(raw: str, expand_repeats: bool = True) -> list[LyricLine]:
    """Lyric text or LRC -> sung lines, with section repeats expanded."""
    # 1. tokenise into events: ("label", label, key, times) | ("line", text, lrc_t, times) | ("blank",)
    events = []
    for raw_line in raw.splitlines():
        s = raw_line.strip()
        if LRC_META.match(s):
            continue
        stamps = LRC_TAG.findall(s)
        s = LRC_WORD_TAG.sub("", LRC_TAG.sub("", s)).strip()
        if not s:
            events.append(("blank",))
            continue
        lab = _section_label(s)
        if lab:
            events.append(("label", lab[0], lab[1], lab[2]))
            continue
        body, times = _repeat(s)
        if BRACKETED.match(body):
            continue
        t = None
        if stamps:
            mm, ss, frac = stamps[0]
            t = int(mm) * 60 + int(ss) + int((frac or "0").ljust(3, "0")[:3]) / 1000
        events.append(("line", body, t, times))

    # 2. group into blocks under their labels
    blocks: list[dict] = []          # {label, key, times, lines:[(text,t,times)], stanza}
    cur = {"label": None, "key": None, "times": 1, "lines": [], "stanza": 0}
    stanza = 0
    for ev in events:
        if ev[0] == "blank":
            if cur["lines"]:
                blocks.append(cur)
                stanza += 1
                cur = {"label": cur["label"], "key": cur["key"], "times": 1, "lines": [], "stanza": stanza,
                       "continued": True}
            continue
        if ev[0] == "label":
            if cur["lines"] or (cur["label"] is not None and not cur.get("continued")):
                blocks.append(cur)
                stanza += 1
            cur = {"label": ev[1], "key": ev[2], "times": ev[3], "lines": [], "stanza": stanza}
            continue
        cur["lines"].append(ev[1:])
    if cur["lines"] or (cur.get("label") and not cur.get("continued")):
        blocks.append(cur)

    # 3. expand
    first_block: dict[str, list] = {}
    out: list[LyricLine] = []
    for b in blocks:
        key = b["key"]
        if b["lines"]:
            if key and key not in first_block and not b.get("continued"):
                first_block[key] = b["lines"]
            reps = b["times"] if expand_repeats else 1
            for r in range(reps):
                for text, t, times in b["lines"]:
                    n = times if expand_repeats else 1
                    for k in range(n):
                        origin = "lyrics" if (r == 0 and k == 0) else ("section-repeat" if r else "repeat-mark")
                        out.append(LyricLine(text, b["label"], b["stanza"], t if (r == 0 and k == 0) else None,
                                             origin))
        elif key and key in first_block and expand_repeats:
            for _ in range(b["times"]):
                for text, _t, times in first_block[key]:
                    for _k in range(times):
                        out.append(LyricLine(text, b["label"], b["stanza"], None, "section-repeat"))
    return out
