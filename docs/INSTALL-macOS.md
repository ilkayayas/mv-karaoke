# Installing on macOS (Apple Silicon)

Tested on a Mac mini M2 Pro with 32 GB RAM. Any Apple Silicon Mac (M1 or newer) works;
with less than 16 GB RAM use `--quality light` for the `lead` mode. Intel Macs are not
supported.

You need about 6 GB of free disk space: around 2.5 GB for the Python packages and around
3.5 GB for the models, which are downloaded on the first run.

All commands go into the **Terminal** app.

## 1. Install Homebrew

Skip this if `brew --version` already prints a version.

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

At the end the installer prints two or three `echo ... >> ~/.zprofile` / `eval ...` lines;
run them, then close and reopen Terminal.

## 2. Install Python 3.12, ffmpeg, deno and git

```bash
brew install python@3.12 ffmpeg deno git
```

- **ffmpeg** reads and writes the video and draws the lyrics
- **deno** is needed by yt-dlp for YouTube downloads
- Python **3.12** is the tested version; 3.11 and 3.13 should also work

Check that ffmpeg can draw subtitles (libass):

```bash
ffmpeg -hide_banner -filters | grep " ass "
```

One line should appear. If nothing appears, replace ffmpeg with the full build:

```bash
brew uninstall ffmpeg
brew tap homebrew-ffmpeg/ffmpeg
brew install homebrew-ffmpeg/ffmpeg/ffmpeg
```

## 3. Get the code

```bash
mkdir -p ~/Projects && cd ~/Projects
git clone https://github.com/ilkayayas/mv-karaoke.git
cd mv-karaoke
```

## 4. Create the environment and install the packages

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[demucs,karaoke,align-mac]"
```

The prompt now starts with `(.venv)`. The install takes a few minutes.

| Extra | Installs |
|---|---|
| `demucs` | Demucs, for `--vocals all` |
| `karaoke` | audio-separator and the karaoke models' code, for `--vocals lead` |
| `align-mac` | stable-ts with mlx-whisper: speech recognition on the Apple GPU |

## 5. Check the installation

```bash
python -m unittest discover -s tests
```

It ends with `OK` (a few tests may be reported as skipped).

## 6. Make your first karaoke video

```bash
python -m mv_karaoke.cli ~/Movies/clip.mp4 --vocals lead
python -m mv_karaoke.cli "https://www.youtube.com/watch?v=VIDEO_ID" --vocals all
```

Put links in quotes: zsh treats `?` as a wildcard and stops with `no matches found`.

The first run downloads the models (about 3.5 GB) and takes noticeably longer. The models
are kept, so later runs start right away.

Open the result:

```bash
open karaoke/clip/karaoke.mp4
```

## 7. Correct the lyrics

```bash
open -e karaoke/clip/lyrics.txt       # opens in TextEdit
```

Fix wrong words and letters, add missing lines at the times the tool listed, save, and run
**the same command** again. Only the timing and the video are redone.

## Every time you open a new Terminal

```bash
cd ~/Projects/mv-karaoke
source .venv/bin/activate
```

## Updating

```bash
cd ~/Projects/mv-karaoke
source .venv/bin/activate
git pull
pip install -e ".[demucs,karaoke,align-mac]"
pip install -U yt-dlp
```

## Removing

Delete the project folder, and if you want the disk space back, the downloaded models:

```bash
rm -rf ~/Projects/mv-karaoke ~/.mv-karaoke
rm -rf ~/.cache/huggingface ~/.cache/torch     # also used by other AI tools you may have
```

## Troubleshooting

**`zsh: no matches found: https://...`**
Put the link in quotes.

**`ffmpeg not found on PATH`**
Run `brew install ffmpeg`, then open a new Terminal window.

**`No such filter: 'ass'` while rendering**
Your ffmpeg has no libass; see step 2.

**`ModuleNotFoundError: No module named ...`**
The environment is not active (no `(.venv)` in the prompt) or the install did not finish:
run `source .venv/bin/activate` and repeat step 4.

**YouTube download fails**
Update yt-dlp, YouTube changes often: `pip install -U yt-dlp`. Make sure deno is installed
(`deno --version`). As a last resort download the video yourself and pass the file.

**Lines are missing in the lead mode**
The karaoke model sometimes takes parts of the lead vocal for backing vocals. Try
`--quality high`, or `--vocals all`. If `lyrics.txt` is still the untouched draft, the next
run refreshes it; otherwise add the missing lines by hand.

**The fan spins up**
Vocal removal and speech recognition keep the CPU and GPU busy for several minutes. The fan
calms down a few minutes after the run ends.
