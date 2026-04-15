# CLAUDE.md — Hyperliquid 自動売買Bot

## プロジェクト概要
Hyperliquid perpDEXでBTC/USDC・SOL/USDCの自動売買botを運用するプロジェクト。

## 開発ルール
- DESIGN.md が設計の正。迷ったらDESIGN.mdに従う
- Python 3.12 + asyncio
- 1ファイル500行以内
- セキュリティ: 秘密鍵・APIキーは絶対にハードコードしない（.env管理）
- テスト: ドライランモード（--mode dry）で必ず動作確認してからliveに切り替え

## デプロイ
- VPS: Contabo（ssh root@207.180.238.184）
- パス: /opt/docker/hyperliquid-bot/
- Docker network: bot-network（既存サービスと分離）
- 手順: scp → docker compose up -d --build → docker logs で確認

## 戦略
- RSI 30-30（最優先）→ シンプルMM → フルスペックMM の順で開発
- 戦略切替: --strategy rsi30 / simple_mm / full_mm

## 注意
- Hyperliquid perpはUSDC証拠金。内部シンボルは `BTC` / `SOL`
- API Walletは署名専用。照会はmasterアドレスで行う
- WebSocketは30秒間隔でping（60秒タイムアウト防止）
- 起動時にreconciliation必須（open orders / positions照合）
