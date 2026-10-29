# Installing on Windows 10 / 11

Works on a normal 64-bit Windows PC; a graphics card is not needed. Without an NVIDIA
graphics card everything runs on the CPU, which takes several times longer than on a Mac
with Apple Silicon. 16 GB RAM or more is recommended; with less, use `--quality light`.

You need about 6 GB of free disk space: around 2.5 GB for the Python packages and around
3.5 GB for the models, which are downloaded on the first run.

All commands go into **PowerShell** (Start menu → type `PowerShell`). Windows Terminal works
too.

## 1. Install Python 3.12, ffmpeg, deno and git

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Gyan.FFmpeg
winget install -e --id DenoLand.Deno
winget install -e --id Git.Git
winget install -e --id Microsoft.VCRedist.2015+.x64
```

- **ffmpeg** reads and writes the video and draws the lyrics
- **deno** is needed by yt-dlp for YouTube downloads
- the **Visual C++ runtime** is needed by PyTorch; it is often installed already

**Close PowerShell and open a new window** so the new programs are found. Then check:

```powershell
py -3.12 --version
ffmpeg -hide_banner -filters | findstr /C:" ass "
deno --version
```

The ffmpeg line must print one line containing `ass` (subtitle drawing with libass).

## 2. Get the code

```powershell
mkdir $HOME\Projects -Force
cd $HOME\Projects
git clone https://github.com/ilkayayas/mv-karaoke.git
cd mv-karaoke
```

## 3. Create the environment and install the packages

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e ".[demucs,karaoke,align]"
```

If activation stops with *"running scripts is disabled on this system"*, allow local scripts
once and activate again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
.venv\Scripts\Activate.ps1
```

(In the classic Command Prompt use `.venv\Scripts\activate.bat` instead.)

The prompt now starts with `(.venv)`. The install takes a few minutes.

| Extra | Installs |
|---|---|
| `demucs` | Demucs, for `--vocals all` |
| `karaoke` | audio-separator and the karaoke models' code, for `--vocals lead` |
| `align` | stable-ts with faster-whisper: speech recognition on the CPU (int8) |

### Optional: NVIDIA graphics card

With an NVIDIA card, install the CUDA build of PyTorch and the GPU runtime afterwards; vocal
removal and speech recognition then run on the card:

```powershell
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128 --force-reinstall
pip install "audio-separator[gpu]"
```

## 4. Check the installation

```powershell
python -m unittest discover -s tests
```

It ends with `OK` (a few tests may be reported as skipped).

## 5. Make your first karaoke video

```powershell
python -m mv_karaoke.cli "$HOME\Videos\clip.mp4" --vocals lead --quality light
python -m mv_karaoke.cli "https://www.youtube.com/watch?v=VIDEO_ID" --vocals all
```

Put links and paths with spaces in quotes.

The first run downloads the models (about 3.5 GB) and takes noticeably longer. The very
first video render can also pause for a minute while ffmpeg builds its font cache. Both
happen only once.

On a CPU-only PC, `--quality light` is a good first try for the `lead` mode; switch to the
default (`fast`) or `high` for the final video.

Open the result:

```powershell
start karaoke\clip\karaoke.mp4
```

## 6. Correct the lyrics

```powershell
notepad karaoke\clip\lyrics.txt
```

Fix wrong words and letters, add missing lines at the times the tool listed, save (Notepad
keeps UTF-8), and run **the same command** again. Only the timing and the video are redone.

## Every time you open a new PowerShell window

```powershell
cd $HOME\Projects\mv-karaoke
.venv\Scripts\Activate.ps1
```

## Updating

```powershell
cd $HOME\Projects\mv-karaoke
.venv\Scripts\Activate.ps1
git pull
pip install -e ".[demucs,karaoke,align]"
pip install -U yt-dlp
```

## Removing

Delete the project folder, and if you want the disk space back, the downloaded models:

```powershell
Remove-Item -Recurse -Force $HOME\Projects\mv-karaoke, $HOME\.mv-karaoke
Remove-Item -Recurse -Force $HOME\.cache\huggingface, $HOME\.cache\torch   # also used by other AI tools
```

## Troubleshooting

**`'ffmpeg' is not recognized` / `ffmpeg not found on PATH`**
Open a new PowerShell window after installing. If it still fails, run
`winget install -e --id Gyan.FFmpeg` again and sign out and back in to Windows.

**`No such filter: 'ass'` while rendering**
Your ffmpeg has no libass. Install the Gyan build from step 1 and make sure it comes first
on PATH (`where.exe ffmpeg` shows which one is used).

**`DLL load failed while importing ...` (torch)**
Install the Visual C++ runtime: `winget install -e --id Microsoft.VCRedist.2015+.x64`.

**`running scripts is disabled on this system`**
See step 3.

**`ModuleNotFoundError: No module named ...`**
The environment is not active (no `(.venv)` in the prompt) or the install did not finish:
activate it and repeat step 3.

**Warning about symlinks from huggingface_hub**
Harmless: Windows without Developer Mode stores the models as copies instead of links. To
hide it: `setx HF_HUB_DISABLE_SYMLINKS_WARNING 1`.

**YouTube download fails**
Update yt-dlp, YouTube changes often: `pip install -U yt-dlp`. Make sure deno is installed
(`deno --version`). As a last resort download the video yourself and pass the file.

**It is slow**
Without an NVIDIA card everything runs on the CPU. Use `--quality light` while trying
things out, keep `--max-height 1080` (the default) for YouTube, and render the final video
once you are happy with `lyrics.txt`.
