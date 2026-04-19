# DESIGN.md — Hyperliquid 自動売買Bot

## 1. 概要
- **一言で**: Hyperliquidで複数戦略を切り替えて自動売買するbot
- **ターゲット**: 自分（個人運用）
- **取引所**: Hyperliquid（分散型perpDEX、USDC証拠金）
- **実行環境**: VPS（Docker）— Contabo既存VPSに相乗り
- **コスト**: VPS追加費用なし。運用資金$200（1ウォレット、メイン+サブアカウント各$100）

## 2. 取引ペア・証拠金の整理

Hyperliquid perpは**USDC証拠金**。内部シンボルは`BTC`/`SOL`等（`/USDT`ではない）。

| ペア | 表記 | 内部シンボル | 証拠金 | 備考 |
|------|------|-------------|--------|------|
| BTC永久先物 | BTC/USDC | `BTC` | USDC | 起動時にmeta APIで正確なシンボル・tick/lot sizeを取得する |
| SOL永久先物 | SOL/USDC | `SOL` | USDC | 同上 |

## 3. 戦略一覧（開発優先順）

Codexレビューに基づき、$100帯ではdirectional戦略を最優先とする。

| 優先度 | 戦略名 | 種別 | 目的 |
|--------|--------|------|------|
| **1（最優先）** | RSI 30-30 | ミーンリバージョン | 主力。$100で現実的に利益を狙える |
| **2** | シンプルMM | マーケットメイク | 学習用。低頻度・広スプレッドで慎重に |
| **3（最後）** | フルスペックMM | マーケットメイク | 資金が増えてから本格導入 |

### 3-1. RSI 30-30（最優先）

**コンセプト**: RSI Channel + ボリンジャーバンド + EMA9 の3指標で反転ポイントを検出。マルチタイムフレームでフィルタリング。

| パラメータ | 値 | 備考 |
|-----------|-----|------|
| エントリー足 | 5分 | 短期で数を打つ |
| フィルター足 | 30分 | トレンド方向の確認 |
| RSI期間 | 14 | RSI Channel（価格投影型） |
| RSI OBレベル | 70 | — |
| RSI OSレベル | 30 | — |
| ボリンジャーバンド | SMA20, σ2.0 | — |
| EMA | 9期間 | 短期トレンド方向 |
| 損切り | ATR(14) × 1.5 | ボラ連動。最低でも直近スイング外 |
| 利確 | 損切り × 2.0 | RR 2.0固定 |
| 最大同時ポジション | 1 | $100資金のため |

**エントリーロジック（買いの場合）**:
1. 価格がRSI Channel下バンド（RSI=30相当価格）以下に到達
2. 次の足で始値 > 前の足の安値（反転の兆候）
3. その足の終値 > EMA 9（短期トレンド転換）
4. BB下バンド付近であること
5. 30分足のEMA 9が上向き（上位足フィルター: 方向一致のみ）

**決済ロジック**:
- 利確: エントリー価格 + ATR(14) × 1.5 × 2.0
- 損切り: エントリー価格 - ATR(14) × 1.5
- 損切りは max(ATR × 1.5, 直近スイングの外側) を採用

### 3-2. シンプルMM（学習用）

**コンセプト**: Binance + Hyperliquid BBOのデュアルアンカーでフェア価格を算出。低頻度・広スプレッドで慎重に運用。

| パラメータ | BTC | SOL |
|-----------|-----|-----|
| スプレッド | 15bps | 25bps |
| 注文サイズ | $10 | $8 |
| 最大ポジション | $30 | $25 |
| 損切り（累計） | -$20 | -$15 |
| 注文レベル数 | 1（マルチレベルなし） | 1 |
| 更新間隔 | 1秒 | 1秒 |

**Codex指摘の反映**:
- Binanceアンカー + **Hyperliquid自身のBBO/mark価格との差分監視**を必須とする
- 乖離が閾値を超えたらquoteを停止する安全機構
- maker手数料1.5bps（往復3bps）を考慮し、最小スプレッド = 手数料 × 3 以上
- 片側停止あり（最大ポジション到達で該当サイドの注文停止）
- 最大保有時間: 300秒超過でIOCクローズ

