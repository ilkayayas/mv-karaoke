---
name: mv-karaoke
description: Turn a music video into a karaoke video that keeps the original picture - removes the vocals (all vocals, or only the lead singer, as the user chooses) and overlays word-by-word synced lyrics with a progressive highlight. The lyrics are taken from the song itself and saved next to the video for hand correction. Input is a local video file (mp4, mov, mkv, webm), a YouTube link, or a direct link to a video file. Use when the user wants a karaoke version of a music video or clip, or wants the singer removed from a video while keeping the picture. Runs locally on macOS (Apple Silicon) and Windows.
license: MIT
---

# mv-karaoke

`<skill>` below is this skill's folder.

## 0. Setup check

The tool runs from the skill's own Python environment:

- macOS: `PY=<skill>/.venv/bin/python`
- Windows: `PY=<skill>\.venv\Scripts\python.exe`

If that file doesn't exist, the skill is not installed yet: walk the user through
`docs/INSTALL-macOS.md` or `docs/INSTALL-Windows.md` (from the step "Create the environment").
Run the commands below with `$PY` instead of `python`, from the folder where the user wants
the output, or pass `--out DIR`.

## 1. Before processing anything: ask how to remove the vocals

Never pick this yourself and never fall back to a default. Ask, with both options and their trade-offs:

| Option | Flag | What happens |
|---|---|---|
| Remove all vocals | `--vocals all` | Lead, backing vocals, harmonies and choirs go. Cleanest, but vocal-heavy songs can sound hollow or show artefacts. |
| Remove the lead vocal only | `--vocals lead` | Only the lead singer goes; backing vocals stay, like professional karaoke tracks. Fuller sound; backing vocals may double some lines. |

Offer a third answer, "let me hear both first":

```
$PY -m mv_karaoke.cli <input> --preview-vocals [--preview-start SECONDS]
```

It writes `preview/preview_all.mp3` and `preview/preview_lead.mp3` (a 20 s excerpt). Give both files to
the user. Ask in the user's language (Turkish: "Tüm vokalleri sil" / "Yalnızca ana vokali sil" /
"Önce ikisini dinleyeyim").

## 2. Lyrics: never search for them, never ask for them

The first run takes the lyrics from the song with speech recognition on the removed vocal and writes
`lyrics.txt` next to `karaoke.mp4`. The video is made from that file. Don't look lyrics up anywhere and
don't ask the user to paste them. For a YouTube link the video's own subtitles in the song's language
are used when they exist (uploaded ones as the draft, automatic ones to fill gaps); `--no-subs` turns
that off.

## 3. Run

```
$PY -m mv_karaoke.cli "<video file | YouTube link | video file link>" --vocals all|lead [--quality light|fast|high]
```

| `--quality` | all | lead | Use when |
|---|---|---|---|
| `light` | htdemucs | MDX-Net karaoke (52 MB) | under 8 GB RAM, CPU-only Windows, quick tries |
| `fast` (default) | htdemucs | MelBand-RoFormer karaoke (1.7 GB) | normal use |
| `high` | htdemucs_ft | 3-model karaoke ensemble | final output, slower |

The input can be a video file, a YouTube link (downloaded with yt-dlp, at most 1080p;
`--max-height 0` for the best available) or a direct link to a video file (downloaded as a
file). Downloads are reused on later runs. If a YouTube download fails, run
`pip install -U yt-dlp` and retry.

Output folder (default `./karaoke/<video id or file name>/`): `karaoke.mp4`, `lyrics.txt`, `lyrics.lrc`,
`lyrics.json`, `stems/`. Other options: `--band` (translucent band behind the lyrics for busy footage),
`--keep-original-audio` (original song as a second audio track), `--title-card`.

## 4. After the first run

Tell the user where `lyrics.txt` is and that speech recognition makes mistakes, Turkish letters
especially (ı/i, ş/s, ğ/g, â), plus misheard, split or missing words. They correct the file by hand,
one sung line per line; a blank line starts a new stanza. Then run the same command again: separation
and recognition are reused, only the timing and the video are redone (seconds). An edited
`lyrics.txt` is never overwritten.

Show the lines listed under "lines to check" with their times; those are where the timing is unsure.
Also show the "singing heard but no lyrics placed" parts (times, and what was recognised there):
those are lines missing from `lyrics.txt`. The user adds them at the right place and runs again.

## Rights

The result is derived from a copyrighted recording and video. Personal use only; don't upload or sell
it unless the user holds the rights.
