"""Build a synthetic music video with a known answer, for end-to-end tests.

  python tests/make_test_clip.py OUT_DIR [SONG.json]

Writes OUT_DIR/test_clip.mp4 (1280x720, 25 fps, animated picture + song) and
OUT_DIR/truth.json (every sung word with its exact start/end, lead vs backing).

The song: by default original lyrics written for this test; or a song file (see
tests/songs/) with public-domain lyrics. A lead "singer" made with espeak-ng word by word
with a pitch melody, a quieter backing voice echoing the last word of chosen lines, and a
synthesized band (bass, chords, kick, snare, hats). Instrumental breaks test the countdown.

Song file: {"title", "artist", "sections": [{"name", "lines": [...], "gap_before": s,
"echo": true|false}], "asr_mistakes": {"word": "misheard" | null}}

Needs: numpy, soundfile, espeakng-loader (pip), ffmpeg on PATH.
"""
from __future__ import annotations

import ctypes
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 44100
BPM = 96
BEAT = 60 / BPM

# Default song: original lyrics written for this test only.
VERSE = [
    "Akşam olur ışıklar yanar",
    "Sokaklarda bir şarkı kalır",
    "Rüzgâr eser yapraklar döner",
    "Gönlüm yine seni arar",
]
CHORUS = [
    "Gel bu gece şarkı söyle",
    "Yıldızlar bize eşlik etsin",
]
MELODY = [55, 60, 64, 60, 57, 52, 50, 55, 62, 66, 60, 57]   # espeak pitch values (0-100)
DEFAULT_SONG = {
    "title": "Test Şarkısı", "artist": "mv-karaoke",
    "sections": [
        {"name": "verse", "lines": VERSE, "gap_before": 6.0, "echo": False},
        {"name": "chorus", "lines": CHORUS, "gap_before": 8.0, "echo": True},
        {"name": "chorus", "lines": CHORUS, "gap_before": 0.0, "echo": True},
    ],
}


# --------------------------------------------------------------------------- #
class Espeak:
    def __init__(self, voice: str = "tr"):
        import espeakng_loader
        self.lib = ctypes.CDLL(espeakng_loader.get_library_path())
        data_parent = str(Path(espeakng_loader.get_data_path()).parent).encode()
        self.lib.espeak_Initialize.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        self.sr = self.lib.espeak_Initialize(0x02, 0, data_parent, 0)   # AUDIO_OUTPUT_SYNCHRONOUS
        if self.sr <= 0:
            raise RuntimeError("espeak-ng init failed")
        self.buf: list[np.ndarray] = []
        CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p)

        def cb(wav, n, events):
            if n > 0:
                self.buf.append(np.ctypeslib.as_array(wav, shape=(n,)).astype(np.float32) / 32768.0)
            return 0
        self._cb = CB(cb)
        self.lib.espeak_SetSynthCallback(self._cb)
        self.lib.espeak_SetVoiceByName(voice.encode())
        self.lib.espeak_Synth.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint, ctypes.c_int,
                                          ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]

    def say(self, text: str, pitch: int = 50, rate: int = 125, rng: int = 20) -> np.ndarray:
        self.lib.espeak_SetParameter(1, rate, 0)    # espeakRATE
        self.lib.espeak_SetParameter(3, pitch, 0)   # espeakPITCH
        self.lib.espeak_SetParameter(4, rng, 0)     # espeakRANGE
        self.buf = []
        b = text.encode("utf-8") + b"\0"
        self.lib.espeak_Synth(b, len(b), 0, 1, 0, 1, None, None)   # POS_CHARACTER, espeakCHARS_UTF8
        self.lib.espeak_Synchronize()
        x = np.concatenate(self.buf) if self.buf else np.zeros(1, np.float32)
        x = resample(x, self.sr, SR)
        return trim(x)


def resample(x: np.ndarray, a: int, b: int) -> np.ndarray:
    if a == b:
        return x
    t_old = np.arange(len(x)) / a
    t_new = np.arange(int(len(x) * b / a)) / b
    return np.interp(t_new, t_old, x).astype(np.float32)


def trim(x: np.ndarray, thr: float = 0.02) -> np.ndarray:
    idx = np.where(np.abs(x) > thr)[0]
    if len(idx) == 0:
        return x[:1]
    return x[max(0, idx[0] - 40): idx[-1] + 40]


