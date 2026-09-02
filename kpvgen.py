#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kpvgen — 実画面キャプチャ＋実写クリップ＋モーションを HyperFrames で組む製品PV生成。

  python3 kpvgen.py build specs/kshoken.json
  python3 kpvgen.py build specs/kshoken.json --skip-capture   # 撮影済みの画像を使い回す

仕組み（kurage本体の動画生成と同じレンダリング経路）:
  1. capture  : headless Chrome で製品の実画面を撮る（URLで状態を再現）
  2. narration: Audio8 TTS。数字の読みは spec の reading でカナ確定（読み間違い防止）
  3. compose  : HTML+GSAP の HyperFrames プロジェクトを生成
  4. render   : npx hyperframes render（Node 22）
  5. verify   : ffprobe で尺・解像度、Whisper聴き取りで「言うべき語」を機械検証

specの形は specs/*.json を参照。守っていること:
  - 数字は spec に書いた実測値だけを表示・読み上げる（生成AIに数字を作らせない）
  - 地図など読み込みの遅い画面は白率で検証してから使う
  - 検証に落ちたら完成と言わない（出力を消して失敗させる）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HYPERFRAMES_VERSION = os.environ.get("KPVGEN_HYPERFRAMES_VERSION", "0.4.44")
NVM_SH = os.path.expanduser("~/.nvm/nvm.sh")
CHROME = os.environ.get("KPVGEN_CHROME", "google-chrome")
TTS_API = os.environ.get("KPVGEN_TTS_API", "http://127.0.0.1:18350/tts")
WHISPER_BIN = os.environ.get("KPVGEN_WHISPER_BIN",
                             "/home/kojima/work/kaimom/vendor/whisper.cpp/build/bin/whisper-cli")
WHISPER_MODEL = os.environ.get("KPVGEN_WHISPER_MODEL",
                               "/mnt/data/kaimom/models/ggml-large-v3-turbo.bin")


def run(cmd, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run(cmd, **kw)


def die(msg: str) -> None:
    print(f"NG: {msg}", file=sys.stderr)
    sys.exit(1)


def media_duration(path: Path) -> float:
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)])
    return float((r.stdout or "0").strip() or 0)


# ── 1. capture ─────────────────────────────────────────────

def white_ratio(png: Path) -> float:
    from PIL import Image
    im = Image.open(png).convert("RGB").resize((120, 80))
    px = list(im.getdata())
    return sum(1 for c in px if c[0] > 245 and c[1] > 245 and c[2] > 245) / len(px)


def capture_scene(sc: dict, out: Path, size: str) -> None:
    budget = int(sc.get("load_seconds", 30)) * 1000
    for attempt in (1, 2):
        r = run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--hide-scrollbars", f"--window-size={size.replace('x', ',')}",
                 f"--virtual-time-budget={budget}",
                 f"--screenshot={out}", sc["url"]], timeout=180)
        if out.exists() and out.stat().st_size > 10000:
            ratio = white_ratio(out)
            limit = float(sc.get("max_white_ratio", 0.90))
            if ratio <= limit:
                print(f"  capture OK {out.name} (白率{ratio:.0%})")
                return
            print(f"  白率{ratio:.0%} > {limit:.0%} — 読み込み待ちを倍にして再試行")
            budget *= 2
    die(f"画面が読み込み切れていない: {sc['url']}")


# ── 2. narration ───────────────────────────────────────────

def build_narration(spec: dict, work: Path) -> tuple[Path | None, float]:
    narr = spec.get("narration")
    if not narr:
        return None, 0.0
    if narr.get("file"):
        src = Path(narr["file"])
    else:
        import urllib.request
        req = urllib.request.Request(TTS_API, method="POST",
                                     headers={"Content-Type": "application/json"},
                                     data=json.dumps({"text": narr["reading"]}).encode())
        wav = work / "narration.wav"
        with urllib.request.urlopen(req, timeout=300) as r:
            wav.write_bytes(r.read())
        src = wav
    mp3 = work / "narration.mp3"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-b:a", "192k", str(mp3)])
    dur = media_duration(mp3)
    print(f"  narration {dur:.1f}秒")
    return mp3, dur


def narration_keyword_time(audio: Path, keyword: str) -> float | None:
    """Whisperのタイムスタンプから、keywordが発話される時刻(秒)を実測する。

    セグメント内の文字位置で線形補間する（±0.5秒程度の精度で、シーン同期には十分）。
    """
    if not Path(WHISPER_BIN).exists():
        return None
    t = run([WHISPER_BIN, "-m", WHISPER_MODEL, "-l", "ja", "-f", str(audio), "-np"],
            timeout=600).stdout
    pat = re.compile(r"\[(\d+):(\d+):(\d+\.\d+) --> (\d+):(\d+):(\d+\.\d+)\]\s*(.*)")
    for line in t.splitlines():
        m = pat.match(line.strip())
        if not m:
            continue
        st = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        en = int(m.group(4)) * 3600 + int(m.group(5)) * 60 + float(m.group(6))
        text = m.group(7).strip()
        if keyword in text:
            pos = text.index(keyword) / max(len(text), 1)
            return st + (en - st) * pos
    return None


# ── 3. compose（HyperFramesプロジェクト生成） ─────────────

TELOP_CSS = """
.telop { position:absolute; left:56px; bottom:64px; z-index:30; opacity:0; transform:translateY(26px);
  background:rgba(255,254,251,.96); border:4px solid #191f27; padding:18px 26px 16px 34px; }
.telop::before { content:""; position:absolute; left:0; top:0; bottom:0; width:9px; background:#e08a00; }
.telop b { display:block; font-size:40px; font-weight:900; color:#151a21; line-height:1.4; }
.telop span { display:block; font-size:21px; color:#5b6572; margin-top:2px; }
"""

STATS_CSS = """
.stats { position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
  background:#fffefb; }
.stats-grid { display:grid; grid-template-columns:repeat(2, 400px); gap:26px; }
.stat { border:4px solid #191f27; background:#fff; padding:26px 30px; }
.stat b { display:block; font-family:'JetBrains Mono',monospace; font-size:64px; font-weight:700;
  color:#151a21; letter-spacing:-0.03em; font-variant-numeric:tabular-nums; }
.stat span { display:block; font-size:22px; color:#5b6572; margin-top:6px; }
.stats-head { position:absolute; top:70px; left:0; right:0; text-align:center;
  font-size:40px; font-weight:900; color:#151a21; }
"""

ENDCARD_CSS = """
.endcard { position:absolute; inset:0; background:#fffefb; display:flex; flex-direction:column;
  align-items:center; justify-content:center; text-align:center; }
.endcard::before, .endcard::after { content:""; position:absolute; left:0; right:0; height:12px; background:#191f27; }
.endcard::before { top:0; } .endcard::after { bottom:0; }
.endcard h1 { font-size:78px; font-weight:900; color:#151a21; }
.endcard .sub { font-size:30px; color:#5b6572; margin-top:10px; }
.endcard .price { font-size:40px; font-weight:900; color:#0a726b; margin-top:26px; }
.endcard .url { font-family:'JetBrains Mono',monospace; font-size:26px; color:#151a21; margin-top:18px; }
.endcard .url2 { font-family:'JetBrains Mono',monospace; font-size:26px; color:#1668d6; margin-top:6px; }
.endcard .credit { font-size:19px; color:#9aa7b4; margin-top:14px; }
.endcard img { width:150px; margin-top:30px; }
"""


def esc(t: str) -> str:
    import html as _h
    return _h.escape(str(t or ""))


def telop_html(t: dict | None) -> str:
    if not t:
        return ""
    sub = f"<span>{esc(t.get('sub'))}</span>" if t.get("sub") else ""
    return f'<div class="telop"><b>{esc(t.get("main"))}</b>{sub}</div>'


def compose(spec: dict, work: Path, narration: Path | None, narr_delay: float) -> Path:
    W, H = spec.get("width", 1920), spec.get("height", 1080)
    scenes = spec["scenes"]
    total = sum(float(s["duration"]) for s in scenes)
    proj = work / "hf"
    if proj.exists():
        shutil.rmtree(proj)
    (proj / "assets").mkdir(parents=True)

    body, gsap = [], []
    t0 = 0.0
    for i, sc in enumerate(scenes):
        dur = float(sc["duration"])
        kind = sc["type"]
        inner = ""
        if kind in ("capture", "image"):
            img = f"assets/s{i}.png"
            shutil.copy(sc["_png"], proj / img)
            z1, z2 = sc.get("zoom_from", 1.0), sc.get("zoom_to", 1.12)
            ox, oy = sc.get("origin", ["50%", "40%"])
            inner = (f'<img class="bg" id="bg{i}" src="{img}" '
                     f'style="transform-origin:{ox} {oy};">')
            gsap.append(f"tl.fromTo('#bg{i}',{{scale:{z1}}},"
                        f"{{scale:{z2},duration:{dur},ease:'none'}},{t0:.2f});")
        elif kind == "clip":
            src = Path(sc["file"])
            dst = f"assets/clip{i}.mp4"
            # 音声トラックは剥がして映像のみ置く。hyperframesはmuted属性を無視して
            # <video>の音声もフル音量で混ぜるため(bg+fgで二重・2026-09-02実測)、
            # クリップ音声はclip{i}.mp3(音量焼き込み済み)だけに一本化する。
            run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-c:v", "copy", "-an",
                 str(proj / dst)])
            fit = sc.get("fit", "blur")
            if fit == "blur":   # 縦素材を横に敷く: ぼかし背景+中央
                inner = (f'<video class="clipbg" src="{dst}" data-start="{t0:.2f}" '
                         f'data-duration="{dur:.2f}" muted playsinline preload="auto"></video>'
                         f'<video class="clipfg" src="{dst}" data-start="{t0:.2f}" '
                         f'data-duration="{dur:.2f}" muted playsinline preload="auto"></video>')
            else:
                inner = (f'<video class="bg" src="{dst}" data-start="{t0:.2f}" '
                         f'data-duration="{dur:.2f}" muted playsinline preload="auto"></video>')
            if sc.get("audio_volume"):
                amp3 = f"assets/clip{i}.mp3"
                # hyperframesはdata-volumeを適用しない(0.4.44実測)。音量はmp3に焼き込む。
                run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn",
                     "-filter:a", f"volume={sc['audio_volume']}",
                     "-b:a", "160k", str(proj / amp3)])
                body.append(f'<audio src="{amp3}" data-start="{t0:.2f}" '
                            f'data-duration="{dur:.2f}" data-track-index="3" '
                            f'data-volume="1"></audio>')
        elif kind == "stats":
            cards = []
            for j, st in enumerate(sc["items"]):
                cards.append(f'<div class="stat"><b id="num{i}_{j}" '
                             f'data-target="{st["value"]}">0</b><span>{esc(st["label"])}</span></div>')
                gsap.append(
                    f"const o{i}_{j}={{v:0}};const el{i}_{j}=document.getElementById('num{i}_{j}');"
                    f"tl.to(o{i}_{j},{{v:{st['value']},duration:{min(dur-0.6,1.6):.2f},"
                    f"ease:'power2.out',onUpdate:()=>{{el{i}_{j}.textContent=Math.round(o{i}_{j}.v).toLocaleString('ja-JP')}}}},{t0+0.35:.2f});")
            head = f'<div class="stats-head">{esc(sc.get("title"))}</div>' if sc.get("title") else ""
            inner = f'<div class="stats">{head}<div class="stats-grid">{"".join(cards)}</div></div>'
        elif kind == "endcard":
            gsap.append(f"tl.fromTo('#scene{i} .price',{{scale:0.6,opacity:0}},"
                        f"{{scale:1,opacity:1,duration:0.45,ease:'back.out(2)'}},{t0+0.15:.2f});")
            e = sc
            img = ""
            if e.get("image"):
                dst = f"assets/end{i}.webp"
                shutil.copy(e["image"], proj / dst)
                img = f'<img src="{dst}">'
            inner = (f'<div class="endcard"><h1>{esc(e.get("title"))}</h1>'
                     f'<div class="sub">{esc(e.get("sub"))}</div>'
                     f'<div class="price">{esc(e.get("price"))}</div>'
                     f'<div class="url">{esc(e.get("url1"))}</div>'
                     f'<div class="url2">{esc(e.get("url2"))}</div>'
                     + (f'<div class="credit">{esc(e.get("credit"))}</div>' if e.get("credit") else "")
                     + f'{img}</div>')
        else:
            die(f"未知のscene type: {kind}")

        fade = ('opacity:0;" data-anim="fadein' if i else '"')
        body.append(f'<section class="scene" id="scene{i}" data-start="{t0:.2f}" '
                    f'data-duration="{dur:.2f}" style="z-index:{10+i};{fade}">'
                    f'{inner}{telop_html(sc.get("telop"))}</section>')
        if i:
            gsap.append(f"tl.to('#scene{i}',{{opacity:1,duration:0.35}},{t0:.2f});")
        tel = sc.get("telop")
        if tel:
            # テロップはCSSで初期非表示。タイムラインで所定時刻に出す
            gsap.append(f"tl.to('#scene{i} .telop',{{y:0,opacity:1,duration:0.5,"
                        f"ease:'power2.out'}},{t0 + float(tel.get('at', 0.4)):.2f});")
        t0 += dur

    audio_tag = ""
    if narration:
        shutil.copy(narration, proj / "assets/narration.mp3")
        audio_tag = (f'<audio src="assets/narration.mp3" data-start="{narr_delay:.2f}" '
                     f'data-duration="{total - narr_delay:.2f}" data-track-index="5" '
                     f'data-volume="1"></audio>')

    html_doc = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8"><title>{esc(spec.get('name'))}</title>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
html,body {{ width:{W}px; height:{H}px; overflow:hidden; background:#fffefb;
  font-family:'Noto Sans CJK JP','Noto Sans JP',sans-serif; }}
#composition {{ position:relative; width:{W}px; height:{H}px; overflow:hidden; background:#fffefb; }}
.scene {{ position:absolute; inset:0; overflow:hidden; }}
.bg {{ width:100%; height:100%; object-fit:cover; display:block; }}
.clipbg {{ position:absolute; inset:0; width:100%; height:100%; object-fit:cover;
  filter:blur(26px) brightness(0.92); transform:scale(1.12); }}
.clipfg {{ position:absolute; top:0; bottom:0; left:50%; transform:translateX(-50%);
  height:100%; object-fit:contain; }}
{TELOP_CSS}{STATS_CSS}{ENDCARD_CSS}
</style></head>
<body>
<div id="composition" data-composition-id="main" data-start="0"
     data-duration="{total:.2f}" data-width="{W}" data-height="{H}">
{chr(10).join(body)}
{audio_tag}
</div>
<script>
// hyperframes は window.__timelines["main"] に登録した paused タイムラインを
// フレームごとに seek して描画する。自走の gsap.to(delay) は実行されない（実測）。
const tl = gsap.timeline({{ paused: true }});
{chr(10).join(gsap)}
window.__timelines = window.__timelines || {{}};
window.__timelines["main"] = tl;
</script>
</body></html>"""
    (proj / "index.html").write_text(html_doc, encoding="utf-8")
    (proj / "hyperframes.json").write_text(json.dumps({
        "$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
        "paths": {"blocks": "compositions", "components": "compositions/components",
                  "assets": "assets"}}, indent=2), encoding="utf-8")
    (proj / "package.json").write_text(json.dumps({
        "name": f"kpvgen-{spec.get('id', 'pv')}", "private": True, "type": "module",
        "scripts": {"render": f"npx --yes hyperframes@{HYPERFRAMES_VERSION} render"}},
        indent=2), encoding="utf-8")
    (proj / "meta.json").write_text(json.dumps(
        {"id": spec.get("id", "pv"), "name": spec.get("name", "PV")}, indent=2),
        encoding="utf-8")
    print(f"  compose OK 総尺{total:.1f}秒 / {len(scenes)}シーン")
    return proj


# ── 4. render ──────────────────────────────────────────────

def render(proj: Path, out: Path) -> None:
    cmd = (f'source "{NVM_SH}" 2>/dev/null; nvm use 22 >/dev/null 2>&1; '
           f'cd "{proj}" && npx --yes hyperframes@{HYPERFRAMES_VERSION} render --output "{out}"')
    r = run(["bash", "-c", cmd], timeout=900)
    if r.returncode != 0 or not out.exists():
        die(f"render失敗 rc={r.returncode}\n{(r.stdout or '')[-800:]}\n{(r.stderr or '')[-800:]}")
    print(f"  render OK {out} ({out.stat().st_size // 1024}KB)")


# ── 5. verify ──────────────────────────────────────────────

def verify(spec: dict, out: Path) -> None:
    total = sum(float(s["duration"]) for s in spec["scenes"])
    dur = media_duration(out)
    if abs(dur - total) > 1.5:
        die(f"尺が合わない: {dur:.1f}秒 (期待{total:.1f})")
    r = run(["ffprobe", "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)])
    wh = (r.stdout or "").strip().split("\n")[0]
    print(f"  verify 尺{dur:.1f}秒 / {wh}")
    must = (spec.get("narration") or {}).get("must_hear") or []
    if must and Path(WHISPER_BIN).exists():
        wav = out.with_suffix(".check.wav")
        run(["ffmpeg", "-y", "-v", "error", "-i", str(out), "-vn", "-ac", "1",
             "-ar", "16000", str(wav)])
        t = run([WHISPER_BIN, "-m", WHISPER_MODEL, "-l", "ja", "-f", str(wav),
                 "-np", "-nt"], timeout=600).stdout
        # 数字の表記ゆれを吸収する（「5万5000円」をWhisperは「55,000円」と書く）。
        # 万/千を展開した数字だけの形に潰してから比較する。
        def numfold(x: str) -> str:
            x = re.sub(r"[,，\s]", "", x)
            # 千は万より先に展開する。「5万5千」を万が先に食うと 5万5→55000 の後に
            # 千が残って 55000千=5500万 に化ける(2026-09-02実測)。
            x = re.sub(r"(\d+)千", lambda m: str(int(m.group(1)) * 1000), x)
            x = re.sub(r"(\d+)万(\d+)", lambda m: str(int(m.group(1)) * 10000 + int(m.group(2)) * (1000 if len(m.group(2)) == 1 else 1)), x)
            x = re.sub(r"(\d+)万", lambda m: str(int(m.group(1)) * 10000), x)
            return x
        norm = numfold(t)
        missing = [m for m in must if numfold(m) not in norm]
        wav.unlink(missing_ok=True)
        if missing:
            out.unlink(missing_ok=True)
            die(f"聴き取り検証に失敗（言えていない語）: {missing}\n聴き取り: {t.strip()[:300]}")
        print(f"  verify 聴き取りOK: {must}")


# ── main ───────────────────────────────────────────────────

def build(spec_path: Path, skip_capture: bool) -> None:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    work = ROOT / "outputs" / spec.get("id", spec_path.stem)
    work.mkdir(parents=True, exist_ok=True)
    size = f"{spec.get('width', 1920)}x{spec.get('height', 1080)}"

    for i, sc in enumerate(spec["scenes"]):
        if sc["type"] == "capture":
            png = work / f"cap{i}.png"
            sc["_png"] = png
            if skip_capture and png.exists():
                print(f"  capture reuse {png.name}")
            else:
                capture_scene(sc, png, size)
        elif sc["type"] == "image":
            sc["_png"] = Path(sc["file"])

    narration, _ = build_narration(spec, work)
    delay = float((spec.get("narration") or {}).get("delay", 0))

    # sync_to: 指定キーワードの発話時刻に、そのシーンの開始を合わせる。
    # 前のシーン群の尺を比例で縮め、同期シーンが残り全部を受け持つ（総尺は不変）。
    scenes = spec["scenes"]
    for idx, sc in enumerate(scenes):
        kw = sc.get("sync_to")
        if not kw or not narration:
            continue
        t_kw = narration_keyword_time(narration, kw)
        if t_kw is None:
            print(f"  sync_to: {kw!r} が聴き取れず、同期をスキップ")
            break
        target = delay + t_kw - float(sc.get("sync_lead", 0.2))
        before = sum(float(x["duration"]) for x in scenes[:idx])
        total = sum(float(x["duration"]) for x in scenes)
        if not (3.0 < target < total - 1.0):
            print(f"  sync_to: 目標{target:.1f}秒が範囲外のためスキップ")
            break
        factor = target / before
        if factor > 1.0:
            # 引き伸ばしはクリップ(実写素材)に掛けない。素材尺を超えると白落ちする
            # (2026-09-02実測)。クリップ以外のシーンだけで伸び分を吸収する。
            fixed = sum(float(x["duration"]) for x in scenes[:idx] if x.get("type") == "clip")
            flex = before - fixed
            if flex <= 0:
                print("  sync_to: 可変シーンが無く引き伸ばし不可、同期をスキップ")
                break
            factor = (target - fixed) / flex
            for x in scenes[:idx]:
                if x.get("type") != "clip":
                    x["duration"] = round(float(x["duration"]) * factor, 2)
        else:
            for x in scenes[:idx]:
                x["duration"] = round(float(x["duration"]) * factor, 2)
        scenes[idx]["duration"] = round(total - sum(float(x["duration"]) for x in scenes[:idx]), 2)
        print(f"  sync_to: 「{kw}」発話={t_kw:.1f}秒(音声内) → シーン{idx}を{target:.1f}秒開始に調整"
              f" (前段を×{factor:.3f})")
        break

    proj = compose(spec, work, narration, delay)
    out = work / f"{spec.get('id', 'pv')}.mp4"
    render(proj, out)
    verify(spec, out)
    print(f"完成: {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("spec")
    b.add_argument("--skip-capture", action="store_true")
    args = ap.parse_args()
    if args.cmd == "build":
        build(Path(args.spec), args.skip_capture)


if __name__ == "__main__":
    main()
