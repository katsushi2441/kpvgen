"""RQDB4AI entrypoint: run one MiniMax H3 (ComfyUI) generation on 0.14, serialized.

0.14のGPUはollama(gemma・kmontage台本用)とH3で共有する。直列化は「1つのワーカーが
0.14の全キュー(ollama web/worker + h3)を順に処理する」ことで実現する(rqdb4ai-worker
の単一プロセスが一度に1ジョブ→H3実行中はollamaジョブが走らない)。停止操作は不要。
唯一の残課題はgemmaのVRAM常駐(前のジョブのgemmaが残ると20GBのH3が入らない)なので、
H3開始時にgemmaをアンロードしてVRAMを空けてから生成する。

worker(0.3)からHTTPで0.14を操作し、完成mp4を0.3のローカルへ保存してパスを返す。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests

def _comfy_free(comfy_url: str) -> None:
    """ComfyUIにモデルのアンロードとメモリ解放を要求(待機中に約43GB握り続ける対策)。失敗は無視。"""
    try:
        requests.post(f"{comfy_url.rstrip('/')}/free",
                      json={"unload_models": True, "free_memory": True}, timeout=20)
    except Exception:
        pass


def _unload_ollama(ollama_url: str, timeout: int = 150) -> str:
    ollama_url = ollama_url.rstrip("/")
    try:
        ps = requests.get(f"{ollama_url}/api/ps", timeout=10).json().get("models", [])
    except Exception:
        ps = []
    for m in ps:
        try:
            requests.post(f"{ollama_url}/api/generate",
                          json={"model": m["name"], "keep_alive": 0, "prompt": ""},
                          timeout=30)
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


def h3_generate_job(
    workflow: dict[str, Any],
    output_filename: str,
    comfy_url: str = "http://192.168.0.14:8001",
    ollama_url: str = "http://192.168.0.14:11434",
    save_dir: str = "/home/kojima/work/kpvgen/outputs/h3_queue",
    poll_interval: int = 10,
    generation_timeout: int = 3000,
    source: str = "rqdb4ai",
    **_: Any,
) -> dict[str, Any]:
    if not isinstance(workflow, dict) or not workflow:
        raise RuntimeError("workflow(dict) is required")
    comfy_url = comfy_url.rstrip("/")
    steps: list[str] = []
    steps.append(_unload_ollama(ollama_url))
    # ComfyUIはH3モデル(約43GB)をRAMに保持し続け待機中も解放しない(0.14のメモリ逼迫の主因)。
    # 前回の残骸を掃除してから生成し、生成後も解放する。
    _comfy_free(comfy_url)
    r = requests.post(f"{comfy_url}/prompt", json={"prompt": workflow}, timeout=30)
    r.raise_for_status()
    pid = r.json().get("prompt_id")
    if not pid:
        raise RuntimeError("comfy returned no prompt_id")
    steps.append(f"submitted:{pid}")

    deadline = time.time() + generation_timeout
    out_name = None
    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            hist = requests.get(f"{comfy_url}/history/{pid}", timeout=15).json()
        except Exception:
            continue
        if pid not in hist:
            continue
        st = hist[pid].get("status", {})
        if st.get("status_str") == "error":
            msg = ""
            for m in st.get("messages", []):
                if m[0] == "execution_error":
                    msg = str(m[1].get("exception_message"))[:400]
            raise RuntimeError(f"comfy execution error: {msg}")
        if st.get("completed"):
            for o in (hist[pid].get("outputs") or {}).values():
                for x in o.get("video", []) + o.get("videos", []) + o.get("gifs", []):
                    out_name = (x.get("filename"), x.get("subfolder", ""))
            break
    if not out_name:
        raise RuntimeError("H3 generation timed out or produced no video")

    fn, sub = out_name
    v = requests.get(f"{comfy_url}/view",
                     params={"filename": fn, "subfolder": sub, "type": "output"},
                     timeout=180)
    v.raise_for_status()
    Path(save_dir).mkdir(parents=True, exist_ok=True)
    dst = Path(save_dir) / output_filename
    dst.write_bytes(v.content)
    steps.append(f"downloaded:{dst}")
    _comfy_free(comfy_url)  # 生成した約43GBのモデルをRAMから解放
    steps.append("comfy_freed")
    return {"video_path": str(dst), "bytes": len(v.content), "steps": steps, "source": source}
