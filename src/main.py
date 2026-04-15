"""Hyperliquid 自動売買Bot — メインエントリポイント"""

import argparse
import asyncio
import signal
import sys
import time

from src.config import BotConfig
from src.exchange.hyperliquid import HyperliquidClient
from src.exchange.binance_feed import BinanceFeed
from src.strategies.base import SignalType
from src.strategies.rsi30 import RSI30Strategy
from src.strategies.simple_mm import SimpleMMStrategy
from src.strategies.full_mm import FullMMStrategy
from src.data.candle_builder import CandleBuilder, Candle
from src.data.db import Database
from src.notify.discord import DiscordNotifier
from src.risk.position import PositionTracker
from src.risk.risk_manager import RiskManager
from src.utils.logger import get_logger
from src.utils.reconcile import reconcile_on_startup

log = get_logger("main")


class Bot:
    """メインBotクラス — 全コンポーネントの統合"""

    def __init__(self, config: BotConfig):
        self.cfg = config
        self._running = False

        # コンポーネント
        self.hl = HyperliquidClient(
            wallet_address=config.account_address,
            api_private_key=config.api_private_key if config.mode == "live" else "",
            account_address=config.account_address,
        )
        self.binance = BinanceFeed(config.symbol)
        self.db = Database()
        self.discord = DiscordNotifier(config.discord_webhook_url)
        self.position_tracker = PositionTracker()

        # 戦略
        self.strategy = self._create_strategy()

        # リスク管理
        risk_cfg = self._get_risk_config()
        self.risk = RiskManager(
            max_loss_usd=risk_cfg["max_loss"],
            max_position_usd=risk_cfg["max_position"],
        )

        # キャンドルビルダー
        self.candle_5m = CandleBuilder(interval_seconds=300)
        self.candle_30m = CandleBuilder(interval_seconds=1800)

    def _create_strategy(self):
        if self.cfg.strategy == "rsi30":
            return RSI30Strategy(self.cfg.symbol, self.cfg.mode, self.cfg.rsi30)
        elif self.cfg.strategy == "simple_mm":
            return SimpleMMStrategy(self.cfg.symbol, self.cfg.mode, self.cfg.simple_mm)
        elif self.cfg.strategy == "full_mm":
            return FullMMStrategy(self.cfg.symbol, self.cfg.mode, self.cfg.simple_mm)
        raise ValueError(f"Unknown strategy: {self.cfg.strategy}")

    def _get_risk_config(self) -> dict:
        if self.cfg.strategy == "rsi30":
            return {
                "max_loss": self.cfg.rsi30.max_loss_usd,
                "max_position": self.cfg.rsi30.max_position_usd,
            }
        elif self.cfg.strategy == "simple_mm":
            return {
                "max_loss": self.cfg.simple_mm.max_loss_usd,
                "max_position": self.cfg.simple_mm.max_position_usd,
            }
        return {"max_loss": 20.0, "max_position": 30.0}

    async def start(self):
        """Bot起動"""
        self._running = True
        log.info(
            f"Starting bot: strategy={self.cfg.strategy} "
            f"symbol={self.cfg.symbol} mode={self.cfg.mode}"
        )

        # 初期化
        await self.db.connect()
        await self.discord.start()
        await self.hl.connect()

        # Reconciliation（live modeのみ）
        if self.cfg.mode == "live":
            await reconcile_on_startup(self.hl, self.db, self.position_tracker)

        # 過去キャンドルをロード
        await self._load_historical_candles()

        # 残高取得
        balance = self.hl.get_account_balance()

        # 起動通知
        await self.discord.notify_startup(
            self.cfg.strategy, self.cfg.mode, self.cfg.symbol, balance
        )

        # WebSocket接続
        await self.binance.start()
        await self.hl.start_ws(
            self.cfg.symbol,
            on_trade=self._on_hl_trade,
        )

        # メインループ
        tasks = [
            asyncio.create_task(self._main_loop()),
            asyncio.create_task(self._health_check_loop()),
            asyncio.create_task(self._daily_summary_loop()),
            asyncio.create_task(self._position_sync_loop()),
        ]

        # MM戦略の場合はquote更新ループを追加
        if self.cfg.strategy in ("simple_mm", "full_mm"):
            tasks.append(asyncio.create_task(self._mm_quote_loop()))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def _load_historical_candles(self):
        """過去キャンドルデータでインジケーターをウォームアップ"""
        log.info("Loading historical candles...")

        # 5分足
        candles_5m = self.hl.get_candles(self.cfg.symbol, "5m", limit=200)
        for c in candles_5m:
            candle = Candle(
                timestamp=c["t"] / 1000,
                open=float(c["o"]),
                high=float(c["h"]),
                low=float(c["l"]),
                close=float(c["c"]),
                volume=float(c.get("v", 0)),
            )
            self.candle_5m.load_single(candle)
            if self.cfg.strategy == "rsi30":
                self.strategy.on_candle(candle)

        # 30分足
        candles_30m = self.hl.get_candles(self.cfg.symbol, "30m", limit=100)
        for c in candles_30m:
            candle = Candle(
                timestamp=c["t"] / 1000,
                open=float(c["o"]),
                high=float(c["h"]),
                low=float(c["l"]),
                close=float(c["c"]),
                volume=float(c.get("v", 0)),
            )
            self.candle_30m.load_single(candle)
            if self.cfg.strategy == "rsi30":
                self.strategy.on_filter_candle(candle)

        log.info(
            f"Loaded {len(candles_5m)} 5m candles, {len(candles_30m)} 30m candles. "
            f"Strategy ready: {self.strategy.ready()}"
        )

    async def _on_hl_trade(self, trade: dict):
        """HLトレードデータのコールバック"""
        price = float(trade.get("px", 0))
        size = float(trade.get("sz", 0))
        ts = trade.get("time", time.time() * 1000) / 1000

        # キャンドルビルダーに投入
        completed_5m = self.candle_5m.update(price, size, ts)
        completed_30m = self.candle_30m.update(price, size, ts)

        # 30分足確定 → フィルター更新
        if completed_30m and self.cfg.strategy == "rsi30":
            self.strategy.on_filter_candle(completed_30m)

        # 5分足確定 → シグナル判定
        if completed_5m:
            await self._process_candle(completed_5m)

        # リアルタイムSL/TP監視
        self.strategy.on_trade(price, size, ts)

        # MM戦略: 価格更新
        if self.cfg.strategy in ("simple_mm", "full_mm"):
            hl_mid = price
            hl_mark = price
            if self.binance.ready:
                self.strategy.update_prices(self.binance.mid, hl_mid, hl_mark)

    async def _process_candle(self, candle: Candle):
        """キャンドル確定時のシグナル処理"""
        signal = self.strategy.on_candle(candle)

        if signal.type == SignalType.NONE:
            return

        # リスクチェック
        pos = self.position_tracker.get(self.cfg.symbol)
        pos_usd = abs(pos.size * candle.close)
        if not self.risk.can_open(self.cfg.symbol, signal.size_usd, pos_usd):
            log.warning("Risk limit reached, skipping signal")
            return

        if self.risk.should_stop():
            log.warning(f"Max loss reached ({self.risk.net_pnl:.2f}), stopping")
            await self.discord.notify_stop_loss(
                abs(self.risk.net_pnl), self.risk.net_pnl
            )
            return

        # 注文実行 or ドライラン記録
        is_buy = signal.type == SignalType.BUY
        side = "buy" if is_buy else "sell"

        if self.cfg.mode == "live":
            size = signal.size_usd / candle.close
            result = await self.hl.place_order(
                symbol=self.cfg.symbol,
                is_buy=is_buy,
                size=size,
                price=signal.price,
                order_type="limit",
            )
            if result:
                await self.db.insert_order(
                    strategy=self.strategy.name,
                    symbol=self.cfg.symbol,
                    side=side,
                    price=signal.price,
                    size=size,
                    order_type="limit",
                    status="placed",
                )
        else:
            # ドライラン: shadow_fillsに記録
            await self.db.insert_shadow_fill(
                strategy=self.strategy.name,
                symbol=self.cfg.symbol,
                side=side,
                signal_price=signal.price,
                would_fill_price=signal.price,
                size=signal.size_usd / signal.price,
                estimated_pnl=0.0,
                fill_model="touch",
            )

        # Discord通知（stateを渡して詳細表示）
        state = self.strategy.get_state() if hasattr(self.strategy, 'get_state') else None
        balance = self.hl.get_account_balance() or 100.0  # ドライランは$100想定
        await self.discord.notify_entry(
            strategy=self.strategy.name,
            symbol=self.cfg.symbol,
            side=side,
            price=signal.price,
            size=signal.size_usd,
            state=state,
            balance=balance,
        )

        log.info(
            f"Signal executed: {side} {self.cfg.symbol} @ {signal.price:.2f} "
            f"SL={signal.stop_loss:.2f} TP={signal.take_profit:.2f} "
            f"mode={self.cfg.mode}"
        )

    async def _mm_quote_loop(self):
        """MM戦略: 定期的にquoteを計算しログ/通知"""
        interval = self.cfg.simple_mm.update_interval_ms / 1000
        last_notify = 0.0
        notify_interval = 300  # 5分ごとにDiscord通知
        while self._running:
            await asyncio.sleep(interval)
            if not hasattr(self.strategy, 'get_quotes'):
                continue
            if not self.strategy.ready():
                continue

            quotes = self.strategy.get_quotes()
            if not quotes["should_quote"]:
                continue

            # ドライラン: shadow_fillsに記録（bidとask両方）
            # マルチレベル対応
            levels = quotes.get("levels")
            if levels:
                for level in levels:
                    for side_key, side_name in [("bid", "buy"), ("ask", "sell")]:
                        price = level.get(f"{side_key}_price", 0)
                        size = level.get(f"{side_key}_size", 0)
                        if price > 0 and size > 0:
                            await self.db.insert_shadow_fill(
                                strategy=self.strategy.name,
                                symbol=self.cfg.symbol,
                                side=side_name,
                                signal_price=price,
                                would_fill_price=price,
                                size=size,
                                estimated_pnl=0.0,
                                fill_model="mm_quote",
                            )
            else:
                for side_key, side_name in [("bid", "buy"), ("ask", "sell")]:
                    price = quotes.get(f"{side_key}_price", 0)
                    size = quotes.get(f"{side_key}_size", 0)
                    if price > 0 and size > 0:
                        await self.db.insert_shadow_fill(
                            strategy=self.strategy.name,
                            symbol=self.cfg.symbol,
                            side=side_name,
                            signal_price=price,
                            would_fill_price=price,
                            size=size,
                            estimated_pnl=0.0,
                            fill_model="mm_quote",
                        )

            # 5分ごとにDiscord通知
            now = time.time()
            if now - last_notify >= notify_interval:
                last_notify = now
                state = self.strategy.get_state() if hasattr(self.strategy, 'get_state') else None
                await self.discord.notify_mm_quote(
                    strategy=self.strategy.name,
                    symbol=self.cfg.symbol,
                    quotes=quotes,
                    state=state,
                )

    async def _main_loop(self):
        """メインループ — 状態監視"""
        while self._running:
            await asyncio.sleep(1)

    async def _health_check_loop(self):
        """6時間ごとのヘルスチェック"""
        while self._running:
            await asyncio.sleep(6 * 3600)
            pos = self.position_tracker.get(self.cfg.symbol)
            await self.discord.notify_health(
                strategy=self.strategy.name,
                ws_connected=self.hl.ws_connected and self.binance.connected,
                position_info=f"{pos.size} @ {pos.entry_price:.2f}",
            )
            await self.db.insert_heartbeat(
                strategy=self.strategy.name,
                ws_connected=self.hl.ws_connected and self.binance.connected,
                last_quote_age_ms=self.hl.ws_last_msg_age_ms,
                error_count=0,
            )

    async def _daily_summary_loop(self):
        """毎日0:00 UTCに日次サマリーを送信"""
        while self._running:
            # 次の0:00 UTCまで待機
            now = time.time()
            tomorrow = (int(now / 86400) + 1) * 86400
            await asyncio.sleep(tomorrow - now + 60)  # 1分の余裕

            summary = await self.db.get_daily_summary(self.strategy.name)
            await self.discord.notify_daily_summary(summary)

    async def _position_sync_loop(self):
        """5秒ごとにポジションをサーバーと同期"""
        while self._running:
            await asyncio.sleep(5)
            if self.cfg.mode != "live":
                continue
            positions = self.hl.get_positions()
            for pos in positions:
                if pos["symbol"] == self.cfg.symbol:
                    self.position_tracker.sync_from_exchange(
                        symbol=pos["symbol"],
                        size=pos["size"],
                        entry_price=pos["entry_price"],
                        unrealized_pnl=pos["unrealized_pnl"],
                    )

    async def shutdown(self):
        """グレースフルシャットダウン"""
        log.info("Shutting down...")
        self._running = False

        # live modeならポジション決済
        if self.cfg.mode == "live":
            pos = self.position_tracker.get(self.cfg.symbol)
            if pos.size != 0:
                log.info(f"Closing position: {pos.size}")
                await self.hl.place_order(
                    symbol=self.cfg.symbol,
                    is_buy=pos.size < 0,
                    size=abs(pos.size),
                    order_type="market",
                    reduce_only=True,
                )

        await self.discord.notify_shutdown("Manual shutdown")
        await self.hl.close()
        await self.binance.close()
        await self.discord.close()
        await self.db.close()
        log.info("Shutdown complete")


def parse_args():
    parser = argparse.ArgumentParser(description="Hyperliquid Trading Bot")
    parser.add_argument(
        "--strategy", choices=["rsi30", "simple_mm", "full_mm"],
        default="rsi30", help="Trading strategy"
    )
    parser.add_argument(
        "--symbol", default="BTC", help="Trading symbol (BTC, SOL, etc.)"
    )
    parser.add_argument(
        "--mode", choices=["dry", "live"], default="dry",
        help="dry=no real orders, live=real trading"
    )
    return parser.parse_args()


async def main():
    args = parse_args()
    config = BotConfig.from_env(
        strategy=args.strategy,
        symbol=args.symbol,
        mode=args.mode,
    )

    bot = Bot(config)

    # シグナルハンドラ
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.shutdown()))

    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
