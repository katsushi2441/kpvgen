#!/usr/bin/env python3
"""ACE-Step 1.5 Turbo XL と HeartMuLa oss-3B を同じ歌詞・同じ尺で順に回し、所要時間を results.json に書く。
数字はこのスクリプトが測る（記事は読むだけ）。0.14 側の VRAM/RAM は ~/bench/sample_*.csv のサンプラーが2秒ごとに記録。"""
import json, time, sys
from redis import Redis
from rq import Queue
from rq.job import Job
c = Redis.from_url("redis://127.0.0.1:6379/0")
lyr_ace = open("outputs/acestep_queue/test_lyrics_ja.txt", encoding="utf-8").read().strip()
lyr_hm = open("outputs/heartmula_queue/test_lyrics_ja.txt", encoding="utf-8").read().strip()
tags_hm = open("outputs/heartmula_queue/test_tags.txt", encoding="utf-8").read().strip()
prompt_ace = "Japanese lo-fi pop, soft female vocal, warm Rhodes piano, gentle drums, vinyl texture, 80 BPM, calm night study mood"
runs = [
    ("acestep", 60), ("heartmula", 60), ("acestep", 180), ("heartmula", 180),
]
results = []
for engine, secs in runs:
    out = f"bench-{engine}-{secs}s.mp3"
    if engine == "acestep":
        job = Queue("acestep-192-168-0-14", connection=c).enqueue(
            "rqdb4ai_acestep_job.acestep_generate_job", prompt=prompt_ace, lyrics=lyr_ace, output_filename=out,
            audio_duration=secs, vocal_language="ja", extra={"thinking": True, "shift": 3.0, "inference_steps": 8},
            save_dir="/home/kojima/work/kpvgen/outputs/music_bench", job_timeout=2400, result_ttl=86400, failure_ttl=86400)
    else:
        job = Queue("heartmula-192-168-0-14", connection=c).enqueue(
            "rqdb4ai_heartmula_job.heartmula_generate_job", lyrics=lyr_hm, tags=tags_hm, output_filename=out,
            max_audio_length_ms=secs * 1000, save_dir="/home/kojima/work/kpvgen/outputs/music_bench",
            job_timeout=2400, result_ttl=86400, failure_ttl=86400)
    t0 = time.time(); start = time.strftime("%H:%M:%S")
    print(f"[{start}] enqueued {engine} {secs}s {job.id}", flush=True)
    while True:
        time.sleep(3)
        j = Job.fetch(job.id, connection=c); st = j.get_status()
        if st in ("finished", "failed", "stopped", "canceled"):
            break
        if time.time() - t0 > 2400:
            st = "timeout"; break
    wall = round(time.time() - t0)
    end = time.strftime("%H:%M:%S")
    r = {"engine": engine, "seconds": secs, "status": str(st), "wall_s": wall, "start": start, "end": end, "file": out}
    if st == "finished":
        res = j.result or {}
        r["steps"] = res.get("steps"); r["bytes"] = res.get("bytes"); r["elapsed_gen_s"] = res.get("elapsed_s")
    else:
        r["error"] = (j.exc_info or "")[-300:]
    print(f"[{end}] {engine} {secs}s -> {st} wall {wall}s", flush=True)
    results.append(r)
    json.dump(results, open("outputs/music_bench/results.json", "w"), ensure_ascii=False, indent=1)
print("BENCH_DONE" if all(x["status"] == "finished" for x in results) else "BENCH_FAIL", flush=True)
