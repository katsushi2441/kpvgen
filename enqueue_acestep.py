#!/usr/bin/env python3
"""ACE-Step 1.5 Turbo XL の曲生成を rqdb4ai の acestep-192-168-0-14 キューに投入する。
  /usr/bin/python3 enqueue_acestep.py --prompt "lo-fi hip hop, instrumental" --lyrics-file lyrics.txt \
      --duration 60 --out song.mp3 [--language ja] [--wait]
完成物は kpvgen/outputs/acestep_queue/<out>。歌詞ファイルが無ければインスト([instrumental])。
0.14 の GPU は H3・Ollama と共有なので、直接 API を叩かずこのキューに載せる(直列化)。
"""
import argparse, sys, time
from redis import Redis
from rq import Queue
from rq.job import Job

ap = argparse.ArgumentParser()
ap.add_argument("--prompt", required=True)
ap.add_argument("--lyrics-file", default="")
ap.add_argument("--duration", type=float, default=60)
ap.add_argument("--out", required=True, help="出力ファイル名(mp3)")
ap.add_argument("--language", default="ja")
ap.add_argument("--wait", action="store_true")
a = ap.parse_args()
lyrics = open(a.lyrics_file, encoding="utf-8").read().strip() if a.lyrics_file else "[instrumental]"
c = Redis.from_url("redis://127.0.0.1:6379/0")
job = Queue("acestep-192-168-0-14", connection=c).enqueue(
    "rqdb4ai_acestep_job.acestep_generate_job",
    prompt=a.prompt, lyrics=lyrics, output_filename=a.out, audio_duration=a.duration,
    vocal_language=a.language, job_timeout=1800, result_ttl=86400, failure_ttl=86400)
print("enqueued", job.id)
if a.wait:
    t0 = time.time()
    while True:
        time.sleep(5)
        j = Job.fetch(job.id, connection=c); st = j.get_status()
        if st in ("finished", "failed", "stopped", "canceled"):
            print(st, "%.0fs" % (time.time() - t0))
            print(j.result if st == "finished" else (j.exc_info or "")[-600:])
            sys.exit(0 if st == "finished" else 1)
        if time.time() - t0 > 1800:
            print("timeout"); sys.exit(2)
