#!/usr/bin/env python3
"""results.json の各ジョブ時間帯(start〜end)に対応する 0.14 サンプラー CSV(時刻,VRAM MiB,GPU util %,RAM MB)から
VRAM ピーク・GPU 使用率平均・RAM ピークを出す。  /usr/bin/python3 peaks.py results.json sample.csv"""
import json, sys
res = json.load(open(sys.argv[1]))
rows = []
for l in open(sys.argv[2]):
    p = l.strip().split(",")
    if len(p) == 4 and p[0].count(":") == 2:
        rows.append((p[0], int(p[1]), int(p[2]), int(p[3])))
base_vram = min(r[1] for r in rows); base_ram = min(r[3] for r in rows)
out = []
for r in res:
    w = [x for x in rows if r["start"] <= x[0] <= r["end"]]
    if not w: continue
    out.append({"engine": r["engine"], "seconds": r["seconds"], "wall_s": r["wall_s"],
                "vram_peak_mib": max(x[1] for x in w), "vram_peak_delta_gb": round((max(x[1] for x in w) - base_vram) / 1024, 1),
                "gpu_util_avg": round(sum(x[2] for x in w) / len(w)), "ram_peak_mb": max(x[3] for x in w),
                "ram_peak_delta_gb": round((max(x[3] for x in w) - base_ram) / 1024, 1), "samples": len(w)})
print(json.dumps(out, ensure_ascii=False, indent=1))