### 3-3. フルスペックMM（将来）

Temari型の完全実装。資金$1,000以上になってから着手。
- 逆選択検出（Fill Toxicity）
- SMC構造分析（BOS/CHoCH）
- マルチレベル注文
- 非対称スプレッドQuoter（sqrt skew）
- ボラティリティ連動スプレッド

→ 現時点では設計のみ。実装はPhase 4以降。

### 3-4. Session Breakout（短期検証用）

**コンセプト**: UTC 0:00〜8:00 のレンジ高値/安値を記録し、UTC 8:00以降にレンジをブレイクした方向にエントリー。1日1回の明確なシグナル。

| パラメータ | 値 | 備考 |
|-----------|-----|------|
| レンジ時間帯 | UTC 0:00〜8:00 | アジア時間に相当 |
| ブレイク時間帯 | UTC 8:00〜23:59 | 欧米時間にトレンド発生を狙う |
| エントリー足 | 5分 | ブレイク判定用 |
| 損切り | レンジ反対側 | 明確な損切り位置 |
| 利確1（半分決済） | レンジ幅 × 1 | 早めに一部利確 |
| 利確2（残り決済） | レンジ幅 × 2 | RR比2 |
| 最大同時ポジション | 1 | — |
| 1日の最大エントリー数 | 1 | 最初のブレイクだけ拾う |
| 対象銘柄 | BTC, SOL | 両方並行稼働 |

**エントリーロジック（買いの場合）**:
1. UTC 0:00〜8:00 の期間に 5分足の高値/安値を逐次更新してレンジを確定
2. UTC 8:00以降、終値がレンジ高値を上抜けたら成行買い
3. その日の最初のブレイクのみ。2回目以降は無視

**決済ロジック**:
- レンジ幅 = range_high − range_low
- ロング時: 損切り = range_low、利確1 = entry + range_width（半分決済）、利確2 = entry + range_width × 2（残り決済）
- ショートは鏡像
- UTC 23:59 までに決済されなければ強制クローズ（日またぎ禁止）

**なぜこの戦略か**:
- 勝率30〜40%・RR比2〜4 → 3回に1回勝てば+収支
- 損切り位置が明確 → 退場リスク極小
- 1日1回の判定 → 短期検証に適する

## 4. 価格データソース

| ソース | 用途 | 接続方法 |
|--------|------|----------|
| Binance Futures | 参照価格（BBO） | WebSocket `bookTicker` |
| Hyperliquid | 実際の板情報・mark価格 | WebSocket (`l2Book`, `trades`) |
| Hyperliquid REST | キャンドルデータ（5m/30m）、meta情報 | REST API（定期ポーリング） |

**フェア価格算出（MM用）**:
```
fair_price = binance_mid + median(hl_mid - binance_mid)  # 60秒ウィンドウ
ただし: |fair_price - hl_mark| > 閾値 の場合は quote停止
```

**RSI/BB/EMA算出**: Hyperliquidのキャンドルデータから自前計算（REST取得 + 自前で最新足をリアルタイム更新）

## 5. ウォレット・nonce設計

**Codex指摘**: 複数戦略が同じAPI Walletを使うとnonce衝突する。戦略ごとにsignerを分ける。

### 構成: MetaMaskウォレット1つ + Hyperliquidサブアカウント

```
MetaMaskウォレット（1つ）
  ├── メインアカウント → BTC用（$100）→ API Wallet A
  └── サブアカウント  → SOL用（$100）→ API Wallet B
```

| アカウント | 用途 | API Wallet | 証拠金 |
|-----------|------|------------|--------|
| メインアカウント | BTC/USDC取引 | API Wallet A（専用） | $100 |
| サブアカウント | SOL/USDC取引 | API Wallet B（専用） | $100 |

- ウォレットは1つ。Hyperliquid UIでサブアカウントを作成して証拠金を分離
- 各アカウントに専用API Walletを生成（nonce衝突防止）
- API Walletは**署名専用**。照会（残高・ポジション取得）はmasterアドレスで行う
- 将来、同一ペアで2戦略同時稼働する場合はAPI Walletを追加で作成