# --------------------------------------------------------------------------- #
def band(duration: float) -> np.ndarray:
    """Am - F - C - G, one bar each; bass, pad, kick, snare, hats. Stereo."""
    n = int(duration * SR)
    t = np.arange(n) / SR
    out = np.zeros((n, 2), np.float32)
    roots = [110.0, 87.31, 130.81, 98.0]
    thirds = [1.189, 1.26, 1.26, 1.26]
    bar = 4 * BEAT
    for k in range(int(duration / bar) + 1):
        s, e = int(k * bar * SR), min(n, int((k + 1) * bar * SR))
        if s >= n:
            break
        tt = t[s:e] - k * bar
        r = roots[k % 4]
        env = np.minimum(1, tt / 0.3) * np.exp(-tt * 0.25)
        pad = sum(np.sin(2 * np.pi * r * 2 * m * tt) for m in (1, thirds[k % 4], 1.498)) * 0.06 * env
        bass = (np.sign(np.sin(2 * np.pi * r / 2 * tt)) * 0.05 + np.sin(2 * np.pi * r / 2 * tt) * 0.18) * \
               np.exp(-(tt % BEAT) * 3)
        out[s:e, 0] += pad * 1.1 + bass
        out[s:e, 1] += pad * 0.9 + bass
    rng = np.random.default_rng(7)
    for b in range(int(duration / BEAT)):
        s = int(b * BEAT * SR)
        L = min(int(0.25 * SR), n - s)
        if L <= 0:
            break
        tt = np.arange(L) / SR
        if b % 2 == 0:
            kick = np.sin(2 * np.pi * (50 + 80 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 12) * 0.5
            out[s:s + L] += kick[:, None]
        else:
            sn = rng.standard_normal(L) * np.exp(-tt * 20) * 0.15
            out[s:s + L] += sn[:, None]
        for h in (0, 0.5):
            hs = int((b + h) * BEAT * SR)
            HL = min(int(0.05 * SR), n - hs)
            if HL > 0:
                hat = rng.standard_normal(HL) * np.exp(-np.arange(HL) / SR * 90) * 0.04
                out[hs:hs + HL, 0] += hat * 0.7
                out[hs:hs + HL, 1] += hat
    return out


def place(track: np.ndarray, clip: np.ndarray, t: float, gain: float, pan: float = 0.5):
    s = int(t * SR)
    e = min(len(track), s + len(clip))
    track[s:e, 0] += clip[: e - s] * gain * (1 - pan) * 2 * 0.5 ** 0.5
    track[s:e, 1] += clip[: e - s] * gain * pan * 2 * 0.5 ** 0.5


def build(out_dir: Path, song: dict | None = None) -> dict:
    song = song or DEFAULT_SONG
    out_dir.mkdir(parents=True, exist_ok=True)
    lead = Espeak("tr")
    # synthesize first, lay out after: the song length follows from the words
    events = []          # (kind, t, clip, text, line_index)
    truth = {"title": song.get("title"), "artist": song.get("artist"), "lines": [], "backing": [], "bpm": BPM}
    t, note = 0.0, 0
    for sec in song["sections"]:
        t += sec.get("gap_before", 0.0)
        for line in sec["lines"]:
            words = []
            for w in line.split():
                clip = lead.say(w, pitch=MELODY[note % len(MELODY)])
                note += 1
                events.append(("lead", t, clip))
                words.append({"text": w, "start": round(t, 3), "end": round(t + len(clip) / SR, 3)})
                t += len(clip) / SR + 0.07
            truth["lines"].append({"text": line, "section": sec["name"], "words": words})
            if sec.get("echo"):            # backing voice echoes the last word, higher and to the right
                last = line.split()[-1]
                clip = lead.say(last, pitch=85, rate=110)
                bt = t + 0.15
                events.append(("backing", bt, clip))
                truth["backing"].append({"text": last, "start": round(bt, 3), "end": round(bt + len(clip) / SR, 3)})
                t = bt + len(clip) / SR + 0.6
            else:
                t += 0.9
    duration = round(t + 4.0, 2)
    vocal = np.zeros((int(duration * SR), 2), np.float32)
    backing = np.zeros_like(vocal)
    for kind, et, clip in events:
        if kind == "lead":
            place(vocal, clip, et, 0.9)
        else:
            place(backing, clip, et, 0.45, pan=0.75)
    music = band(duration)
    mix = music * 0.55 + vocal + backing
    mix *= 0.9 / max(1e-6, float(np.abs(mix).max()))
    sf.write(str(out_dir / "test_song.wav"), mix, SR, subtype="PCM_16")
    sf.write(str(out_dir / "truth_lead_vocal.wav"), vocal, SR, subtype="PCM_16")
    (out_dir / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")

    # picture: slowly moving colour gradients with a "life" texture over it
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"gradients=s=1280x720:r=25:d={duration}:c0=0x2b1055:c1=0x7597de:c2=0xd4418e:"
                             f"c3=0x0bab64:nb_colors=4:speed=0.015",
        "-f", "lavfi", "-i", f"life=s=320x180:r=25:mold=8:ratio=0.08:life_color=#ffffff:death_color=#000000,"
                             f"scale=1280:720:flags=neighbor",
        "-i", str(out_dir / "test_song.wav"),
        "-filter_complex", "[1:v]format=rgba,colorkey=black:0.3:0.1,colorchannelmixer=aa=0.35[l];"
                           "[0:v]format=rgba[g];[g][l]overlay=format=auto,format=yuv420p[v]",
        "-map", "[v]", "-map", "2:a", "-t", str(duration),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "aac", "-b:a", "192k",
        str(out_dir / "test_clip.mp4")], check=True)
    return truth


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "test_out")
    spec = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")) if len(sys.argv) > 2 else None
    tr = build(out, spec)
    n = sum(len(l["words"]) for l in tr["lines"])
    print(f"wrote {out / 'test_clip.mp4'}: {len(tr['lines'])} lines, {n} lead words, {len(tr['backing'])} backing echoes")
