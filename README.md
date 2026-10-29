# mv-karaoke

Turn a music video into a karaoke video. The original picture is kept, the vocals are
removed, and the lyrics are drawn on top with a word-by-word highlight that follows the
singer.

- **Input:** a local video file (`.mp4`, `.mov`, `.mkv`, `.webm`, `.m4v`, `.avi`), a YouTube
  link, or a direct link to a video file
- **Vocal removal, your choice per song:** remove *all* vocals, or only the *lead* singer and
  keep the backing vocals
- **Lyrics from the song itself:** taken from the vocals with speech recognition (and from the
  video's own YouTube subtitles when there are any), saved as `lyrics.txt` next to the video
  so you can correct them by hand; a re-run takes seconds
- **Word-level timing** that keeps your text exactly as written, Turkish-aware
- **Runs locally** on macOS (Apple Silicon) and Windows; nothing is uploaded
- Usable as a plain command-line tool or as a Claude Code skill ([`SKILL.md`](SKILL.md))

## Installation

Step-by-step guides:

- **[macOS (Apple Silicon)](docs/INSTALL-macOS.md)**
- **[Windows 10 / 11](docs/INSTALL-Windows.md)**

In short, you need Python 3.12, ffmpeg (with libass), deno (for YouTube downloads) and
about 6 GB of free disk space for the Python packages and the models, which are downloaded
on first use.

```bash
# macOS
brew install python@3.12 ffmpeg deno git
git clone https://github.com/ilkayayas/mv-karaoke.git && cd mv-karaoke
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[demucs,karaoke,align-mac]"
```

```powershell
# Windows (PowerShell)
winget install Python.Python.3.12 Gyan.FFmpeg DenoLand.Deno Git.Git
git clone https://github.com/ilkayayas/mv-karaoke.git; cd mv-karaoke
py -3.12 -m venv .venv; .venv\Scripts\Activate.ps1
pip install -e ".[demucs,karaoke,align]"
```

## Usage

```bash
python -m mv_karaoke.cli clip.mp4 --vocals lead
python -m mv_karaoke.cli "https://www.youtube.com/watch?v=VIDEO_ID" --vocals all
python -m mv_karaoke.cli "https://example.com/clip.mp4" --vocals lead --quality high
```

Always put links in quotes (zsh and PowerShell treat `?` and `&` specially).

### 1. Choose how the vocals are removed

There is no default, because the right choice depends on the song. Without `--vocals` the
tool asks before it downloads or processes anything.

| `--vocals` | Removes | Good for |
|---|---|---|
| `all` | lead, backing vocals, harmonies, choirs | the cleanest karaoke; vocal-heavy songs can sound hollow |
| `lead` | only the lead singer; backing vocals stay | fuller sound, like professional karaoke tracks; parts the model takes for backing vocals stay audible |

Not sure? Listen to a 20-second excerpt in both modes first:

```bash
python -m mv_karaoke.cli clip.mp4 --preview-vocals        # writes preview_all.mp3 / preview_lead.mp3
```

| `--quality` | `all` uses | `lead` uses | When |
|---|---|---|---|
| `light` | Demucs htdemucs | UVR MDX-Net karaoke (52 MB) | less than 8 GB RAM, CPU-only machines, quick tries |
| `fast` (default) | Demucs htdemucs | MelBand-RoFormer karaoke (1.7 GB) | normal use |
| `high` | Demucs htdemucs_ft | 3 karaoke models averaged | final output; several times slower |

### 2. Correct the lyrics, run again

The first run writes `lyrics.txt` next to `karaoke.mp4`. Speech recognition makes mistakes
(Turkish letters especially: ı/i, ş/s, ğ/g, â; misheard, split or missing words), so:

1. Open `karaoke/<video>/lyrics.txt` and fix it: one sung line per line, a blank line
   between stanzas. Section labels (`[Chorus]`, `[Nakarat]`) and repeat marks (`(x2)`) are
   understood.
2. Run **the same command** again. Download, vocal removal and recognition are reused; only
   the timing and the video are redone, in seconds.

At the end of every run the tool lists:

- **lines to check:** lines whose timing is uncertain
- **singing heard but no lyrics placed:** times where someone sings but `lyrics.txt` has
  nothing, with whatever was recognised there (also saved to `missing.json`). Add those
  lines at the right place.

Once you have edited `lyrics.txt`, it is never overwritten. While it is still the untouched
draft, a run that finds more words refreshes it (the old draft goes to `lyrics.prev.txt`).

### 3. Output

Everything goes to `./karaoke/<YouTube id or file name>/` (or `--out DIR`):

| File | |
|---|---|
| `karaoke.mp4` | original picture, size and frame rate, with the instrumental as the soundtrack |
| `lyrics.txt` | the lyrics shown on screen; edit this one |
| `lyrics.lrc` | enhanced LRC with word times, for karaoke apps and music players |
| `lyrics.json` | word timings with confidence values |
| `missing.json` | sung parts with no lyrics (only when there are any) |
| `stems/<mode>-<quality>/` | `instrumental.wav`, `vocals.wav` and the cached recognition |

On screen: two lines at the bottom, the next line readable before it is sung, each word
filling from white to yellow while it is sung, and a 3-2-1 countdown after long
instrumental parts.

### Options

| Option | |
|---|---|
| `--vocals all\|lead` | how the vocals are removed (asked when missing) |
| `--quality light\|fast\|high` | separation models, see above |
| `--preview-vocals` | 20 s excerpt in both modes, then stop (`--preview-start SECONDS`) |
| `--out DIR` | output folder |
| `--lang tr` | song language (default `tr`); used for recognition, casing rules and subtitles |
| `--max-height 1080` | YouTube download height limit; `0` = best available (4K renders much slower) |
| `--no-subs` | don't use the YouTube video's subtitles |
| `--no-gap-pass` | don't re-listen to sung parts the first recognition left empty |
| `--retranscribe` | run speech recognition again |
| `--force` | redo vocal removal and recognition |
| `--lyrics FILE` | use another lyrics file instead of `<out>/lyrics.txt` |
| `--band` | translucent band behind the lyrics, for busy footage |
| `--title-card` | show artist and title during the intro |
| `--keep-original-audio` | add the original song as a second audio track |
| `--whisper MODEL` | Whisper model (default `large-v3-turbo`) |
| `--asr-backend` | `auto`, `mlx` (Apple Silicon), `faster-whisper`, `whisper` |
| `--device` | `auto`, `mps`, `cuda`, `cpu` (for Demucs) |
| `--no-video` | stop after `lyrics.json` / `lyrics.lrc` |

## How it works

| Step | What happens | Tools |
|---|---|---|
| Input | local file used as is; YouTube link downloaded with yt-dlp (best picture and sound, up to 1080p); other links downloaded as a file. Audio is placed on the video's clock. | yt-dlp, deno, ffmpeg |
| Vocals | removed the way you chose | Demucs (`all`), audio-separator with UVR / MelBand-RoFormer karaoke models (`lead`) |
| Recognition | Whisper on the removed vocal, cut along its own pauses; sung parts left empty are transcribed again one by one | stable-ts with mlx-whisper (Mac) or faster-whisper (Windows) |
| Subtitles | for YouTube links, the video's own subtitles in the song's language: uploaded ones become the lyrics draft, automatic ones fill parts recognition missed; their offset is measured and they are ignored if they don't follow the singing | yt-dlp |
| Timing | every word of `lyrics.txt` is timed against the recognised words | `mv_karaoke/match.py` |
| Video | lyrics burned onto the original picture, instrumental as the soundtrack | ffmpeg + libass; VideoToolbox on Mac |

The text on screen is always exactly what is in `lyrics.txt`; recognition only decides
*when* each word is sung. Both sides are normalised (Turkish-aware, so `IŞIK` = `ışık`) and
aligned as sequences with fuzzy word similarity. Split or merged words are allowed, and
missed lines, ad-libs and hallucinations can't pull neighbouring words out of place. Strong
matches become anchors, and words without a match are placed where singing was heard, in
proportion to their syllables. A chorus written once but sung twice is detected.

## Using it as a Claude Code skill

Clone the repository into your skills folder and install it there:

```bash
git clone https://github.com/ilkayayas/mv-karaoke.git ~/.claude/skills/mv-karaoke
cd ~/.claude/skills/mv-karaoke
# then follow the installation guide for your system from step "Create the environment"
```

Then ask for a karaoke video of a clip or link. The skill asks how to remove the vocals
before anything is processed, and afterwards points you to `lyrics.txt` and the parts to check.

The skill needs a local installation (ffmpeg, PyTorch and about 3.5 GB of models), so it is
meant for Claude Code on your own machine, not for uploading to a hosted chat.

## Tests

```bash
python -m unittest discover -s tests      # no models or network needed
python tests/bench_match.py               # randomised benchmark of the lyric timing
```

## Troubleshooting

See the troubleshooting sections of the [macOS](docs/INSTALL-macOS.md#troubleshooting) and
[Windows](docs/INSTALL-Windows.md#troubleshooting) guides. The most common fix for a
failing YouTube download is `pip install -U yt-dlp`.

## Rights

Music videos and recordings are copyrighted. The karaoke videos you make with this tool are
derived from them: keep them for personal use unless you hold the rights. Lyrics are never
looked up online; they come from the video itself and stay on your computer.

## License and credits

MIT, see [LICENSE](LICENSE).

Built on [Demucs](https://github.com/adefossez/demucs) (MIT),
[audio-separator](https://github.com/nomadkaraoke/python-audio-separator) (MIT) and the
UVR community karaoke models, [Whisper](https://github.com/openai/whisper) (MIT) via
[stable-ts](https://github.com/jianfch/stable-ts), [mlx-whisper](https://github.com/ml-explore/mlx-examples)
and [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [yt-dlp](https://github.com/yt-dlp/yt-dlp)
and [ffmpeg](https://ffmpeg.org) with libass. Model weights are not part of this repository;
each is downloaded from its source under its own license. The bundled font is DejaVu Sans
Bold ([license](mv_karaoke/fonts/DejaVu-LICENSE.txt)).

Ideas from [karaoke-maker](https://github.com/chad-nethery/karaoke-maker) and
[ai-music-video-skill](https://github.com/Dracowyn/ai-music-video-skill), both MIT.