## 6. データ構造

### 保存先: SQLite（VPSローカル、Dockerボリュームマウント）

**Codex推奨のテーブル分離・append-only設計**:

```sql
-- 注文履歴
orders (
  id INTEGER PRIMARY KEY,
  timestamp TEXT,
  strategy TEXT,        -- 'rsi30' / 'simple_mm' / 'full_mm'
  symbol TEXT,          -- 'BTC' / 'SOL'
  side TEXT,            -- 'buy' / 'sell'
  price REAL,
  size REAL,
  order_type TEXT,      -- 'limit' / 'ioc'
  status TEXT,          -- 'placed' / 'filled' / 'partial' / 'cancelled'
  hl_order_id TEXT
)

-- 約定履歴
fills (
  id INTEGER PRIMARY KEY,
  timestamp TEXT,
  order_id INTEGER REFERENCES orders(id),
  strategy TEXT,
  symbol TEXT,
  side TEXT,
  price REAL,
  size REAL,
  fee REAL,
  realized_pnl REAL
)

-- ポジションスナップショット
positions (
  id INTEGER PRIMARY KEY,
  timestamp TEXT,
  strategy TEXT,
  symbol TEXT,
  size REAL,
  entry_price REAL,
  unrealized_pnl REAL,
  margin_used REAL
)

-- 状態スナップショット（起動時reconciliation用）
state_snapshots (
  id INTEGER PRIMARY KEY,
  timestamp TEXT,
  strategy TEXT,
  data TEXT             -- JSON: open_orders, positions, balances
)

-- ヘルスチェック
heartbeat (
  id INTEGER PRIMARY KEY,
  timestamp TEXT,
  strategy TEXT,
  ws_connected INTEGER,
  last_quote_age_ms INTEGER,
  error_count INTEGER
)
```

### ドライラン用の追加テーブル
```sql
-- シャドウ実行結果（ドライラン比較用）
shadow_fills (
  id INTEGER PRIMARY KEY,
  timestamp TEXT,
  strategy TEXT,
  symbol TEXT,
  side TEXT,
  signal_price REAL,    -- シグナル発生時の価格
  would_fill_price REAL,-- 約定想定価格
  size REAL,
  estimated_pnl REAL,
  fill_model TEXT       -- 共通fillモデルの識別子
)
```

## 7. 外部サービス・API

| サービス | 用途 | 認証方法 |
|----------|------|----------|
| Hyperliquid API | 注文・照会・WebSocket | API Wallet秘密鍵（.env） |
| Binance Futures API | 参照価格WebSocket | 不要（公開データ） |
| Discord | 通知 | 既存Discord Bot活用 |

### 環境変数（.env）
| キー | 説明 | 取得元 |
|------|------|--------|
| HL_WALLET_ADDRESS | MetaMaskウォレットアドレス（共通） | MetaMask |
| HL_API_PRIVATE_KEY_A | BTC用API Wallet秘密鍵（メインアカウント） | Hyperliquid UIで生成 |
| HL_API_PRIVATE_KEY_B | SOL用API Wallet秘密鍵（サブアカウント） | Hyperliquid UIで生成 |
| HL_SUB_ACCOUNT_ADDRESS | サブアカウントアドレス | Hyperliquid UIで確認 |
| DISCORD_WEBHOOK_URL | Discord通知用Webhook | Claude Codeが作成 |

## 8. アーキテクチャ

### asyncio設計（Codex推奨）

```
┌─────────────────────────────────────────────┐
│                 Main Event Loop              │
│                                              │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │ WS受信   │  │ 戦略判断  │  │ 注文執行   │  │
│  │ Task     │→ │ Task     │→ │ Task      │  │
│  └──────────┘  └──────────┘  └───────────┘  │
│       ↓                           ↓          │
│  ┌──────────┐              ┌───────────┐     │
│  │ Candle   │              │ DB書込み   │     │
│  │ Builder  │              │ Queue     │     │
│  └──────────┘              └───────────┘     │
│                                   ↓          │
│                            ┌───────────┐     │
│                            │ Discord   │     │
│                            │ 通知Queue │     │
│                            └───────────┘     │
└─────────────────────────────────────────────┘
```

