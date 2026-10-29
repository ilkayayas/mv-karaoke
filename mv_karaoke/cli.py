"""mv-karaoke command line.

  python -m mv_karaoke.cli INPUT --vocals all|lead [options]
  python -m mv_karaoke.cli INPUT --preview-vocals [--preview-start 60]

INPUT is a YouTube/yt-dlp link or a local video file (mp4, mov, mkv, webm, m4v).

--vocals has no default on purpose: removing every vocal can leave a song hollow, so the
choice is made per song. Without it the tool asks on the terminal, and refuses to start
when there is no terminal to ask on. --preview-vocals renders a short excerpt in both
modes so the choice can be made by ear first.

Lyrics are never looked up or asked for: the first run takes them from the song with
speech recognition and writes <out>/lyrics.txt next to the video. Fix wrong words or
letters in that file and run the same command again; separation and recognition are
cached, only the timing and the video are redone.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from .ingest import ingest
from .separate import separate
from .vocals import VocalMode, VocalModeRequired, describe_modes, resolve_mode


def log(msg: str):
    print(f"[mv-karaoke] {msg}", flush=True)


def prepare_lyrics_file(lyrics_file: Path, new_draft: str, old_draft: str, work: Path,
                        user_given: bool = False, source: str = "song", log=print) -> bool:
    """Write or refresh the lyrics draft. A file the user has edited is never touched: it is
    rewritten only while it is still exactly the draft this tool wrote. Returns True when the
    file now holds a fresh draft."""
    import hashlib
    sha = lambda t: hashlib.sha256(t.encode("utf-8")).hexdigest()
    stamp = work / ".lyrics-draft.sha256"
    if not lyrics_file.exists():
        lyrics_file.write_text(new_draft, encoding="utf-8")
        stamp.write_text(sha(new_draft) + "\n", encoding="utf-8")
        log(f"lyrics written from the {source}: {lyrics_file}")
        return True
    if user_given:
        log(f"lyrics from {lyrics_file}")
        return False
    current = lyrics_file.read_text(encoding="utf-8")
    untouched = (stamp.exists() and stamp.read_text(encoding="utf-8").strip() == sha(current)) or current == old_draft
    if untouched and current != new_draft:
        (work / "lyrics.prev.txt").write_text(current, encoding="utf-8")
        lyrics_file.write_text(new_draft, encoding="utf-8")
        stamp.write_text(sha(new_draft) + "\n", encoding="utf-8")
        log("lyrics.txt was still the untouched draft: rewritten with the new findings "
            "(old one in lyrics.prev.txt)")
        return True
    log(f"lyrics from {lyrics_file}")
    return False


def default_out(inp: str) -> Path:
    """./karaoke/<video id or file name>: stable across runs, so stems are reused."""
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", inp)
    if m:
        name = m.group(1)
    elif re.match(r"^\w+:", inp) and not Path(inp).exists():
        name = re.sub(r"[^\w-]+", "_", inp)[:60]
    else:
        name = Path(inp).stem
    return Path.cwd() / "karaoke" / re.sub(r'[\\/:*?"<>|]+', "", name).strip()


def make_previews(media, work: Path, start: float | None, length: float, quality: str, device: str) -> list[Path]:
    """Cut an excerpt (default: around a third into the song, usually a chorus) and run both modes."""
    s = start if start is not None else max(0.0, media.duration / 3)
    excerpt = work / "preview" / "excerpt.wav"
    excerpt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{s:.2f}", "-t", f"{length:.2f}", "-i", str(media.audio),
                    "-c:a", "pcm_s16le", str(excerpt)], check=True)
    out = []
    for mode in VocalMode:
        stems = separate(excerpt, work / "preview", mode, quality, device, force=True, log=log)
        dst = work / "preview" / f"preview_{mode.value}.mp3"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(stems.instrumental), "-q:a", "2", str(dst)],
                       check=True)
        out.append(dst)
    return out


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):        # Windows consoles / redirected output
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="mv-karaoke", description="Music video -> karaoke video.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="vocal modes:\n" + describe_modes("en"))
    ap.add_argument("input", help="local video file, YouTube link (yt-dlp) or direct link to a video file")
    ap.add_argument("--vocals", help="all | lead  (required; asked interactively when omitted)")
    ap.add_argument("--preview-vocals", action="store_true",
                    help="render a short excerpt with both modes to compare, then stop")
    ap.add_argument("--preview-start", type=float, help="excerpt start in seconds (default: 1/3 into the song)")
    ap.add_argument("--preview-length", type=float, default=20.0)
    ap.add_argument("--quality", choices=["light", "fast", "high"], default="fast",
                    help="separation quality. light: small models for low-RAM/CPU machines; "
                         "fast: htdemucs / RoFormer karaoke; high: htdemucs_ft / 3-model karaoke ensemble")
    ap.add_argument("--device", default="auto", help="auto | mps | cuda | cpu")
    ap.add_argument("--out", help="output folder (default: ./karaoke/<video id or file name>)")
    ap.add_argument("--lang", default="tr", help="language for prompts and lyric rules (tr, en, ...)")
    ap.add_argument("--yes", action="store_true", help="never ask; fail if a required choice is missing")
    ap.add_argument("--force", action="store_true", help="redo separation and recognition even when cached")
    d = ap.add_argument_group("download (when the input is a link)")
    d.add_argument("--max-height", type=int, default=1080,
                   help="YouTube: download at most this video height (default 1080; 0 = best available)")
    g = ap.add_argument_group("lyrics and timing")
    g.add_argument("--lyrics", help="lyrics file to use (default: <out>/lyrics.txt, written from the song "
                                    "on the first run and corrected by hand)")
    g.add_argument("--whisper", default="large-v3-turbo", help="Whisper model for recognition")
    g.add_argument("--asr-backend", default="auto", help="auto | mlx | faster-whisper | whisper")
    g.add_argument("--asr-json", help="use word timings from this JSON instead of running Whisper")
    g.add_argument("--retranscribe", action="store_true", help="run speech recognition again")
    g.add_argument("--no-gap-pass", action="store_true",
                   help="don't re-listen to sung parts the first recognition left empty")
    g.add_argument("--no-subs", action="store_true", help="don't use the YouTube video's own subtitles")
    r = ap.add_argument_group("video")
    r.add_argument("--band", action="store_true", help="translucent band behind the lyrics")
    r.add_argument("--title-card", action="store_true", help="show artist and title before the first line")
    r.add_argument("--keep-original-audio", action="store_true",
                   help="add the original song as a second audio track")
    r.add_argument("--encoder", default="auto", help="auto (hardware when available) | x264")
    r.add_argument("--no-video", action="store_true", help="stop after lyrics.json / lyrics.lrc")
    a = ap.parse_args(argv)

    # Ask before any download or processing
    mode = None
    if not a.preview_vocals:
        try:
            mode = resolve_mode(a.vocals, a.lang, interactive=not a.yes and sys.stdin.isatty())
        except VocalModeRequired as e:
            print(e, file=sys.stderr)
            return 2

    work = Path(a.out).expanduser() if a.out else default_out(a.input)
    log(f"working in {work}")
    media = ingest(a.input, work, log, max_height=a.max_height or None)

    if a.preview_vocals:
        files = make_previews(media, work, a.preview_start, a.preview_length, a.quality, a.device)
        log("listen and choose, then run again with --vocals all or --vocals lead:")
        for f in files:
            log(f"  {f}")
        return 0

    stems = separate(media.audio, work, mode, a.quality, a.device, force=a.force, log=log)
    (work / "stems.json").write_text(json.dumps({
        "mode": stems.mode.value, "instrumental": str(stems.instrumental), "vocals": str(stems.vocals),
        "model": stems.model, "device": stems.device}, indent=1), encoding="utf-8")
    log(f"vocals removed ({mode.value}): {stems.instrumental}")

    # ---- word timing from the removed vocal (cached next to the stems) -------------------
    from .asr import draft_lyrics, load_meta, load_words, save_words, transcribe, voiced_regions
    from .fill import clock, fill_gaps, gap_pass, missing_report
    voiced = voiced_regions(stems.vocals)
    asr_cache = stems.vocals.parent / "asr.json"
    meta = {}
    if a.asr_json:
        words, lang = load_words(Path(a.asr_json))
        meta = {"gap_pass": True}
        log(f"word timings from {a.asr_json}")
    elif asr_cache.exists() and not a.force and not a.retranscribe:
        words, lang = load_words(asr_cache)
        meta = load_meta(asr_cache)
        log("word timings: reusing earlier speech recognition")
    else:
        words, lang = transcribe(stems.vocals, a.lang, a.whisper, a.asr_backend, a.device, log, voiced)
        meta = {"model": a.whisper}
    lang = a.lang or lang
    first_words = list(words)          # what an untouched lyrics.txt draft was made from
    if not meta.get("gap_pass") and not a.no_gap_pass:
        def listen(regions):
            return transcribe(stems.vocals, lang, a.whisper, a.asr_backend, a.device, log, regions,
                              separate_regions=True)[0]
        words, gaps, n = gap_pass(stems.vocals, words, voiced, listen, log)
        if gaps:
            log(f"second listen found {n} more words")
        meta["gap_pass"] = True
    if not a.asr_json:
        save_words(words, lang, asr_cache, meta)

    # ---- the video's own YouTube subtitles: fill what recognition missed -----------------
    subs, manual_draft = None, None
    from .ingest import is_youtube
    if media.source_url and is_youtube(media.source_url) and not a.no_subs:
        from .subs import draft_from_manual, fetch, measure_offset, shift, to_words
        subs = fetch(media.source_url, work, lang or "tr", log)
        if subs and subs.cues:
            sw = to_words(subs, lang)
            off, support = measure_offset(sw, words, lang)
            if off is None:
                log(f"subtitles don't follow the singing here ({support} matching words): not used")
                subs = None
            else:
                if abs(off) >= 0.05:
                    log(f"subtitles are {off:+.2f} s off the singing: corrected")
                sw = shift(sw, off)
                for c in subs.cues:
                    c.start, c.end = c.start + off, c.end + off
                words, n = fill_gaps(words, sw, voiced)
                if n:
                    log(f"subtitles filled {n} words where recognition heard nothing")
                if subs.kind == "manual":
                    manual_draft = draft_from_manual(subs)
    if not words:
        log("speech recognition found no words in the vocal; check the vocal mode or the input")
        return 1

    # ---- lyrics: taken from the song itself; the user corrects lyrics.txt by hand --------
    lyrics_file = Path(a.lyrics).expanduser() if a.lyrics else work / "lyrics.txt"
    first_draft = prepare_lyrics_file(lyrics_file, manual_draft or draft_lyrics(words),
                                      draft_lyrics(first_words), work, user_given=bool(a.lyrics),
                                      source="subtitles" if manual_draft else "song", log=log)

    from .lyrics import parse_lyrics
    from .match import sync_lyrics
    lines = parse_lyrics(lyrics_file.read_text(encoding="utf-8-sig"))
    if not lines:
        log(f"{lyrics_file} has no lyric lines")
        return 1
    doc = sync_lyrics(lines, words, lang, voiced=voiced, duration=media.duration)
    (work / "lyrics.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    # ---- render onto the original picture ------------------------------------------------
    from .render import Style, render, write_lrc
    write_lrc(doc, work / "lyrics.lrc", media.title, media.artist)
    if a.no_video:
        log("done (--no-video)")
        return 0
    style = Style(band=a.band)
    mp4 = render(media.video, stems.instrumental, doc, work, media.width, media.height, style,
                 media.title if a.title_card else None, media.artist if a.title_card else None,
                 keep_original_audio=a.keep_original_audio, encoder=a.encoder, log=log)

    s = doc["stats"]
    print()
    log(f"karaoke video : {mp4}")
    log(f"lyrics file   : {lyrics_file}")
    log(f"timing        : {s['anchored_ratio']:.0%} of words anchored to the singing")
    if doc["review"]:
        log("lines to check (time  text):")
        for i in doc["review"][:20]:
            ln = doc["lines"][i]
            log(f"   {ln['start']:7.2f}s  {ln['text']}")
    missing = missing_report(doc, voiced, words)
    if missing:
        log(f"singing heard but no lyrics placed ({len(missing)} parts) - add these lines to {lyrics_file.name}:")
        for m in missing:
            heard = f'  heard: "{m["heard"]}"' if m["heard"] else "  (nothing recognised)"
            log(f"   {clock(m['start'])} - {clock(m['end'])}{heard}")
        (work / "missing.json").write_text(json.dumps(missing, ensure_ascii=False, indent=1), encoding="utf-8")
    if first_draft:
        src = "the uploaded subtitles" if manual_draft else "speech recognition"
        log(f"the lyrics were taken from {src}. Fix any wrong words or letters in")
        log(f"{lyrics_file.name}, then run the same command again: only the timing and the video")
        log("are redone, separation and recognition are reused.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
