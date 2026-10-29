"""Burn word-synced karaoke lyrics onto the ORIGINAL video and swap in the instrumental.

Picture: the source video at its own resolution and frame rate; only the lyric band is
drawn on top (no blur, no darkening of the clip).

Lyrics (ASS, rendered by ffmpeg's libass):
  * two lines at the bottom, alternating slots, so the next line is readable before it starts
  * each word fills left-to-right (\\kf) exactly from its sung start - never ahead of the voice
  * unsung words white, sung words in the accent colour, dark outline + soft shadow
  * a line appears up to `preview` s before it is sung and stays until shortly after
  * optional translucent band behind the text (--band) for busy footage
  * countdown dots before a line that follows a long instrumental gap
  * long lines are wrapped by measured pixel width of the bundled font
The bundled font (DejaVu Sans Bold, all Turkish glyphs) is passed to libass via fontsdir,
so Mac, Windows and Linux render the same.
"""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

FONT_DIR = Path(__file__).parent / "fonts"
FONT_FILE = FONT_DIR / "DejaVuSans-Bold.ttf"
FONT_NAME = "DejaVu Sans"


@dataclass
class Style:
    font: str = FONT_NAME
    size: float = 0.058         # font size as a share of video height
    accent: str = "FFC400"      # sung colour, RRGGBB (warm yellow)
    unsung: str = "FFFFFF"
    outline: str = "101010"
    slot1: float = 0.80         # vertical centres of the two lines, share of height
    slot2: float = 0.90
    preview: float = 4.0        # at most this many seconds a line is shown before its first word
                            # (it appears as soon as its slot is free and the line before it started)
    linger: float = 0.5         # seconds a line stays after its last word
    band: bool = False
    max_width: float = 0.86     # max line width, share of video width