**役割分離**:
- WS受信Task: Binance + Hyperliquid WebSocket接続管理、ping送信（60秒切断防止）、再接続+状態再構築
- 戦略判断Task: 市場データを受け取り、各戦略のシグナル判定
- 注文執行Task: 注文の発行・キャンセル。nonce管理
- DB書込みQueue: SQLiteへの非同期書き込み（メインループをブロックしない）
- Discord通知Queue: 通知の非同期送信

### WebSocket再接続設計
- ping送信: 30秒間隔（60秒タイムアウト防止）
- 切断検出: pong未受信 or 例外
- 再接続: 指数バックオフ（1s → 2s → 4s → 最大30s）
- **状態再構築**: 再接続後にREST APIでopen orders / positions / balancesを取得してローカル状態と照合

### 起動時reconciliation（Codex推奨）
1. REST APIでopen orders取得 → 不明な注文があればキャンセル
2. REST APIでpositions取得 → ローカルDBの最終スナップショットと照合
3. 差分があればローカル状態を修正
4. 正常状態を確認してからメインループ開始

## 9. 通知（Discord）

既存Discord Botに新チャンネル `#trading-bot` を追加。Webhookで通知。

| イベント | 通知内容 |
|---------|---------|
| bot起動/停止 | 戦略名、モード（dry/live）、資金残高 |
| エントリー | 戦略、ペア、方向、価格、サイズ |
| 決済 | PnL、保有時間 |
| 損切り発動 | 損失額、累計PnL |
| エラー | エラー内容、自動リトライ状況 |
| 日次サマリー | 取引回数、勝率、累計PnL、最大DD |
| ヘルスチェック | 6時間ごとにWS接続状態・ポジション状況 |

## 10. Phase 0: セットアップ

所要時間: ユーザー作業 約15分

**Claude Codeが完了させること:**
- [ ] プロジェクト作成（ローカル）+ 初期コード作成
- [ ] .env.example 作成（キー名のみ、値なし）
- [ ] Dockerfile + docker-compose.yml 作成
- [ ] Discordに `#trading-bot` チャンネル作成 + Webhook URL取得
- [ ] scp でVPSに転送

**ユーザーがやること:**
- [ ] bot専用MetaMaskウォレット作成（1つ）
- [ ] $200をHyperliquidに入金（Arbitrum経由）
- [ ] Hyperliquid UIでサブアカウント作成、$100を振り分け
- [ ] メインアカウント用API Wallet A を生成
- [ ] サブアカウント用API Wallet B を生成
- [ ] VPSにSSH接続して.env作成:
  ```
  ssh root@207.180.238.184
  cp /opt/docker/hyperliquid-bot/.env.example /opt/docker/hyperliquid-bot/.env
  nano /opt/docker/hyperliquid-bot/.env
  # API Wallet秘密鍵・ウォレットアドレス等を入力
  chmod 600 /opt/docker/hyperliquid-bot/.env
  ```

## 11. フェーズ定義

| Phase | 内容 | 完了条件 |
|-------|------|----------|
| 0 | セットアップ | ユーザー作業完了（ウォレット・.env） |
| 1 | **RSI 30-30 実装 + ドライラン** | BTC/SOLでシグナル発生・ログ記録・Discord通知が動作 |
| 2 | **RSI 30-30 少額実弾テスト** | 5日以上安定稼働、PnL記録正常 |
| 3 | **シンプルMM 実装 + ドライラン** | quote配置・在庫管理・損切りが動作 |
| 4 | **シンプルMM 少額実弾テスト** | 5日以上安定稼働 |
| 5 | Discord操作UI | 開始/停止/戦略切替/状態確認がDiscordから可能 |
| 6 | フルスペックMM（資金増加後） | Temari型の完全実装 |

### 検証フロー詳細

**Phase 1: ドライラン（シャドウ実行）**
- 実際に注文は出さない
- リアルタイムの板データでシグナルを検出し、shadow_fillsテーブルに記録
- **共通fillモデル**で約定判定（touchではなく、実際の板の深さを考慮）
- **最低5日間**実行（複数のボラ局面を跨ぐため — Codex指摘）
- Discord通知でリアルタイムに状況把握

