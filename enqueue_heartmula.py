import os
#!/usr/bin/env python3
"""HeartMuLa oss-3B の曲生成を rqdb4ai の heartmula-192-168-0-14 キューに投入する。
  /usr/bin/python3 enqueue_heartmula.py --lyrics-file lyrics.txt --tags-file tags.txt --seconds 60 --out song.mp3 [--wait]
歌詞は [Intro]/[Verse]/[Chorus]/[Bridge]/[Outro] 付き、タグは半角カンマ区切り(空白なし)。
完成物は kpvgen/outputs/heartmula_queue/<out>。0.14 の GPU は H3・Ollama・ACE-Step と共有なのでキュー経由のみ。
"""
import argparse, sys, time
from redis import Redis
from rq import Queue
from rq.job import Job

ap = argparse.ArgumentParser()
ap.add_argument("--lyrics-file", required=True)
ap.add_argument("--tags-file", required=True)
ap.add_argument("--seconds", type=int, default=60)
ap.add_argument("--out", required=True)
ap.add_argument("--cfg", type=float, default=1.5)
ap.add_argument("--wait", action="store_true")
a = ap.parse_args()
a.out = os.path.abspath(a.out)  # 相対パスだと worker 側で outputs/ の下に二重に付いて scp が落ちる
c = Redis.from_url("redis://127.0.0.1:6379/0")
job = Queue("heartmula-192-168-0-14", connection=c).enqueue(
    "rqdb4ai_heartmula_job.heartmula_generate_job",
    lyrics=open(a.lyrics_file, encoding="utf-8").read(), tags=open(a.tags_file, encoding="utf-8").read().strip(),
    output_filename=a.out, max_audio_length_ms=a.seconds * 1000, cfg_scale=a.cfg,
    job_timeout=2400, result_ttl=86400, failure_ttl=86400)
print("enqueued", job.id)
if a.wait:
    t0 = time.time()
    while True:
        time.sleep(5)
        j = Job.fetch(job.id, connection=c); st = j.get_status()
        if st in ("finished", "failed", "stopped", "canceled"):
            print(st, "%.0fs" % (time.time() - t0)); print(j.result if st == "finished" else (j.exc_info or "")[-800:])
            sys.exit(0 if st == "finished" else 1)
        if time.time() - t0 > 2400:
            print("timeout"); sys.exit(2)
