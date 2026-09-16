#!/usr/bin/env python3
"""mp3 を whisper.cpp(ja) で聴き取り、元の歌詞との文字誤り率(CER)と、行ごとの一致を数える。
  /usr/bin/python3 lyric_cer.py <mp3> <lyrics.txt>
見出し([Verse] 等)と空白は除いて比較。数字はここで数える。"""
import re, subprocess, sys, tempfile, json
WHISPER = "/home/kojima/work/kaimom/vendor/whisper.cpp/build/bin/whisper-cli"
MODEL = "/mnt/data/kaimom/models/ggml-large-v3-turbo.bin"
def norm(s):
    s = re.sub(r"\[[^\]]*\]", "", s)
    return re.sub(r"[\s、。,.!?！？「」…〜~\-]", "", s)
def lev(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]
mp3, lyr = sys.argv[1], sys.argv[2]
wav = tempfile.mktemp(suffix=".wav", dir="/tmp/claude-1000")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", mp3, "-ar", "16000", "-ac", "1", wav], check=True)
out = subprocess.run([WHISPER, "-m", MODEL, "-l", "ja", "-f", wav, "-np"], capture_output=True, text=True).stdout
heard = "".join(re.sub(r"^\[[^\]]*\]\s*", "", l) for l in out.splitlines())
ref = open(lyr, encoding="utf-8").read()
a, b = norm(ref), norm(heard)
d = lev(a, b)
lines = [norm(l) for l in ref.splitlines() if norm(l)]
hit = sum(1 for l in lines if l in b)
print(json.dumps({"file": mp3.split("/")[-1], "ref_chars": len(a), "heard_chars": len(b), "cer": round(d / max(1, len(a)), 3),
                  "lines_exact": f"{hit}/{len(lines)}", "heard": heard[:400]}, ensure_ascii=False))