**Phase 2: 少額実弾**
- ドライランの結果を検証してからエントリー
- 最小注文サイズで開始
- ドライランとの乖離を記録・比較

## 12. ファイル構成

```
hyperliquid-bot/
├── DESIGN.md                  — この設計書
├── CLAUDE.md                  — Claude Code用ルール
├── docker-compose.yml         — bot + bot-network定義
├── Dockerfile                 — Python slim
├── requirements.txt           — 依存パッケージ
├── .env.example               — 環境変数テンプレート
├── .gitignore                 — .env, __pycache__, *.db
├── src/
│   ├── main.py                — エントリポイント（CLI引数: --strategy, --mode, --symbol）
│   ├── config.py              — 設定管理（環境変数 + 戦略パラメータ）
│   ├── exchange/
│   │   ├── hyperliquid.py     — HL API接続・注文・照会
│   │   ├── binance_feed.py    — Binance WebSocket（参照価格）
│   │   └── ws_manager.py      — WebSocket接続管理（ping/再接続/状態再構築）
│   ├── strategies/
│   │   ├── base.py            — 戦略基底クラス
│   │   ├── rsi30.py           — RSI 30-30 戦略
│   │   ├── simple_mm.py       — シンプルMM 戦略
│   │   └── full_mm.py         — フルスペックMM（Phase 6）
│   ├── indicators/
│   │   ├── rsi_channel.py     — RSI Channel（価格投影型）
│   │   ├── bollinger.py       — ボリンジャーバンド
│   │   ├── ema.py             — EMA
│   │   └── atr.py             — ATR
│   ├── risk/
│   │   ├── position.py        — ポジション管理（楽観更新+定期同期）
│   │   └── risk_manager.py    — PnL追跡・損切り・最大ポジション
│   ├── data/
│   │   ├── candle_builder.py  — リアルタイムキャンドル生成
│   │   └── db.py              — SQLite操作（非同期キュー）
│   ├── notify/
│   │   └── discord.py         — Discord Webhook通知
│   └── utils/
│       ├── logger.py          — 構造化ログ
│       └── reconcile.py       — 起動時reconciliation
└── data/
    └── bot.db                 — SQLiteファイル（Dockerボリューム）
```

## 13. Docker構成

```yaml
# docker-compose.yml
services:
  hyperliquid-bot:
    build: .
    container_name: hyperliquid-bot
    restart: unless-stopped
    env_file: .env
    volumes:
      - bot-data:/app/data
    networks:
      - bot-network    # 既存サービスとは分離
    command: >
      python src/main.py
      --strategy rsi30
      --symbol BTC
      --mode dry

volumes:
  bot-data:

networks:
  bot-network:
    driver: bridge
```

- 戦略・ペア・モードはコマンド引数で切替
- bot専用ネットワークで既存コンテナと分離（Codex推奨）
- restart: unless-stopped でVPS再起動時に自動復帰
- SQLiteはDockerボリュームに永続化

## 14. やらないこと
- バックテスト機能（ドライラン + 少額テストで代替）
- Web管理画面（Discord操作UIで代替）
- 複数取引所対応（Hyperliquidのみ）
- フルスペックMM（Phase 6まで着手しない）
- 自動複利（手動で資金追加）

## 15. 既知の制約・注意

| 制約 | 対策 |
|------|------|
| HL maker手数料 1.5bps | MM最小スプレッド = 手数料×3以上 |
| HL rate limit（出来高連動） | 小資本MMは注文頻度を抑制（1秒間隔以上） |
| nonce 上限100個 | 戦略ごとにAPI Wallet分離 |
| WebSocket 60秒タイムアウト | 30秒間隔でping送信 |
| 同一ペア両建て不可 | ペアで戦略を分ける or 時間で切替 |
| ドライランと実弾の乖離 | shadow_fillsで記録し、実弾と比較検証 |
| Contabo VPSはEUリージョン | HLサーバー（東京推定）との遅延あり。MM高頻度には不利だが、RSI/低頻度MMなら問題なし |
