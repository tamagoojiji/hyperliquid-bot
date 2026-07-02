# 魔法の杖 — トレード手法・インジケーター データベース

Discordサーバー「魔法の杖」（課金制・server_id: `1218510304095506473`）で学習した
インジケーター／トレード手法を、チャンネル単位でまとめた知識DB。

- 収集方法: 各チャンネルの本文・画像をユーザーが提供 → Claude Codeが構造化して `channels/` に格納
- 各チャンネルの詳細は `channels/<name>.md`。図表は `channels/assets/<name>/` に保存
- ステータス: ⬜ 未取得 / 🟨 取得中 / ✅ 完了
- **進捗: 23 / 23 チャンネル 完了 ✅**（最終更新: 2026-07-02）

## チャンネル一覧（サイドバー順・全23）

| # | チャンネル | 種別（推定） | 状態 | 詳細 |
|---|-----------|------------|------|------|
| 1 | アホの坂田 | 押し目幅%＋スイング記録 | ✅ | [channels/アホの坂田.md](channels/アホの坂田.md) |
| 2 | パラボリくん | Parabolic SAR + ボリバン + 200EMA | ✅ | [channels/パラボリくん.md](channels/パラボリくん.md) |
| 3 | 金矛 | 一目均衡表(基準線×遅行線クロス) | ✅ | [channels/金矛.md](channels/金矛.md) |
| 4 | ふわふわもこもこ | マインド/資金管理メモ | ✅ | [channels/ふわふわもこもこ.md](channels/ふわふわもこもこ.md) |
| 5 | ダイバ嵐 | ダイバージェンス嵐(複数インジ数カウント) | ✅ | [channels/ダイバ嵐.md](channels/ダイバ嵐.md) |
| 6 | adx | ADX + DMI（NEOさん最重要） | ✅ | [channels/adx.md](channels/adx.md) |
| 7 | atr-資金管理 | ATR＋ポジションサイジング | ✅ | [channels/atr-資金管理.md](channels/atr-資金管理.md) |
| 8 | cvd | 累積ボリュームデルタ（現物強弱） | ✅ | [channels/cvd.md](channels/cvd.md) |
| 9 | gmma | GMMA(イワシ/クジラ)＋PO＋プルバック | ✅ | [channels/gmma.md](channels/gmma.md) |
| 10 | macd | MACD陰転/陽転カウント(3段下げ/天井) | ✅ | [channels/macd.md](channels/macd.md) |
| 11 | mm | Mayer Multiple（長期積立指標） | ✅ | [channels/mm.md](channels/mm.md) |
| 12 | ph | The Phoenix（天井/底シグナル） | ✅ | [channels/ph.md](channels/ph.md) |
| 13 | rci | RCI（順位相関・±80%） | ✅ | [channels/rci.md](channels/rci.md) |
| 14 | rsi-vwap | RSI-VWAP（緑/赤ゾーン・底値当て） | ✅ | [channels/rsi-vwap.md](channels/rsi-vwap.md) |
| 15 | rsi-ボリバン | RSI×ボリンジャー（70%基準） | ✅ | [channels/rsi-ボリバン.md](channels/rsi-ボリバン.md) |
| 16 | 30-30 | 30分足EMA30（赤黄青配列・押し目買い） | ✅ | [channels/30-30.md](channels/30-30.md) |
| 17 | rvi：天井を探す指標 | RVI（天井/底検知・RSI/RCI併用） | ✅ | [channels/rvi.md](channels/rvi.md) |
| 18 | スクイーズ | ボリンジャー幅収縮（買いシグナル） | ✅ | [channels/スクイーズ.md](channels/スクイーズ.md) |
| 19 | 分析サイト等 | Coinglass(OI/FR/清算/オプション) | ✅ | [channels/分析サイト等.md](channels/分析サイト等.md) |
| 20 | 平均足 | 月足平均足（希少な買いシグナル） | ✅ | [channels/平均足.md](channels/平均足.md) |
| 21 | 窓埋めゲーム | CMEギャップ埋め（基準価格逆張り） | ✅ | [channels/窓埋めゲーム.md](channels/窓埋めゲーム.md) |
| 22 | 超大相場買いシグナル | 各インジの重なりチェックリスト(31項目) | ✅ | [channels/超大相場買いシグナル.md](channels/超大相場買いシグナル.md) |
| 23 | antiセットアップ：macd | リンダAntiセットアップ(MACD GC→DC→GC) | ✅ | [channels/antiセットアップ-macd.md](channels/antiセットアップ-macd.md) |

## チャンネルID（ユーザー提供・名前対応は本文取得時に確定）

以下23個のIDが提供済み。サイドバー順とID作成順が一致しないため、
どのIDがどのチャンネルかは各チャンネルの本文取得時に確定する。

```
1265321284347236392
1265319800515596322
1265319879180026010
1265320143903391867
1277862345670131735
1265320100458922146
1267135993308512438
1265320986387943588
1265319968472436818
1277861947638812775
1265321011973324831
1280525244502900860
1298288441347543120
1290263398831099914
1298288900032167997
1280508318586241105
1277174700681859073
1298288128909377647
1278750495875535000
1303253144498606100
1305158316875452498
1306264655273918504
1337081346874540063
```

## 注記
- Bot（SnsTensakuClaude）はこのサーバー未参加のためAPI/Bot経由では読めない（403）。
- したがって本文はユーザー提供の画像・テキストから収集する。
