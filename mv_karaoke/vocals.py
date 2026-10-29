"""How the vocals are removed. There is deliberately NO default: removing every vocal can
leave holes in songs built on harmonies and backing vocals, while removing only the lead
keeps those but sounds less 'empty'. Which one sounds right depends on the song, so the
user chooses before anything is processed.

  all   every vocal (lead, harmonies, backing, choirs) is removed     -> Demucs
  lead  only the lead singer is removed; backing vocals stay in      -> MelBand-RoFormer karaoke model
"""
from __future__ import annotations

import sys
from enum import Enum


class VocalMode(str, Enum):
    ALL = "all"
    LEAD = "lead"


MODE_TEXT = {
    VocalMode.ALL: {
        "tr": ("Tüm vokalleri sil",
               "Ana vokal, arka vokaller, armoniler ve korolar silinir. En temiz karaoke; ama vokal "
               "ağırlıklı şarkılarda boşluk hissi ve silme izleri (metalik ses) olabilir."),
        "en": ("Remove all vocals",
               "Lead, backing vocals, harmonies and choirs are removed. Cleanest karaoke, but "
               "vocal-heavy songs can sound hollow or show artefacts."),
    },
    VocalMode.LEAD: {
        "tr": ("Yalnızca ana vokali sil",
               "Sadece ana şarkıcı silinir; arka vokaller ve korolar müzikte kalır. Profesyonel "
               "karaokelerdeki gibi daha dolgun bir altyapı; arka vokaller bazen ana vokale eşlik eder."),
        "en": ("Remove the lead vocal only",
               "Only the lead singer is removed; backing vocals and choirs stay. Fuller, like "
               "professional karaoke tracks; backing vocals may still double some lead lines."),
    },
}


class VocalModeRequired(SystemExit):
    pass


def parse_mode(value: str | None) -> VocalMode | None:
    if value is None:
        return None
    v = value.strip().lower()
    aliases = {"all": VocalMode.ALL, "tum": VocalMode.ALL, "tüm": VocalMode.ALL, "hepsi": VocalMode.ALL,
               "lead": VocalMode.LEAD, "ana": VocalMode.LEAD, "main": VocalMode.LEAD}
    if v not in aliases:
        raise ValueError(f"unknown vocal mode '{value}' (use: all | lead)")
    return aliases[v]


def describe_modes(lang: str = "tr") -> str:
    lang = "tr" if lang.startswith("tr") else "en"
    rows = []
    for i, mode in enumerate(VocalMode, 1):
        title, text = MODE_TEXT[mode][lang]
        rows.append(f"  {i}) {mode.value:<4}  {title}\n           {text}")
    return "\n".join(rows)


def ask_mode(lang: str = "tr", stream_in=None, stream_out=None) -> VocalMode:
    """Ask on the terminal. Raises VocalModeRequired when there is no terminal to ask on,
    so an unattended run never silently picks a mode."""
    stream_in = stream_in or sys.stdin
    stream_out = stream_out or sys.stdout
    if not (hasattr(stream_in, "isatty") and stream_in.isatty()) and stream_in is sys.stdin:
        raise VocalModeRequired(
            "\nChoose how to remove the vocals with --vocals all | --vocals lead\n" + describe_modes("en") + "\n")
    tr = lang.startswith("tr")
    stream_out.write(("\nVokaller nasıl silinsin?\n" if tr else "\nHow should the vocals be removed?\n")
                     + describe_modes(lang) + "\n")
    while True:
        stream_out.write("Seçiminiz [1/2]: " if tr else "Your choice [1/2]: ")
        stream_out.flush()
        ans = (stream_in.readline() or "").strip().lower()
        if ans in ("1", "all", "tüm", "tum", "hepsi"):
            return VocalMode.ALL
        if ans in ("2", "lead", "ana"):
            return VocalMode.LEAD
        if ans == "":
            raise VocalModeRequired("no choice made")


def resolve_mode(value: str | None, lang: str = "tr", interactive: bool = True) -> VocalMode:
    mode = parse_mode(value)
    if mode is not None:
        return mode
    if not interactive:
        raise VocalModeRequired(
            "\n--vocals is required (all | lead); nothing was processed.\n" + describe_modes("en") + "\n")
    return ask_mode(lang)
