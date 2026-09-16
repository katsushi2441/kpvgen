"""RQDB4AI entrypoint: run one HeartMuLa oss-3B song generation on 0.14, serialized.

ACE-Step と同じく 0.14 の GPU を ollama(gemma)・H3・ACE-Step と共有するので、
rqdb4ai-h3-worker の単一プロセスが処理するキュー(heartmula-192-168-0-14)に載せる。
HeartMuLa は API サーバーを持たず CLI(examples/run_music_generation.py)が 1 回ごとに
モデルをロードして終了するので、unit の start/stop は要らない(プロセス終了 = VRAM/RAM 解放)。
`--lazy_load true` で HeartMuLa(3B, bf16) と HeartCodec(fp32) を順にロード/解放し 24GB に収める。

worker(0.3) から ssh で 0.14 上の CLI を実行し、完成 mp3 を scp で 0.3 のローカルへ保存する。
歌詞は [Intro]/[Verse]/[Chorus]/[Bridge]/[Outro] 付き、タグは半角カンマ区切り(空白なし)。
"""
from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

HOST = "192.168.0.14"
SSH = ["ssh", "-p", "2222", "-i", "/home/kojima/.ssh/id_kfreqai_backup",
       "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", HOST]
SCP = ["scp", "-P", "2222", "-i", "/home/kojima/.ssh/id_kfreqai_backup", "-o", "BatchMode=yes"]
HEARTLIB = "/home/kojima/heartlib"


class _Res:
    def __init__(self, r):
        self.returncode = r.returncode
        self.stdout = (r.stdout or b"").decode("utf-8", "replace")
        self.stderr = (r.stderr or b"").decode("utf-8", "replace")


def _run(cmd: list[str], timeout: int, input_text: str | None = None) -> _Res:
    """text=True だと ssh の出力に非UTF-8バイトが混ざったとき decode で落ちる(2026-09-16)。バイトで受けて置換する。"""
    return _Res(subprocess.run(cmd, input=(input_text.encode("utf-8") if input_text is not None else None),
                               capture_output=True, timeout=timeout))


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


def heartmula_generate_job(
    lyrics: str,
    tags: str,
    output_filename: str,
    max_audio_length_ms: int = 60000,
    cfg_scale: float = 1.5,
    temperature: float = 1.0,
    topk: int = 50,
    ollama_url: str = "http://192.168.0.14:11434",
    save_dir: str = "/home/kojima/work/kpvgen/outputs/heartmula_queue",
    generation_timeout: int = 1800,
    source: str = "rqdb4ai",
    **_: Any,
) -> dict[str, Any]:
    if not lyrics or not tags or not output_filename:
        raise RuntimeError("lyrics, tags and output_filename are required")
    steps: list[str] = [_unload_ollama(ollama_url)]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    remote_dir = f"{HEARTLIB}/outputs/rq-{stamp}"
    # 入力ファイルを 0.14 に置く(ssh の stdin 経由。引数に歌詞を載せない)
    setup = (f"mkdir -p {shlex.quote(remote_dir)} && cat > {shlex.quote(remote_dir + '/lyrics.txt')} "
             f"&& printf %s {shlex.quote(tags.strip())} > {shlex.quote(remote_dir + '/tags.txt')}")
    r = _run(SSH + [setup], timeout=60, input_text=lyrics.strip() + "\n")
    if r.returncode != 0:
        raise RuntimeError(f"remote setup failed: {r.stderr.strip()[:200]}")
    steps.append(f"remote_dir:{remote_dir}")
    # 保存を torchaudio.save に頼らないランナー(kpvgen/heartmula_runner.py)を毎回送る(版管理は kpvgen 側)
    r = _run(SCP + [str(Path(__file__).with_name("heartmula_runner.py")),
                    f"{HOST}:{HEARTLIB}/examples/run_music_generation_sf.py"], timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"runner scp failed: {r.stderr.strip()[:200]}")
    cmd = (f"cd {HEARTLIB} && .venv/bin/python examples/run_music_generation_sf.py --model_path ./ckpt --version 3B "
           f"--lyrics {shlex.quote(remote_dir + '/lyrics.txt')} --tags {shlex.quote(remote_dir + '/tags.txt')} "
           f"--save_path {shlex.quote(remote_dir + '/output.mp3')} --max_audio_length_ms {int(max_audio_length_ms)} "
           f"--cfg_scale {float(cfg_scale)} --temperature {float(temperature)} --topk {int(topk)} --lazy_load true "
           f"> {shlex.quote(remote_dir + '/run.log')} 2>&1")
    t0 = time.time()
    r = _run(SSH + [cmd], timeout=generation_timeout)
    elapsed = round(time.time() - t0)
    if r.returncode != 0:
        tail = _run(SSH + [f"tail -c 1500 {shlex.quote(remote_dir + '/run.log')}"], timeout=30).stdout
        raise RuntimeError(f"heartmula generation failed (rc={r.returncode}, {elapsed}s): {tail[-600:]}")
    steps.append(f"generated:{elapsed}s")
    Path(save_dir).mkdir(parents=True, exist_ok=True)
    dst = Path(save_dir) / output_filename
    r = _run(SCP + [f"{HOST}:{remote_dir}/output.mp3", str(dst)], timeout=300)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"scp failed: {r.stderr.strip()[:200]}")
    steps.append(f"downloaded:{dst}")
    return {"audio_path": str(dst), "bytes": dst.stat().st_size, "elapsed_s": elapsed,
            "remote_dir": remote_dir, "steps": steps, "source": source}
