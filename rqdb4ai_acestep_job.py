"""RQDB4AI entrypoint: run one ACE-Step 1.5 (Turbo XL) song generation on 0.14, serialized.

0.14 の GPU は ollama(gemma)・H3(ComfyUI)・ACE-Step で共有する。直列化は H3 と同じく
「rqdb4ai-h3-worker の単一プロセスが 0.14 の全キュー(ollama web/worker + h3 + acestep)を
順に処理する」ことで実現する。ACE-Step の API サーバーはモデルを降ろす口を持たず、
XL Turbo は待機中も VRAM 16.6GB / RAM 十数GB を握り続ける(2026-09-16 実測)。
そのため API サーバーは常駐させず、**ジョブの間だけ 0.14 の user unit を起動し、終わったら止める**。
起動→初回生成は 50 秒(XL 20GB のロード込み)。

失敗の実例(2026-09-16): 生成の合間に別ジョブが 0.14 の Ollama に gemma を再ロード(6.3GB)し、
ACE-Step 16.6GB と合わせて VRAM が尽き「Insufficient free VRAM」。だから開始時に Ollama を
アンロードし、ワーカーの直列化で他ジョブを割り込ませない。

worker(0.3) から ssh で unit を start/stop し、HTTP で生成し、完成 mp3 を 0.3 のローカルへ保存する。
"""
from __future__ import annotations

import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

SSH = ["ssh", "-p", "2222", "-i", "/home/kojima/.ssh/id_kfreqai_backup",
       "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "192.168.0.14"]
UNIT = "acestep-api.service"


def _unit(action: str) -> str:
    r = subprocess.run(SSH + [f"systemctl --user {action} {UNIT}"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"systemctl {action} failed: {r.stderr.strip()[:200]}")
    return f"unit_{action}"


def _unload_ollama(ollama_url: str, timeout: int = 150) -> str:
    ollama_url = ollama_url.rstrip("/")
    try:
        ps = requests.get(f"{ollama_url}/api/ps", timeout=10).json().get("models", [])
    except Exception:
        ps = []
    for m in ps:
        try:
            requests.post(f"{ollama_url}/api/generate",
                          json={"model": m["name"], "keep_alive": 0, "prompt": ""}, timeout=30)
        except Exception:
            pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            n = len(requests.get(f"{ollama_url}/api/ps", timeout=10).json().get("models", []))
        except Exception:
            n = 0
        if n == 0:
            return "vram_free"
        time.sleep(5)
    return "vram_wait_timeout"


def _wait_health(api_url: str, timeout: int = 180) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(f"{api_url}/health", timeout=5).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(3)
    raise RuntimeError("acestep-api did not become healthy")


def acestep_generate_job(
    prompt: str,
    lyrics: str,
    output_filename: str,
    audio_duration: float = 60,
    vocal_language: str = "ja",
    model: str = "acestep-v15-xl-turbo",
    audio_format: str = "mp3",
    extra: dict[str, Any] | None = None,
    api_url: str = "http://192.168.0.14:18300",
    ollama_url: str = "http://192.168.0.14:11434",
    save_dir: str = "/home/kojima/work/kpvgen/outputs/acestep_queue",
    poll_interval: int = 5,
    generation_timeout: int = 1500,
    source: str = "rqdb4ai",
    **_: Any,
) -> dict[str, Any]:
    """prompt=曲の説明(英語推奨)、lyrics=[verse]/[chorus] 付き歌詞(インストは "[instrumental]")。
    audio_duration は 10〜600 秒。戻り値の audio_path が 0.3 上の mp3。"""
    if not prompt or not output_filename:
        raise RuntimeError("prompt and output_filename are required")
    api_url = api_url.rstrip("/")
    steps: list[str] = [_unload_ollama(ollama_url)]
    steps.append(_unit("start"))
    try:
        _wait_health(api_url)
        body = {"prompt": prompt, "lyrics": lyrics or "[instrumental]", "vocal_language": vocal_language,
                "audio_duration": float(audio_duration), "audio_format": audio_format, "model": model,
                "thinking": False}
        if extra:
            body.update(extra)
        r = requests.post(f"{api_url}/release_task", json=body, timeout=120)
        r.raise_for_status()
        tid = (r.json().get("data") or {}).get("task_id")
        if not tid:
            raise RuntimeError(f"release_task returned no task_id: {r.text[:200]}")
        steps.append(f"submitted:{tid}")
        deadline = time.time() + generation_timeout
        item: dict[str, Any] = {}
        while time.time() < deadline:
            time.sleep(poll_interval)
            try:
                q = requests.post(f"{api_url}/query_result", json={"task_id_list": [tid]}, timeout=30).json()
            except Exception:
                continue
            items = q.get("data") or []
            item = items[0] if items else {}
            if item.get("status") in (1, 2):
                break
        if item.get("status") != 1:
            raise RuntimeError(f"acestep generation failed or timed out: {str(item)[:300]}")
        import json as _json
        result = item.get("result")
        files = _json.loads(result) if isinstance(result, str) else (result or [])
        if not files or not files[0].get("file"):
            raise RuntimeError("acestep returned no audio file")
        file_url = files[0]["file"]  # "/v1/audio?path=%2F..."
        a = requests.get(api_url + file_url, timeout=300)
        a.raise_for_status()
        Path(save_dir).mkdir(parents=True, exist_ok=True)
        dst = Path(save_dir) / output_filename
        dst.write_bytes(a.content)
        steps.append(f"downloaded:{dst}")
        metas = files[0].get("metas") or {}
        return {"audio_path": str(dst), "bytes": len(a.content), "metas": metas, "steps": steps, "source": source}
    finally:
        # 成功・失敗・タイムアウトのどれでも API を止めて VRAM/RAM を返す(H3 の /free と同じ思想)。
        try:
            steps.append(_unit("stop"))
        except Exception as exc:  # 止められなくても結果は返す。次のジョブの start で復帰する。
            steps.append(f"unit_stop_failed:{str(exc)[:100]}")
