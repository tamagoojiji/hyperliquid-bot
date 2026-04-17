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
from src.strategies.pivot_bounce import PivotBounceStrategy
from src.strategies.breakout import BreakoutStrategy
from src.strategies.macd_vwap import MACDVWAPStrategy
from src.strategies.rsi30_fibo import RSI30FiboStrategy
from src.strategies.pivot_bb import PivotBBStrategy
from src.strategies.pivot_vwap import PivotVWAPStrategy
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
        elif self.cfg.strategy == "pivot_bounce":
            return PivotBounceStrategy(self.cfg.symbol, self.cfg.mode, self.cfg.rsi30)
        elif self.cfg.strategy == "breakout":
            return BreakoutStrategy(self.cfg.symbol, self.cfg.mode, self.cfg.rsi30)
        elif self.cfg.strategy == "macd_vwap":
            return MACDVWAPStrategy(self.cfg.symbol, self.cfg.mode, self.cfg.rsi30)
        elif self.cfg.strategy == "rsi30_fibo":
            return RSI30FiboStrategy(self.cfg.symbol, self.cfg.mode, self.cfg.rsi30)
        elif self.cfg.strategy == "pivot_bb":
            return PivotBBStrategy(self.cfg.symbol, self.cfg.mode, self.cfg.rsi30)
        elif self.cfg.strategy == "pivot_vwap":
            return PivotVWAPStrategy(self.cfg.symbol, self.cfg.mode, self.cfg.rsi30)
        raise ValueError(f"Unknown strategy: {self.cfg.strategy}")

    def _get_risk_config(self) -> dict:
        if self.cfg.strategy in ("rsi30", "pivot_bounce", "breakout", "macd_vwap", "rsi30_fibo", "pivot_bb", "pivot_vwap"):
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
            if self.cfg.strategy in ("pivot_bounce", "breakout", "macd_vwap", "rsi30_fibo", "pivot_bb", "pivot_vwap"):
                self.strategy.on_candle(candle)

        # 30分足 — rsi30は200EMA用に多めに取得
        candles_30m_limit = 300 if self.cfg.strategy == "rsi30" else 100
        candles_30m = self.hl.get_candles(self.cfg.symbol, "30m", limit=candles_30m_limit)
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
                self.strategy.on_candle(candle)
            elif self.cfg.strategy in ("pivot_bounce", "breakout", "macd_vwap", "rsi30_fibo", "pivot_bb", "pivot_vwap"):
                self.strategy.on_filter_candle(candle)

        log.info(
            f"Loaded {len(candles_5m)} 5m candles, {len(candles_30m)} 30m candles. "
            f"Strategy ready: {self.strategy.ready()}"
        )

    async def _on_hl_trade(self, trade: dict):
        """HLトレードデータのコールバック"""
        try:
            price = float(trade.get("px", 0))
            size = float(trade.get("sz", 0))
            ts = trade.get("time", time.time() * 1000) / 1000

            # キャンドルビルダーに投入
            completed_5m = self.candle_5m.update(price, size, ts)
            completed_30m = self.candle_30m.update(price, size, ts)

            # 30分足確定
            if completed_30m:
                if self.cfg.strategy == "rsi30":
                    # rsi30は30分足でシグナル判定
                    await self._process_candle(completed_30m)
                elif self.cfg.strategy in ("pivot_bounce", "breakout", "macd_vwap", "rsi30_fibo", "pivot_bb", "pivot_vwap"):
                    # 他戦略は30分足をフィルターとして使用
                    self.strategy.on_filter_candle(completed_30m)

            # 5分足確定 → シグナル判定（rsi30以外）
            if completed_5m and self.cfg.strategy != "rsi30":
                await self._process_candle(completed_5m)

            # リアルタイムSL/TP監視
            self.strategy.on_trade(price, size, ts)

            # MM戦略: 価格更新
            if self.cfg.strategy in ("simple_mm", "full_mm"):
                hl_mid = price
                hl_mark = price
                if self.binance.ready:
                    self.strategy.update_prices(self.binance.mid, hl_mid, hl_mark)
        except Exception as e:
            log.error(f"Error in _on_hl_trade: {e}", exc_info=True)

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
        """MM戦略: 仮想約定シミュレーション付きドライラン

        仕組み:
        1. 毎秒quoteを計算（bid/askの価格とサイズ）
        2. 実際のHLトレード価格がbid以下に下がったら「買い約定」
        3. 実際のHLトレード価格がask以上に上がったら「売り約定」
        4. 仮想ポジションを持ち、反対側で決済されたらPnLを計算
        5. エントリー・決済をDiscordに通知
        """
        interval = self.cfg.simple_mm.update_interval_ms / 1000

        # 仮想ポジション管理
        virtual_pos = 0.0        # +ロング / -ショート
        virtual_entry = 0.0      # エントリー価格
        virtual_pnl = 0.0        # 累計PnL
        virtual_trades = 0       # 取引回数
        virtual_wins = 0         # 勝ち回数

        # 累計計測の開始時刻（JST）
        # SOL: 2026-04-16 00:00 JST = 2026-04-15 15:00 UTC
        # BTC: 2026-04-16 07:00 JST = 2026-04-15 22:00 UTC
        from datetime import datetime, timezone, timedelta
        if self.cfg.symbol == "SOL":
            pnl_start_utc = datetime(2026, 4, 15, 15, 0, 0, tzinfo=timezone.utc).timestamp()
        else:  # BTC
            pnl_start_utc = datetime(2026, 4, 15, 22, 0, 0, tzinfo=timezone.utc).timestamp()
        pnl_started = False

        while self._running:
            await asyncio.sleep(interval)
            if not hasattr(self.strategy, 'get_quotes'):
                continue
            if not self.strategy.ready():
                continue

            # 開始時刻まで待機（quoteは計算するが累計に含めない）
            if not pnl_started:
                if time.time() >= pnl_start_utc:
                    pnl_started = True
                    virtual_pnl = 0.0
                    virtual_trades = 0
                    virtual_wins = 0
                    log.info(f"PnL tracking started for {self.cfg.symbol}")

            quotes = self.strategy.get_quotes()
            if not quotes["should_quote"]:
                continue

            # quote価格を取得
            levels = quotes.get("levels")
            if levels:
                l0 = levels[0]
                bid = l0.get("bid_price", 0)
                ask = l0.get("ask_price", 0)
                bid_sz = l0.get("bid_size", 0)
                ask_sz = l0.get("ask_size", 0)
            else:
                bid = quotes.get("bid_price", 0)
                ask = quotes.get("ask_price", 0)
                bid_sz = quotes.get("bid_size", 0)
                ask_sz = quotes.get("ask_size", 0)

            if bid <= 0 or ask <= 0:
                continue

            # 現在のHL価格を取得
            current = self.candle_5m.current
            if not current:
                continue
            hl_price = current.close

            # === 仮想約定判定 ===

            if virtual_pos == 0.0:
                # ポジションなし → bid/ask両方で待機
                if hl_price <= bid and bid_sz > 0:
                    # 買い約定（誰かがbidに売ってきた）
                    virtual_pos = bid_sz
                    virtual_entry = bid
                    state = self.strategy.get_state() if hasattr(self.strategy, 'get_state') else None
                    await self.discord.notify_entry(
                        strategy=self.strategy.name,
                        symbol=self.cfg.symbol,
                        side="buy",
                        price=bid,
                        size=bid_sz * bid,
                        state=state,
                        balance=100.0,
                    )
                    await self.db.insert_shadow_fill(
                        strategy=self.strategy.name, symbol=self.cfg.symbol,
                        side="buy", signal_price=bid, would_fill_price=bid,
                        size=bid_sz, estimated_pnl=0.0, fill_model="mm_sim",
                    )

                elif hl_price >= ask and ask_sz > 0:
                    # 売り約定（誰かがaskに買ってきた）
                    virtual_pos = -ask_sz
                    virtual_entry = ask
                    state = self.strategy.get_state() if hasattr(self.strategy, 'get_state') else None
                    await self.discord.notify_entry(
                        strategy=self.strategy.name,
                        symbol=self.cfg.symbol,
                        side="sell",
                        price=ask,
                        size=ask_sz * ask,
                        state=state,
                        balance=100.0,
                    )
                    await self.db.insert_shadow_fill(
                        strategy=self.strategy.name, symbol=self.cfg.symbol,
                        side="sell", signal_price=ask, would_fill_price=ask,
                        size=ask_sz, estimated_pnl=0.0, fill_model="mm_sim",
                    )

            elif virtual_pos > 0:
                # ロング中 → ask価格まで上がったら決済
                if hl_price >= ask:
                    pnl = (ask - virtual_entry) * virtual_pos
                    virtual_pnl += pnl
                    virtual_trades += 1
                    if pnl > 0:
                        virtual_wins += 1
                    await self.discord.notify_exit(
                        strategy=self.strategy.name,
                        symbol=self.cfg.symbol,
                        side="buy",
                        price=ask,
                        size=virtual_pos * ask,
                        pnl=pnl,
                        hold_time="MM",
                        total_pnl=virtual_pnl,
                    )
                    await self.db.insert_shadow_fill(
                        strategy=self.strategy.name, symbol=self.cfg.symbol,
                        side="sell", signal_price=ask, would_fill_price=ask,
                        size=virtual_pos, estimated_pnl=pnl, fill_model="mm_sim_exit",
                    )
                    virtual_pos = 0.0
                    virtual_entry = 0.0

            elif virtual_pos < 0:
                # ショート中 → bid価格まで下がったら決済
                if hl_price <= bid:
                    pnl = (virtual_entry - bid) * abs(virtual_pos)
                    virtual_pnl += pnl
                    virtual_trades += 1
                    if pnl > 0:
                        virtual_wins += 1
                    await self.discord.notify_exit(
                        strategy=self.strategy.name,
                        symbol=self.cfg.symbol,
                        side="sell",
                        price=bid,
                        size=abs(virtual_pos) * bid,
                        pnl=pnl,
                        hold_time="MM",
                        total_pnl=virtual_pnl,
                    )
                    await self.db.insert_shadow_fill(
                        strategy=self.strategy.name, symbol=self.cfg.symbol,
                        side="buy", signal_price=bid, would_fill_price=bid,
                        size=abs(virtual_pos), estimated_pnl=pnl, fill_model="mm_sim_exit",
                    )
                    virtual_pos = 0.0
                    virtual_entry = 0.0

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
        "--strategy", choices=["rsi30", "simple_mm", "full_mm", "pivot_bounce", "breakout", "macd_vwap", "rsi30_fibo", "pivot_bb", "pivot_vwap"],
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
