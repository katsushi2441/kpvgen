#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MiniMax H3（ローカル ComfyUI）の API ワークフローを組み立てる。

なぜファイルで持つか（2026-09-05）:
  以前は作業用の一時ディレクトリに JSON を置いていたため、セッションが変わって
  失われた。ComfyUI の履歴も再起動で空になり、復元に時間がかかった。
  ノード構成はここを正とする。

構成（0.14 の ComfyUI 8001 の /object_info で確認済み）:
  UNETLoader → LoraLoaderModelOnly（turbo 8step）
  CLIPLoader(type=minimax) / VAELoader(video, audio)
  → MiniMaxH3ImageToVideo（positive, LATENT を返す）
  → BasicGuider + BasicScheduler(simple, 8) + KSamplerSelect(res_multistep) + RandomNoise
  → SamplerCustomAdvanced → VAEDecode(映像) / VAEDecodeAudio(音声)
  → CreateVideo(24fps, audio) → SaveVideo

仕様:
  短辺768まで（最大 768x1344）・24fps・フレーム数は 17k+5（8秒=192, 15秒=362）
  24GB機では 1344x768 の単発8秒(192f)までが実用。294f/362f は活性値でOOMする。

ライセンス:
  H3 Community License。年商$20M未満は商用可・**「MiniMax H3」表記が必須**・日本は対象。
  生成物を公開する際はクレジットを必ず入れること。
"""

UNET = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
CLIP = "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"
LORA = "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"
VAE_VIDEO = "minimax_h3_video_vae_fp16.safetensors"
VAE_AUDIO = "minimax_h3_audio_vae_fp32.safetensors"


def frames_for(seconds: float, fps: int = 24) -> int:
    """フレーム数は 17k+5 のグリッドに乗せる（H3の制約）。"""
    n = int(round(seconds * fps))
    k = max(1, round((n - 5) / 17))
    return 17 * k + 5


def build(prompt: str, width: int = 1344, height: int = 768,
          length: int = 192, seed: int = 12345, steps: int = 8,
          filename_prefix: str = "h3") -> dict:
    """ComfyUI の /prompt に投げる API 形式のワークフローを返す。"""
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": UNET, "weight_dtype": "default"}},
        "2": {"class_type": "LoraLoaderModelOnly",
              "inputs": {"model": ["1", 0], "lora_name": LORA, "strength_model": 1.0}},
        "3": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": CLIP, "type": "minimax"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_VIDEO}},
        "5": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_AUDIO}},
        "6": {"class_type": "MiniMaxH3ImageToVideo",
              "inputs": {"clip": ["3", 0], "vae": ["4", 0], "prompt": prompt,
                         "width": width, "height": height, "length": length}},
        "7": {"class_type": "BasicGuider",
              "inputs": {"model": ["2", 0], "conditioning": ["6", 0]}},
        "8": {"class_type": "BasicScheduler",
              "inputs": {"model": ["2", 0], "scheduler": "simple",
                         "steps": steps, "denoise": 1.0}},
        "9": {"class_type": "KSamplerSelect",
              "inputs": {"sampler_name": "res_multistep"}},
        "10": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "11": {"class_type": "SamplerCustomAdvanced",
               "inputs": {"noise": ["10", 0], "guider": ["7", 0], "sampler": ["9", 0],
                          "sigmas": ["8", 0], "latent_image": ["6", 1]}},
        "12": {"class_type": "VAEDecode",
               "inputs": {"samples": ["11", 0], "vae": ["4", 0]}},
        "13": {"class_type": "VAEDecodeAudio",
               "inputs": {"samples": ["11", 0], "vae": ["5", 0]}},
        "14": {"class_type": "CreateVideo",
               "inputs": {"images": ["12", 0], "fps": 24.0, "audio": ["13", 0]}},
        "15": {"class_type": "SaveVideo",
               "inputs": {"video": ["14", 0], "filename_prefix": filename_prefix,
                          "format": "mp4"}},
    }


if __name__ == "__main__":
    import json
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else "a test clip"
    print(json.dumps(build(p), ensure_ascii=False, indent=1))
