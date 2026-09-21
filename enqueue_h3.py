#!/usr/bin/env python3
"""MiniMax H3 の8秒実写クリップを rqdb4ai の h3-192-168-0-14 キューに投入する。

  /usr/bin/python3 enqueue_h3.py --prompt "..." --out h3-kghome-8s.mp4 [--seed 20260922] [--wait]

完成 mp4 は kpvgen/outputs/h3_queue/<out>。所要は約28分（1344×768×192f）。
**ComfyUI :8001 を直接叩かない。** 0.14 の GPU は Ollama・ACE-Step と共有で、
rqdb4ai の単一ワーカーが直列化している。
"""
import argparse
import json
import sys
import time
from pathlib import Path

from redis import Redis
from rq import Queue
from rq.job import Job

ROOT = Path(__file__).resolve().parent

ap = argparse.ArgumentParser()
ap.add_argument("--prompt", required=True)
ap.add_argument("--out", required=True, help="出力ファイル名（mp4）")
ap.add_argument("--seed", type=int, default=int(time.strftime("%Y%m%d")))
ap.add_argument("--wait", action="store_true")
a = ap.parse_args()

wf = json.loads((ROOT / "h3_workflow_template.json").read_text(encoding="utf-8"))
hit = {"prompt": 0, "seed": 0, "save": 0}
for node in wf.values():
    ct = node.get("class_type", "")
    ins = node.get("inputs", {})
    if ct == "MiniMaxH3ImageToVideo" and "prompt" in ins:
        ins["prompt"] = a.prompt; hit["prompt"] += 1
    elif ct == "RandomNoise":
        ins["noise_seed"] = a.seed; hit["seed"] += 1
    elif ct == "SaveVideo":
        ins["filename_prefix"] = Path(a.out).stem; hit["save"] += 1
# テンプレの形が変わると無言で前回のプロンプトのまま流れるので、差し替え漏れを止める
if not all(hit.values()):
    print(f"! テンプレートの差し替えに失敗: {hit}", file=sys.stderr)
    sys.exit(1)

c = Redis.from_url("redis://127.0.0.1:6379/0")
job = Queue("h3-192-168-0-14", connection=c).enqueue(
    "rqdb4ai_h3_job.h3_generate_job",
    workflow=wf, output_filename=a.out,
    job_timeout=5400, result_ttl=86400, failure_ttl=86400)
print("enqueued", job.id, "→ outputs/h3_queue/" + a.out)
if a.wait:
    t0 = time.time()
    while True:
        time.sleep(20)
        j = Job.fetch(job.id, connection=c)
        st = j.get_status()
        if st in ("finished", "failed", "stopped", "canceled"):
            print(st, "%.0fs" % (time.time() - t0))
            print(j.result if st == "finished" else (j.exc_info or "")[-800:])
            sys.exit(0 if st == "finished" else 1)
        if time.time() - t0 > 5400:
            print("timeout"); sys.exit(2)
