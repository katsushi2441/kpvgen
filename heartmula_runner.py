#!/usr/bin/env python3
"""HeartMuLa oss-3B を 0.14 で1回実行するランナー（rqdb4ai_heartmula_job が毎回 scp して使う）。

upstream の examples/run_music_generation.py と同じ引数だが、保存を torchaudio.save に頼らない。
torchaudio 2.10 は保存に torchcodec を要求し、0.14（FFmpeg 4.4 + CUDA 12.8）では torchcodec が
読み込めない（2026-09-16 実測）。そこで torchaudio.save を soundfile(wav) → ffmpeg CLI(mp3) に差し替える。
生成本体（HeartMuLaGenPipeline）には触らない。
"""
import argparse
import subprocess
import sys
from pathlib import Path

import soundfile as sf
import torch
import torchaudio


def _save(save_path, wav, sample_rate):
    save_path = Path(save_path)
    wav_path = save_path.with_suffix(".wav")
    sf.write(str(wav_path), wav.to(torch.float32).cpu().numpy().T, int(sample_rate))
    if save_path.suffix.lower() == ".mp3":
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav_path), "-b:a", "192k", str(save_path)], check=True)
        wav_path.unlink(missing_ok=True)
    elif save_path != wav_path:
        wav_path.replace(save_path)


torchaudio.save = _save  # pipeline の postprocess が呼ぶ

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from heartlib import HeartMuLaGenPipeline  # noqa: E402


def str2dtype(s):
    return {"bf16": torch.bfloat16, "bfloat16": torch.bfloat16, "fp16": torch.float16, "float16": torch.float16,
            "fp32": torch.float32, "float32": torch.float32}[s]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model_path", required=True)
    p.add_argument("--version", default="3B")
    p.add_argument("--lyrics", required=True)
    p.add_argument("--tags", required=True)
    p.add_argument("--save_path", required=True)
    p.add_argument("--max_audio_length_ms", type=int, default=240_000)
    p.add_argument("--topk", type=int, default=50)
    p.add_argument("--temperature", type=float, default=1.0)
    p.add_argument("--cfg_scale", type=float, default=1.5)
    p.add_argument("--mula_dtype", type=str2dtype, default=torch.bfloat16)
    p.add_argument("--codec_dtype", type=str2dtype, default=torch.float32)
    p.add_argument("--lazy_load", default="true")
    a = p.parse_args()
    lazy = str(a.lazy_load).lower() in ("1", "true", "yes")
    pipe = HeartMuLaGenPipeline.from_pretrained(
        a.model_path,
        device={"mula": torch.device("cuda"), "codec": torch.device("cuda")},
        dtype={"mula": a.mula_dtype, "codec": a.codec_dtype},
        version=a.version, lazy_load=lazy,
    )
    # upstream と同じく lyrics / tags は「ファイルパス」を渡す（pipeline 側が読む）
    with torch.no_grad():
        pipe({"lyrics": a.lyrics, "tags": a.tags},
             max_audio_length_ms=a.max_audio_length_ms, save_path=a.save_path,
             topk=a.topk, temperature=a.temperature, cfg_scale=a.cfg_scale)
    print(f"Generated music saved to {a.save_path}")


if __name__ == "__main__":
    main()