def ass_color(rrggbb: str, alpha: int = 0) -> str:
    r, g, b = rrggbb[0:2], rrggbb[2:4], rrggbb[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_text(s: str) -> str:
    return s.replace("\\", "").replace("{", "(").replace("}", ")")


class Measure:
    """Pixel width of text in the bundled font (Pillow), with a character-count fallback."""

    def __init__(self, font_px: int):
        self.px = font_px
        try:
            from PIL import ImageFont
            self.font = ImageFont.truetype(str(FONT_FILE), font_px)
        except Exception:
            self.font = None

    def __call__(self, text: str) -> float:
        if self.font is not None:
            return float(self.font.getlength(text))
        return 0.6 * self.px * len(text)


def split_words(words: list[dict], measure: Measure, max_px: float) -> list[list[dict]]:
    """Split a timed line into pieces that fit max_px, at the most balanced word boundary."""
    text = " ".join(w["text"] for w in words)
    if measure(text) <= max_px or len(words) < 2:
        return [words]
    best, best_d = 1, float("inf")
    for k in range(1, len(words)):
        a = measure(" ".join(w["text"] for w in words[:k]))
        b = measure(" ".join(w["text"] for w in words[k:]))
        d = max(a, b)
        if d < best_d:
            best, best_d = k, d
    return split_words(words[:best], measure, max_px) + split_words(words[best:], measure, max_px)


def layout(lines: list[dict], width: int, height: int, style: Style) -> list[dict]:
    """Timed lines -> display rows [{words, start, end}] that fit the screen."""
    fs = round(height * style.size)
    measure = Measure(fs)
    rows = []
    for ln in lines:
        for piece in split_words(ln["words"], measure, width * style.max_width):
            rows.append({"words": piece, "start": piece[0]["start"], "end": piece[-1]["end"]})
    return rows


def dots_drawing(r: int, n: int = 3) -> str:
    """n filled circles (left-aligned slots of a 3-dot row) as an ASS vector drawing,
    centred on the origin. Drawn shapes need no font glyph."""
    def circle(cx):
        k = round(0.5523 * r)
        return (f"m {cx} {-r} b {cx + k} {-r} {cx + r} {-k} {cx + r} 0 "
                f"b {cx + r} {k} {cx + k} {r} {cx} {r} b {cx - k} {r} {cx - r} {k} {cx - r} 0 "
                f"b {cx - r} {-k} {cx - k} {-r} {cx} {-r}")
    gap = round(r * 3.2)
    return " ".join(circle(x) for x in (-gap, 0, gap)[:n])


def build_ass(doc: dict, width: int, height: int, style: Style | None = None,
              title: str | None = None, artist: str | None = None) -> str:
    style = style or Style()
    fs = round(height * style.size)
    outline = max(2, round(fs * 0.07))
    shadow = max(1, round(fs * 0.04))
    rows = layout(doc["lines"], width, height, style)
    ys = [round(height * style.slot1), round(height * style.slot2)]
    cx = width // 2

    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Lyric,{style.font},{fs},{ass_color(style.accent)},{ass_color(style.unsung)},"
        f"{ass_color(style.outline)},{ass_color('000000', 0x70)},0,0,0,0,100,100,0,0,1,{outline},{shadow},5,0,0,0,1\n"
        f"Style: Title,{style.font},{round(fs * 1.3)},{ass_color('FFFFFF')},{ass_color('FFFFFF')},"
        f"{ass_color(style.outline)},{ass_color('000000', 0x70)},0,0,0,0,100,100,0,0,1,{outline},{shadow},5,0,0,0,1\n"
        f"Style: Band,{style.font},10,{ass_color('000000', 0x60)},{ass_color('000000', 0x60)},"
        f"{ass_color('000000', 0xFF)},{ass_color('000000', 0xFF)},0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n"
        f"Style: Dots,{style.font},10,{ass_color(style.accent)},{ass_color(style.unsung, 0x40)},"
        f"{ass_color(style.outline)},{ass_color('000000', 0x70)},0,0,0,0,100,100,0,0,1,{outline},{shadow},5,0,0,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    ev = []

    def add(layer, s, e, st, text):
        if e - s >= 0.02:
            ev.append(f"Dialogue: {layer},{ass_time(s)},{ass_time(e)},{st},,0,0,0,,{text}")

    first = rows[0]["start"] if rows else 0.0
    if title and first > 3.0:
        t1 = min(first - 1.0, 6.0)
        txt = f"{{\\an5\\pos({cx},{height // 2})\\fad(500,500)}}{ass_text(title)}"
        if artist:
            txt += f"\\N{{\\fs{round(fs * 0.8)}\\c{ass_color('DDDDDD')}}}{ass_text(artist)}"
        add(2, 0.3, t1, "Title", txt)

    slot_free = [0.0, 0.0]
    prev_end = 0.0
    shown = []
    for i, row in enumerate(rows):
        slot = i % 2
        start, end = row["start"], row["end"]
        show = max(start - style.preview, slot_free[slot] + 0.05, 0.0)
        if i:
            show = max(show, shown[i - 1] + 0.05)        # appear in reading order
        hide = end + style.linger
        if i + 2 < len(rows):                            # free the slot for the line after next
            hide = min(hide, rows[i + 2]["start"] - style.preview + 0.6, rows[i + 2]["start"] - 0.25)
        hide = max(hide, end + 0.1)
        slot_free[slot] = hide
        shown.append(show)

        parts = []
        lead = int(round((start - show) * 100))
        if lead > 0:
            parts.append(f"{{\\k{lead}}}")
        words = row["words"]
        for j, w in enumerate(words):
            nxt = words[j + 1]["start"] if j + 1 < len(words) else w["end"]
            # the fill runs while the word is sung; a pause before the next word stays filled
            fill = max(5, int(round((min(w["end"], nxt) - w["start"]) * 100)))
            rest = max(0, int(round((nxt - w["start"]) * 100)) - fill)
            sep = " " if j + 1 < len(words) else ""
            parts.append(f"{{\\kf{fill}}}{ass_text(w['text'])}")
            if rest:
                parts.append(f"{{\\k{rest}}}")
            parts.append(sep)
        y = ys[slot]
        add(1, show, hide, "Lyric", f"{{\\an5\\pos({cx},{y})\\fad(150,200)}}" + "".join(parts))

        gap = start - prev_end
        if gap >= 5.0 and start >= 3.5:
            # 3-2-1 countdown: one dot goes out every second, the line starts as the last one does
            r = max(6, round(fs * 0.16))
            dy = y - round(fs * 0.95)
            for k, n in enumerate((3, 2, 1)):
                add(2, start - 3.0 + k, start - 2.0 + k, "Dots",
                    f"{{\\an7\\pos({cx},{dy})\\bord{max(1, outline // 2)}\\c{ass_color(style.accent)}\\p1}}"
                    f"{dots_drawing(r, n)}{{\\p0}}")
        prev_end = max(prev_end, end)

    if style.band and rows:
        top = ys[0] - round(fs * 0.9)
        bot = min(height, ys[1] + round(fs * 0.9))
        add(0, max(0.0, rows[0]["start"] - style.preview - 0.3), rows[-1]["end"] + style.linger + 0.3, "Band",
            f"{{\\an7\\pos(0,{top})\\p1\\bord0\\shad0}}m 0 0 l {width} 0 {width} {bot - top} 0 {bot - top}{{\\p0}}")
    return head + "\n".join(ev) + "\n"


# --------------------------------------------------------------------------- #
def write_lrc(doc: dict, path: Path, title: str | None = None, artist: str | None = None):
    def ts(t):
        m, s = divmod(max(0.0, t), 60)
        return f"{int(m):02d}:{s:05.2f}"
    out = []
    if title:
        out.append(f"[ti:{title}]")
    if artist:
        out.append(f"[ar:{artist}]")
    out.append("[re:mv-karaoke]")
    for ln in doc["lines"]:
        body = " ".join(f"<{ts(w['start'])}>{w['text']}" for w in ln["words"])
        out.append(f"[{ts(ln['start'])}]{body}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def pick_encoder(prefer: str = "auto") -> list[str]:
    """Hardware H.264 when it exists and works, else libx264."""
    if prefer == "x264":
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]
    encs = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if platform.system() == "Darwin" and "h264_videotoolbox" in encs:
        return ["-c:v", "h264_videotoolbox", "-q:v", "65", "-allow_sw", "1"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]


def render(video: Path, instrumental: Path, doc: dict, out_dir: Path, width: int, height: int,
           style: Style | None = None, title: str | None = None, artist: str | None = None,
           keep_original_audio: bool = False, encoder: str = "auto", out_name: str = "karaoke.mp4",
           log=print) -> Path:
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg not found on PATH")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "lyrics.ass").write_text(build_ass(doc, width, height, style, title, artist), encoding="utf-8")
    fonts = out_dir / "fonts"
    fonts.mkdir(exist_ok=True)
    shutil.copy2(FONT_FILE, fonts / FONT_FILE.name)
    mp4 = out_dir / out_name
    # run inside out_dir with relative paths: avoids filter-path escaping (Windows drive colons)
    vf = "ass=lyrics.ass:fontsdir=fonts,format=yuv420p"
    cmd = ["ffmpeg", "-y", "-v", "error", "-stats",
           "-i", str(Path(video).resolve()), "-i", str(Path(instrumental).resolve()),
           "-map", "0:v:0", "-map", "1:a:0"]
    if keep_original_audio:
        cmd += ["-map", "0:a:0"]
    cmd += ["-vf", vf, *pick_encoder(encoder),
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-c:a", "aac", "-b:a", "256k"]
    if keep_original_audio:
        cmd += ["-metadata:s:a:0", "title=Karaoke", "-metadata:s:a:1", "title=Original",
                "-disposition:a:0", "default", "-disposition:a:1", "0"]
    cmd += ["-shortest", "-movflags", "+faststart", out_name]
    log(f"rendering {width}x{height} -> {mp4}")
    res = subprocess.run(cmd, cwd=str(out_dir), capture_output=True, text=True)
    if res.returncode != 0 and "videotoolbox" in " ".join(cmd):
        log("hardware encoder failed, retrying with libx264")
        cmd[cmd.index("-c:v"):cmd.index("-c:v") + 6] = pick_encoder("x264")
        res = subprocess.run(cmd, cwd=str(out_dir), capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit("ffmpeg render failed:\n" + res.stderr[-3000:])
    return mp4


def load_doc(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
