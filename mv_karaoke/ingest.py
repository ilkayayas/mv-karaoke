"""Input: a local video file (mp4, mov, mkv, webm, m4v, avi) or a link. A YouTube link is
downloaded with yt-dlp; any other link is downloaded as a file.

The video stream is never decoded here; it is kept for the final render. The audio is
extracted to 44.1 kHz stereo WAV on the same timebase as the video (container start
offsets are applied), so lyric times line up with the picture.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


@dataclass
class Media:
    video: Path              # original file with picture + sound
    audio: Path              # source.wav, 44.1 kHz stereo, video timebase
    duration: float
    width: int
    height: int
    fps: float
    audio_offset: float      # audio start - video start, seconds (already applied to source.wav)
    title: str | None = None
    artist: str | None = None
    source_url: str | None = None

    def save(self, path: Path):
        d = {k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(self).items()}
        path.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def is_url(s: str) -> bool:
    return bool(re.match(r"^(https?://|ytsearch\d*:)", s, re.I))


def need(tool: str):
    if shutil.which(tool) is None:
        raise SystemExit(f"{tool} not found on PATH (macOS: brew install ffmpeg; Windows: winget install Gyan.FFmpeg)")


def probe(path: Path) -> dict:
    need("ffprobe")
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                          str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _rate(s: str | None) -> float:
    if not s or s == "0/0":
        return 0.0
    a, _, b = s.partition("/")
    return float(a) / float(b or 1)


YOUTUBE_ID = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/)|youtu\.be/)([\w-]{11})")
YOUTUBE_HOST = re.compile(r"^https?://([\w-]+\.)*(youtube\.com|youtu\.be|youtube-nocookie\.com)(/|$)", re.I)
VIDEO_MIME = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-matroska": ".mkv",
              "video/webm": ".webm", "video/x-m4v": ".m4v", "video/x-msvideo": ".avi"}


def is_youtube(url: str) -> bool:
    return bool(YOUTUBE_HOST.match(url.strip())) or url.lower().startswith("ytsearch")


def canonical_url(url: str) -> str:
    """Same video, same key: drops tracking (?si=), timestamps and playlist parameters."""
    m = YOUTUBE_ID.search(url)
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else url.strip()


def format_selector(max_height: int | None) -> str:
    """Best picture + best sound, merged without re-encoding; optionally capped in height
    (a 4K source makes the karaoke render much slower than 1080p, with no gain on most screens)."""
    if not max_height:
        return "bv*+ba/b"
    h = int(max_height)
    return f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"


def download(url: str, work: Path, log=print, max_height: int | None = 1080) -> tuple[Path, dict]:
    """A link -> work/original.<ext>. YouTube links go through yt-dlp, any other link is
    downloaded as a file. Reused when already there (invalidate_if_changed clears the folder
    when the link changes)."""
    info_file = work / "download.json"
    have = [p for p in work.glob("original.*") if p.suffix.lower() in VIDEO_EXT]
    if have and info_file.exists():
        log(f"video already downloaded: {have[0].name}")
        return have[0], json.loads(info_file.read_text(encoding="utf-8"))
    meta = youtube_download(url, work, log, max_height) if is_youtube(url) else file_download(url, work, log)
    info_file.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    video = next(p for p in work.glob("original.*") if p.suffix.lower() in VIDEO_EXT)
    return video, meta


def youtube_download(url: str, work: Path, log, max_height: int | None) -> dict:
    try:
        import yt_dlp
    except ImportError:
        raise SystemExit("yt-dlp is not installed: pip install -U yt-dlp")
    if shutil.which("deno") is None and shutil.which("node") is None:
        log("warning: no JavaScript runtime found; YouTube downloads may fail or be limited. "
            "Install deno (macOS: brew install deno, Windows: winget install DenoLand.Deno)")
    log(f"downloading from YouTube with yt-dlp: {url}")
    opts = {
        "format": format_selector(max_height),
        "merge_output_format": "mkv",          # any codec pair fits; ffmpeg reads it directly
        "outtmpl": {"default": str(work / "original.%(ext)s")},
        "noplaylist": True, "quiet": True, "no_warnings": True, "noprogress": True,
        "retries": 5, "fragment_retries": 5,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:  # yt_dlp.utils.DownloadError and friends
        raise SystemExit(f"YouTube download failed: {str(e).strip()[:400]}\n"
                         "  update yt-dlp and try again (YouTube changes often): pip install -U yt-dlp")
    if "entries" in info:
        info = info["entries"][0]
    if not any(p.suffix.lower() in VIDEO_EXT for p in work.glob("original.*")):
        raise SystemExit("yt-dlp produced no video file")
    artist = info.get("artist") or info.get("creator") or info.get("uploader")
    if artist:
        artist = re.sub(r"\s*-\s*Topic$|VEVO$", "", artist).strip()
    log(f"downloaded: {info.get('height') or '?'}p - {info.get('title')}")
    return {"title": info.get("track") or info.get("title"), "artist": artist, "via": "yt-dlp",
            "id": info.get("id"), "webpage_url": info.get("webpage_url") or url}


def file_download(url: str, work: Path, log, chunk: int = 1 << 20) -> dict:
    """Plain HTTP(S) download of a video file, streamed to disk, then checked with ffprobe."""
    import urllib.error
    import urllib.parse
    import urllib.request

    log(f"downloading file: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "mv-karaoke/0.1"})
    part = work / "original.part"
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype.startswith("text/") or ctype in ("application/json", "application/xhtml+xml"):
                raise SystemExit(f"the link is a web page ({ctype}), not a video file. Give a direct "
                                 "link to the video file (ending in .mp4, .mov, ...) or a YouTube link.")
            total = int(r.headers.get("Content-Length") or 0)
            name = urllib.parse.unquote(Path(urllib.parse.urlparse(r.geturl()).path).name)
            cd = r.headers.get("Content-Disposition") or ""
            m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd, re.I)
            if m:
                name = urllib.parse.unquote(m.group(1))
            done, next_report = 0, 0.1
            with open(part, "wb") as f:
                while True:
                    buf = r.read(chunk)
                    if not buf:
                        break
                    f.write(buf)
                    done += len(buf)
                    if total and done / total >= next_report:
                        log(f"  {done / total:.0%} of {total / 1e6:.0f} MB")
                        next_report += 0.1
    except urllib.error.URLError as e:
        part.unlink(missing_ok=True)
        raise SystemExit(f"download failed: {getattr(e, 'reason', e)}")
    if total and done < total:
        part.unlink(missing_ok=True)
        raise SystemExit(f"download incomplete ({done} of {total} bytes); try again")
    ext = Path(name).suffix.lower()
    if ext not in VIDEO_EXT:
        ext = VIDEO_MIME.get(ctype, ".mp4")
    video = work / f"original{ext}"
    part.replace(video)
    try:
        probe(video)
    except subprocess.CalledProcessError:
        video.unlink(missing_ok=True)
        raise SystemExit("the downloaded file is not a readable video")
    stem = Path(name).stem if name else None
    log(f"downloaded: {video.name} ({done / 1e6:.1f} MB)")
    return {"title": None, "artist": None, "file_name": stem, "via": "http", "webpage_url": url}


def fingerprint(inp: str) -> str:
    if is_url(inp):
        return "url:" + canonical_url(inp)
    p = Path(inp).expanduser().resolve()
    st = p.stat()
    return f"file:{p.name}:{st.st_size}:{int(st.st_mtime)}"


def invalidate_if_changed(inp: str, work: Path, log=print):
    """Cached audio, stems and recognition belong to one input. A different input in the same
    folder clears them (lyrics.txt is kept: it is the user's file)."""
    stamp = work / "input.txt"
    fp = fingerprint(inp)
    if stamp.exists() and stamp.read_text(encoding="utf-8").strip() != fp:
        log("input changed since the last run in this folder: clearing cached audio and stems")
        for p in [work / "source.wav", work / "media.json", work / "download.json", *work.glob("original.*")]:
            p.unlink(missing_ok=True)
        shutil.rmtree(work / "stems", ignore_errors=True)
        shutil.rmtree(work / "preview", ignore_errors=True)
    stamp.write_text(fp + "\n", encoding="utf-8")


def ingest(inp: str, work: Path, log=print, max_height: int | None = 1080) -> Media:
    need("ffmpeg")
    work.mkdir(parents=True, exist_ok=True)
    invalidate_if_changed(inp, work, log)
    meta: dict = {}
    if is_url(inp):
        video, dl = download(inp, work, log, max_height)
        meta = {"title": dl.get("title"), "artist": dl.get("artist"), "source_url": canonical_url(inp)}
        if not meta["title"] and dl.get("file_name"):          # "Sanatçı - Şarkı.mp4" from a plain link
            m = re.match(r"^\s*(.+?)\s+-\s+(.+?)\s*$", dl["file_name"])
            if m:
                meta["artist"], meta["title"] = m.group(1), m.group(2)
    else:
        video = Path(inp).expanduser().resolve()
        if not video.exists():
            raise SystemExit(f"input not found: {video}")
        if video.suffix.lower() not in VIDEO_EXT:
            raise SystemExit(f"not a video file: {video.name} (expected {', '.join(sorted(VIDEO_EXT))})")
    info = probe(video)
    vs = next((s for s in info["streams"] if s.get("codec_type") == "video"
               and not s.get("disposition", {}).get("attached_pic")), None)
    aus = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
    if vs is None:
        raise SystemExit("the input has no video stream")
    if aus is None:
        raise SystemExit("the input has no audio stream")
    tags = {k.lower(): v for k, v in info.get("format", {}).get("tags", {}).items()}
    if not meta.get("title"):
        meta["title"] = tags.get("title")
        meta["artist"] = tags.get("artist")
    if not meta.get("title"):
        m = re.match(r"^\s*(.+?)\s+-\s+(.+?)\s*$", video.stem)
        if m:
            meta["artist"], meta["title"] = m.group(1), m.group(2)

    v0 = float(vs.get("start_time") or 0)
    a0 = float(aus.get("start_time") or 0)
    offset = a0 - v0
    wav = work / "source.wav"
    if not wav.exists():
        log("extracting audio to 44.1 kHz WAV")
        # place audio on the video's clock: delay it if it starts later, trim it if earlier
        af = []
        if offset > 0.0005:
            ms = int(round(offset * 1000))
            af.append(f"adelay={ms}|{ms}")
        elif offset < -0.0005:
            af.append(f"atrim=start={-offset:.4f},asetpts=PTS-STARTPTS")
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-map", f"0:{aus['index']}", "-vn",
               "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le"]
        if af:
            cmd += ["-af", ",".join(af)]
        subprocess.run(cmd + [str(wav)], check=True)
    media = Media(video=video, audio=wav, duration=float(info["format"].get("duration") or 0),
                  width=int(vs["width"]), height=int(vs["height"]),
                  fps=_rate(vs.get("avg_frame_rate")) or _rate(vs.get("r_frame_rate")),
                  audio_offset=round(offset, 4), title=meta.get("title"), artist=meta.get("artist"),
                  source_url=meta.get("source_url"))
    media.save(work / "media.json")
    return media
