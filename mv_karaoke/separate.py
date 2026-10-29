"""Vocal separation for both modes, on CUDA, Apple Silicon (MPS) or CPU.

  all  -> Demucs htdemucs / htdemucs_ft: instrumental = drums + bass + other
  lead -> audio-separator with a MelBand-RoFormer karaoke model: instrumental keeps backing vocals

Outputs in the work folder: instrumental.wav and vocals.wav (the removed part; for
'lead' that is the lead vocal only). The vocal file is what lyric timing listens to.
Stems are cached per mode, so switching modes later doesn't redo the other one.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .vocals import VocalMode

DEMUCS_MODELS = {"light": "htdemucs", "fast": "htdemucs", "high": "htdemucs_ft"}
KARAOKE_MODELS = {
    # 52 MB MDX-Net karaoke model: low-RAM machines, CPU-only Windows, quick tests
    "light": ["UVR_MDXNET_KARA_2.onnx"],
    # 1.7 GB MelBand-RoFormer karaoke model: clearly better lead/backing split, needs ~8 GB RAM
    "fast": ["mel_band_roformer_karaoke_becruily.ckpt"],
    # audio-separator's community "karaoke" ensemble: three karaoke models averaged
    "high": ["mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt",
             "mel_band_roformer_karaoke_gabox_v2.ckpt",
             "mel_band_roformer_karaoke_becruily.ckpt"],
}


@dataclass
class Stems:
    mode: VocalMode
    instrumental: Path
    vocals: Path
    model: str
    device: str


def pick_device(prefer: str = "auto") -> str:
    import torch
    if prefer != "auto":
        return prefer
    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


def stem_dir(work: Path, mode: VocalMode, quality: str) -> Path:
    return work / "stems" / f"{mode.value}-{quality}"


def separate(source_wav: Path, work: Path, mode: VocalMode, quality: str = "fast",
             device: str = "auto", force: bool = False, log=print) -> Stems:
    out = stem_dir(work, mode, quality)
    inst, voc = out / "instrumental.wav", out / "vocals.wav"
    if not force and inst.exists() and voc.exists():
        log(f"stems for '{mode.value}' already in {out}, reusing them")
        return Stems(mode, inst, voc, "cached", "cached")
    out.mkdir(parents=True, exist_ok=True)
    if mode is VocalMode.ALL:
        return _demucs(source_wav, out, DEMUCS_MODELS[quality], device, log)
    return _karaoke(source_wav, out, KARAOKE_MODELS[quality], device, log)


def _demucs(source: Path, out: Path, model: str, device: str, log) -> Stems:
    import soundfile as sf
    from demucs.api import Separator

    dev = pick_device(device)
    log(f"removing all vocals: Demucs '{model}' on {dev}")
    sep = Separator(model=model, device=dev, shifts=1, progress=True)
    _, stems = sep.separate_audio_file(str(source))
    voc = stems["vocals"]
    acc = sum(t for k, t in stems.items() if k != "vocals")
    sf.write(str(out / "vocals.wav"), voc.T.cpu().numpy(), sep.samplerate, subtype="PCM_16")
    sf.write(str(out / "instrumental.wav"), acc.T.cpu().numpy(), sep.samplerate, subtype="PCM_16")
    return Stems(VocalMode.ALL, out / "instrumental.wav", out / "vocals.wav", model, dev)


VOCAL_STEM = re.compile(r"\((?:vocals?|lead[ _-]?vocals?)\)", re.I)
INST_STEM = re.compile(r"\((?:instrumental|inst|no[ _-]?vocals?|backing[ _-]?(?:track|vocals?)?)\)", re.I)


def low_band_share(path: Path, cutoff: float = 150.0) -> float:
    """Share of a file's energy below `cutoff` Hz (bass and kick live there, voices don't)."""
    import numpy as np
    import soundfile as sf
    x, sr = sf.read(str(path), dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    hop = sr * 10
    lo = tot = 0.0
    for s in range(0, max(1, len(x) - sr), hop):            # 1 s every 10 s is plenty
        seg = x[s:s + sr]
        if len(seg) < sr // 2:
            break
        p = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
        f = np.fft.rfftfreq(len(seg), 1 / sr)
        lo += float(p[f < cutoff].sum())
        tot += float(p.sum())
    return lo / tot if tot else 0.0


def classify_outputs(files: list[str]) -> tuple[Path, Path]:
    """audio-separator names files '<input>_(<Stem>)_<model>.wav'. Returns (instrumental, lead vocal).
    Stem names differ between karaoke models ('Karaoke' can mean either side), so when the
    names don't settle it the file with more bass is taken as the instrumental."""
    voc = [f for f in files if VOCAL_STEM.search(Path(f).name)]
    inst = [f for f in files if INST_STEM.search(Path(f).name)]
    if len(voc) == 1 and len(inst) == 1 and voc[0] != inst[0]:
        return Path(inst[0]), Path(voc[0])
    if len(files) != 2:
        raise RuntimeError("expected two stems from the karaoke model, got: " + ", ".join(Path(f).name for f in files))
    a, b = (Path(f) for f in files)
    return (a, b) if low_band_share(a) >= low_band_share(b) else (b, a)


def _karaoke(source: Path, out: Path, models: list[str], device: str, log) -> Stems:
    import logging

    from audio_separator.separator import Separator

    model_dir = Path.home() / ".mv-karaoke" / "models" / "audio-separator"
    model_dir.mkdir(parents=True, exist_ok=True)
    tmp = out / "_raw"
    tmp.mkdir(exist_ok=True)
    log(f"removing the lead vocal only: {', '.join(models)}")
    sep = Separator(log_level=logging.WARNING, model_file_dir=str(model_dir), output_dir=str(tmp),
                    output_format="WAV", sample_rate=44100,
                    ensemble_algorithm="avg_wave" if len(models) > 1 else None)
    sep.load_model(model_filename=models if len(models) > 1 else models[0])
    files = sep.separate(str(source))
    files = [f if Path(f).is_absolute() else str(tmp / f) for f in files]
    inst, voc = classify_outputs(files)
    shutil.move(str(inst), out / "instrumental.wav")
    shutil.move(str(voc), out / "vocals.wav")
    shutil.rmtree(tmp, ignore_errors=True)
    dev = str(getattr(sep, "torch_device", "auto"))
    return Stems(VocalMode.LEAD, out / "instrumental.wav", out / "vocals.wav", "+".join(models), dev)
