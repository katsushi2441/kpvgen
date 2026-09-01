# kpvgen — 製品PV生成（実画面キャプチャ＋実写クリップ＋モーション）

spec(JSON)1枚から、検証済みの製品PVを作る。レンダリングは kurage 本体と同じ
HyperFrames（HTML+GSAP）。

```bash
python3 kpvgen.py build specs/kshoken.json [--skip-capture]
```

工程: capture(実画面・白率検証つき) → narration(Audio8・読みはカナ確定) →
compose(HyperFramesプロジェクト生成) → render(npx hyperframes・Node22) →
verify(尺/解像度 + Whisper聴き取りで「言うべき語」を確認。落ちたら出力を消す)

シーン型: `clip`(実写mp4・縦はぼかし敷き) / `capture`(URL→ズーム) /
`stats`(数字カウントアップ) / `endcard`

## 踏んだ罠（重要）

- **HyperFramesは `window.__timelines["main"]` に登録した paused の gsapタイムラインを
  フレームごとに seek して描画する。** 自走の `gsap.to(..., delay)` は一切実行されない
  （シーンが真っ白のまま書き出された）。アニメは必ず `tl.to(..., 位置)` で書く。
- Whisper検証の期待語は数字の表記ゆれを吸収する（「5万5000円」⇔「55,000円」）。
  ナレーションが正しいのに不合格にした前例。
- 地図つき画面は読み込みが遅い。白率で検証し、失敗したら待ち時間を倍にして再試行。
