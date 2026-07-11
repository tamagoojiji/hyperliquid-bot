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
from src.strategies.session_bo import SessionBreakoutStrategy
from src.strategies.bb_rsi import BBRSIStrategy
from src.strategies.donchian import DonchianStrategy
from src.strategies.ema3030 import EMA3030Strategy
from src.strategies.gmma import GMMAStrategy
from src.strategies.adx_dmi import ADXDMIStrategy
from src.strategies.anti_macd import AntiMACDStrategy
from src.strategies.gap_fill import GapFillStrategy
from src.strategies.paraboli import ParaboliStrategy
from src.strategies.kinboko import KinbokoStrategy
from src.data.candle_builder import CandleBuilder, Candle
from src.indicators.ema import EMA
from src.data.db import Database
from src.notify.discord import DiscordNotifier
from src.risk.position import PositionTracker
from src.risk.risk_manager import RiskManager
from src.risk.funding_gate import FundingGate
from src.backtest.historical import fetch_funding_history
from src.risk.dry_run_pnl import (
    VirtualPosition,
    apply_funding,
    compute_fee,
    compute_net_pnl,
)
from src.utils.logger import get_logger
from src.utils.reconcile import reconcile_on_startup

log = get_logger("main")

# 戦略名 → エントリー足（warmup・シグナル配線の単一情報源）
STRATEGY_ENTRY_TF = {
    "rsi30": "30m", "ema3030": "30m", "anti_macd": "30m",
    "simple_mm": "5m", "full_mm": "5m",
    "pivot_bounce": "5m", "breakout": "5m", "macd_vwap": "5m", "rsi30_fibo": "5m",
    "pivot_bb": "5m", "pivot_vwap": "5m", "session_bo": "5m", "bb_rsi": "5m",
    "gap_fill": "5m",
    "gmma": "1h",
    "adx_dmi": "4h", "paraboli": "4h",
    "donchian": "1d", "kinboko": "1d",
}
# 30分足をフィルターとして受け取る5分足戦略
FILTER_30M_STRATEGIES = (
    "pivot_bounce", "breakout", "macd_vwap", "rsi30_fibo", "pivot_bb", "pivot_vwap",
)


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
        self.candle_1h = CandleBuilder(interval_seconds=3600)
        self.candle_4h = CandleBuilder(interval_seconds=14400)
        self.candle_1d = CandleBuilder(interval_seconds=86400)

        # 日足200EMA — 全directional戦略の方向フィルター（上=ロングのみ/下=ショートのみ）
        self.trend_ema_1d = EMA(period=200)

        # FundingGate — 極端funding時の新規ロング抑制
        self.funding_gate = (
            FundingGate(
                percentile=self.cfg.funding_gate.percentile,
                lookback_hours=self.cfg.funding_gate.lookback_hours,
                min_samples=self.cfg.funding_gate.min_samples,
                long_action=self.cfg.funding_gate.long_action,
            )
            if (self.cfg.funding_gate.enabled
                and self.cfg.strategy not in ("simple_mm", "full_mm"))
            else None
        )

        # ドライラン用: 仮想ポジション + funding rate キャッシュ
        self._virtual_positions: dict[str, VirtualPosition] = {}
        self._current_funding_rate_1h: float | None = None
        self._virtual_pnl_total: float = 0.0
        self._virtual_fees_total: float = 0.0
        self._virtual_funding_total: float = 0.0
        self._virtual_trades: int = 0
        self._virtual_wins: int = 0

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
        elif self.cfg.strategy == "session_bo":
            return SessionBreakoutStrategy(self.cfg.symbol, self.cfg.mode, self.cfg.session_bo)
        elif self.cfg.strategy == "bb_rsi":
            return BBRSIStrategy(self.cfg.symbol, self.cfg.mode)
        elif self.cfg.strategy == "donchian":
            return DonchianStrategy(self.cfg.symbol, self.cfg.mode, self.cfg.donchian)
        elif self.cfg.strategy == "ema3030":
            return EMA3030Strategy(self.cfg.symbol, self.cfg.mode)
        elif self.cfg.strategy == "gmma":
            return GMMAStrategy(self.cfg.symbol, self.cfg.mode)
        elif self.cfg.strategy == "adx_dmi":
            return ADXDMIStrategy(self.cfg.symbol, self.cfg.mode)
        elif self.cfg.strategy == "anti_macd":
            return AntiMACDStrategy(self.cfg.symbol, self.cfg.mode)
        elif self.cfg.strategy == "gap_fill":
            return GapFillStrategy(self.cfg.symbol, self.cfg.mode)
        elif self.cfg.strategy == "kinboko":
            return KinbokoStrategy(self.cfg.symbol, self.cfg.mode)
        elif self.cfg.strategy == "paraboli":
            return ParaboliStrategy(self.cfg.symbol, self.cfg.mode)
        raise ValueError(f"Unknown strategy: {self.cfg.strategy}")

    def _get_risk_config(self) -> dict:
        if self.cfg.strategy in ("rsi30", "pivot_bounce", "breakout", "macd_vwap", "rsi30_fibo", "pivot_bb", "pivot_vwap", "bb_rsi", "ema3030", "gmma", "adx_dmi", "anti_macd", "gap_fill", "paraboli", "kinboko"):
            return {
                "max_loss": self.cfg.rsi30.max_loss_usd,
                "max_position": self.cfg.rsi30.max_position_usd,
            }
        elif self.cfg.strategy == "simple_mm":
            return {
                "max_loss": self.cfg.simple_mm.max_loss_usd,
                "max_position": self.cfg.simple_mm.max_position_usd,
            }
        elif self.cfg.strategy == "session_bo":
            return {
                "max_loss": self.cfg.session_bo.max_loss_usd,
                "max_position": self.cfg.session_bo.max_position_usd,
            }
        elif self.cfg.strategy == "donchian":
            return {
                "max_loss": self.cfg.donchian.max_loss_usd,
                "max_position": self.cfg.donchian.max_position_usd,
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

        # FundingGate 履歴シード（有効時のみ、失敗しても起動続行）
        if self.funding_gate is not None:
            self._seed_funding_gate()

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

        # funding rate 適用/ゲート更新ループを起動
        # dry PnLへのfunding適用はFUNDING_ENABLED、ゲート更新はFUNDING_GATE_ENABLEDで独立制御
        if (self.cfg.mode == "dry" and self.cfg.fees.funding_enabled) \
                or self.funding_gate is not None:
            tasks.append(asyncio.create_task(self._funding_loop()))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def _load_historical_candles(self):
        """過去キャンドルデータでインジケーターをウォームアップ

        STRATEGY_ENTRY_TF に基づき、エントリー足は strategy.on_candle へ、
        30分フィルター足は on_filter_candle へ流す。
        """
        log.info("Loading historical candles...")
        entry_tf = STRATEGY_ENTRY_TF.get(self.cfg.strategy, "5m")
        is_mm = self.cfg.strategy in ("simple_mm", "full_mm")

        def _load(builder, interval: str, limit: int,
                  feed_entry: bool, feed_filter: bool = False) -> int:
            raw = self.hl.get_candles(self.cfg.symbol, interval, limit=limit)
            for c in raw:
                candle = Candle(
                    timestamp=c["t"] / 1000,
                    open=float(c["o"]),
                    high=float(c["h"]),
                    low=float(c["l"]),
                    close=float(c["c"]),
                    volume=float(c.get("v", 0)),
                )
                if builder is not None:
                    builder.load_single(candle)
                if interval == "1d":
                    self.trend_ema_1d.update(candle.close)
                if feed_entry:
                    self.strategy.on_candle(candle)
                elif feed_filter:
                    self.strategy.on_filter_candle(candle)
            return len(raw)

        # 5分足（MM含む全戦略でビルダーは常時ロード）
        n5 = _load(self.candle_5m, "5m",
                   300 if entry_tf == "5m" else 200,
                   feed_entry=(entry_tf == "5m" and not is_mm))
        # 30分足（エントリー or フィルター）
        n30 = _load(self.candle_30m, "30m",
                    300 if entry_tf == "30m" else 100,
                    feed_entry=(entry_tf == "30m"),
                    feed_filter=(self.cfg.strategy in FILTER_30M_STRATEGIES))
        # 1時間足 / 4時間足（該当戦略のみ）
        n1h = n4h = 0
        if entry_tf == "1h":
            n1h = _load(self.candle_1h, "1h", 300, feed_entry=True)
        if entry_tf == "4h":
            n4h = _load(self.candle_4h, "4h", 300, feed_entry=True)
        # 日足（全directional戦略の200EMA方向フィルター + 1d戦略のエントリー足）
        n1d = 0
        if not is_mm:
            n1d = _load(self.candle_1d, "1d", 250, feed_entry=(entry_tf == "1d"))

        # ウォームアップ中に立ったシグナルは注文/仮想ポジションを伴わないため、
        # 内部のポジション状態を破棄する（残すと以後のエントリーが恒久ブロックされる）
        if getattr(self.strategy, "_has_position", False):
            log.warning("Discarding phantom position state from warmup")
        self.strategy.reset_position_state()

        log.info(
            f"Loaded 5m={n5} 30m={n30} 1h={n1h} 4h={n4h} 1d={n1d} candles "
            f"(EMA200_1d ready: {self.trend_ema_1d.ready}). "
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
            completed_1h = self.candle_1h.update(price, size, ts)
            completed_4h = self.candle_4h.update(price, size, ts)
            completed_1d = self.candle_1d.update(price, size, ts)

            entry_tf = STRATEGY_ENTRY_TF.get(self.cfg.strategy, "5m")

            # 日足確定 → 200EMA更新 + 日足戦略のシグナル判定
            if completed_1d:
                self.trend_ema_1d.update(completed_1d.close)
                if entry_tf == "1d":
                    await self._process_candle(completed_1d)

            if completed_4h and entry_tf == "4h":
                await self._process_candle(completed_4h)

            if completed_1h and entry_tf == "1h":
                await self._process_candle(completed_1h)

            # 30分足確定（エントリー or フィルター）
            if completed_30m:
                if entry_tf == "30m":
                    await self._process_candle(completed_30m)
                elif self.cfg.strategy in FILTER_30M_STRATEGIES:
                    self.strategy.on_filter_candle(completed_30m)

            if completed_5m and entry_tf == "5m":
                await self._process_candle(completed_5m)

            # リアルタイムSL/TP監視
            self.strategy.on_trade(price, size, ts)
            # 戦略が発行した exit イベントを処理（dry=仮想決済 / live=reduce-only実注文）
            if self.cfg.mode == "dry":
                await self._handle_virtual_exit()
            else:
                await self._handle_live_exit()

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

        # 日足200EMA方向フィルター: 上ではロングのみ、下ではショートのみ許可
        if (self.cfg.trend_filter_enabled
                and self.cfg.strategy not in ("simple_mm", "full_mm")
                and self.trend_ema_1d.ready):
            ema200 = self.trend_ema_1d.value
            blocked = (
                (signal.type == SignalType.BUY and candle.close < ema200)
                or (signal.type == SignalType.SELL and candle.close > ema200)
            )
            if blocked:
                log.info(
                    f"Trend filter blocked {signal.type.value}: "
                    f"close={candle.close:.2f} ema200_1d={ema200:.2f}"
                )
                # シグナル生成時に立てた戦略内部のポジション状態を破棄
                self.strategy.reset_position_state()
                return

        # FundingGate: 極端funding時の新規ロング抑制（ショートは素通し）
        if (self.funding_gate is not None
                and self.cfg.strategy not in ("simple_mm", "full_mm")
                and signal.type == SignalType.BUY):
            rate = self._current_funding_rate_1h
            allowed, mult, reason = self.funding_gate.check(rate)
            thr = self.funding_gate.threshold
            if not allowed:
                log.info(
                    f"[funding_gate] blocked long: rate={rate*100:.5f}%/h "
                    f"p{int(self.cfg.funding_gate.percentile)}={thr*100:.5f}%/h"
                )
                self.strategy.reset_position_state()
                return
            if mult < 1.0:
                signal.size_usd *= mult
                log.info(
                    f"[funding_gate] halved long size: rate={rate*100:.5f}%/h "
                    f"p{int(self.cfg.funding_gate.percentile)}={thr*100:.5f}%/h "
                    f"size_usd={signal.size_usd:.2f}"
                )

        # リスクチェック
        pos = self.position_tracker.get(self.cfg.symbol)
        pos_usd = abs(pos.size * candle.close)
        if not self.risk.can_open(self.cfg.symbol, signal.size_usd, pos_usd):
            log.warning("Risk limit reached, skipping signal")
            self.strategy.reset_position_state()
            return

        if self.risk.should_stop():
            log.warning(f"Max loss reached ({self.risk.net_pnl:.2f}), stopping")
            await self.discord.notify_stop_loss(
                abs(self.risk.net_pnl), self.risk.net_pnl
            )
            self.strategy.reset_position_state()
            return

        # 注文実行 or ドライラン記録
        is_buy = signal.type == SignalType.BUY
        side = "buy" if is_buy else "sell"

        if self.cfg.mode == "live":
            size = signal.size_usd / candle.close
            # 逆張り系(is_maker=True)=指値、ブレイク系(is_maker=False)=成行
            # （taker想定シグナルをPost-Only指値で出すと約定せず幽霊ポジションになる）
            order_type = "limit" if signal.is_maker else "market"
            result = await self.hl.place_order(
                symbol=self.cfg.symbol,
                is_buy=is_buy,
                size=size,
                price=signal.price if order_type == "limit" else None,
                order_type=order_type,
            )
            if result:
                await self.db.insert_order(
                    strategy=self.strategy.name,
                    symbol=self.cfg.symbol,
                    side=side,
                    price=signal.price,
                    size=size,
                    order_type=order_type,
                    status="placed",
                )
            else:
                log.warning("Order placement failed, resetting strategy state")
                self.strategy.reset_position_state()
                return
        else:
            # ドライラン: 仮想ポジション作成 + 手数料記録
            # maker/takerは戦略のSignalが宣言する（逆張り=指値maker、ブレイク=成行taker）
            is_maker = signal.is_maker
            entry_fee = compute_fee(
                signal.size_usd, is_maker,
                self.cfg.fees.maker_bps, self.cfg.fees.taker_bps,
            )
            size_base = signal.size_usd / signal.price
            # directional戦略は全て仮想ポジションで決済まで追跡する（MM系はmm_sim経路）
            if self.cfg.strategy not in ("simple_mm", "full_mm"):
                self._virtual_positions[self.cfg.symbol] = VirtualPosition(
                    strategy=self.strategy.name,
                    symbol=self.cfg.symbol,
                    side=side,
                    entry_price=signal.price,
                    size=size_base,
                    entry_time=time.time(),
                    is_maker_entry=is_maker,
                    entry_fee=entry_fee,
                )
                self._virtual_fees_total += entry_fee
            await self.db.insert_shadow_fill(
                strategy=self.strategy.name,
                symbol=self.cfg.symbol,
                side=side,
                signal_price=signal.price,
                would_fill_price=signal.price,
                size=size_base,
                estimated_pnl=0.0,
                fill_model="entry",
                fee=entry_fee,
                funding=0.0,
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

    async def _handle_virtual_exit(self):
        """戦略が発行した exit を受け取り、手数料 + funding を差し引いて記録"""
        evt = self.strategy.consume_exit_event()
        if evt is None:
            return
        vp = self._virtual_positions.pop(self.cfg.symbol, None)
        if vp is None:
            return

        exit_notional = abs(vp.size) * evt.exit_price
        exit_fee = compute_fee(
            exit_notional, evt.is_maker,
            self.cfg.fees.maker_bps, self.cfg.fees.taker_bps,
        )
        result = compute_net_pnl(vp, evt.exit_price, exit_fee)

        self._virtual_fees_total += exit_fee
        self._virtual_funding_total += result["funding"]
        self._virtual_pnl_total += result["net_pnl"]
        self._virtual_trades += 1
        if result["net_pnl"] > 0:
            self._virtual_wins += 1

        await self.db.insert_shadow_fill(
            strategy=self.strategy.name,
            symbol=self.cfg.symbol,
            side="sell" if vp.side == "buy" else "buy",
            signal_price=evt.exit_price,
            would_fill_price=evt.exit_price,
            size=abs(vp.size),
            estimated_pnl=result["net_pnl"],
            fill_model=f"exit_{evt.reason}",
            fee=exit_fee,
            funding=result["funding"],
        )

        hold_sec = time.time() - vp.entry_time
        await self.discord.notify_exit(
            strategy=self.strategy.name,
            symbol=self.cfg.symbol,
            side=vp.side,
            price=evt.exit_price,
            size=exit_notional,
            pnl=result["net_pnl"],
            hold_time=f"{int(hold_sec / 60)}m",
            total_pnl=self._virtual_pnl_total,
        )
        log.info(
            f"[dry exit] {evt.reason} raw={result['raw_pnl']:.4f} "
            f"fee={result['total_fee']:.4f} funding={result['funding']:.4f} "
            f"net={result['net_pnl']:.4f}"
        )

    async def _handle_live_exit(self):
        """live: 戦略のSL/TP発火を実際のreduce-only成行決済に変換する"""
        evt = self.strategy.consume_exit_event()
        if evt is None:
            return
        pos = self.position_tracker.get(self.cfg.symbol)
        if pos.size == 0:
            log.warning(f"Live exit ({evt.reason}) but no exchange position, skipping")
            return
        # 保有と逆方向のreduce-only成行でクローズ
        is_buy = pos.size < 0
        result = await self.hl.place_order(
            symbol=self.cfg.symbol,
            is_buy=is_buy,
            size=abs(pos.size),
            order_type="market",
            reduce_only=True,
        )
        if result:
            await self.db.insert_order(
                strategy=self.strategy.name,
                symbol=self.cfg.symbol,
                side="buy" if is_buy else "sell",
                price=evt.exit_price,
                size=abs(pos.size),
                order_type="market",
                status="placed",
            )
            await self.discord.notify_exit(
                strategy=self.strategy.name,
                symbol=self.cfg.symbol,
                side=evt.side,
                price=evt.exit_price,
                size=abs(pos.size) * evt.exit_price,
                pnl=0.0,  # 実現PnLはposition同期・fillsで確定する
                hold_time="-",
                total_pnl=self.risk.net_pnl,
            )
            log.info(
                f"[live exit] {evt.reason} reduce-only market close "
                f"size={abs(pos.size)} ref_price={evt.exit_price:.2f}"
            )
        else:
            log.error(f"Live exit order FAILED ({evt.reason}) — position remains open!")
            await self.discord.notify_error(
                f"決済注文失敗: {self.cfg.symbol} {evt.reason} — ポジションが残っています",
                "手動確認が必要",
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

        from datetime import datetime, timezone
        if self.cfg.symbol == "SOL":
            pnl_start_utc = datetime(2026, 4, 15, 15, 0, 0, tzinfo=timezone.utc).timestamp()
        else:  # BTC
            pnl_start_utc = datetime(2026, 4, 15, 22, 0, 0, tzinfo=timezone.utc).timestamp()
        pnl_started = False

        maker_bps = self.cfg.fees.maker_bps
        taker_bps = self.cfg.fees.taker_bps

        while self._running:
            await asyncio.sleep(interval)
            if not hasattr(self.strategy, 'get_quotes'):
                continue
            if not self.strategy.ready():
                continue

            if not pnl_started:
                if time.time() >= pnl_start_utc:
                    pnl_started = True
                    self._virtual_pnl_total = 0.0
                    self._virtual_fees_total = 0.0
                    self._virtual_funding_total = 0.0
                    self._virtual_trades = 0
                    self._virtual_wins = 0
                    log.info(f"PnL tracking started for {self.cfg.symbol}")

            quotes = self.strategy.get_quotes()
            if not quotes["should_quote"]:
                continue

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

            current = self.candle_5m.current
            if not current:
                continue
            hl_price = current.close

            vp = self._virtual_positions.get(self.cfg.symbol)

            # === 仮想約定判定（MM は両側とも maker 想定） ===

            if vp is None:
                if hl_price <= bid and bid_sz > 0:
                    await self._mm_open_virtual(
                        side="buy", price=bid, size_base=bid_sz, is_maker=True,
                        maker_bps=maker_bps, taker_bps=taker_bps,
                    )
                elif hl_price >= ask and ask_sz > 0:
                    await self._mm_open_virtual(
                        side="sell", price=ask, size_base=ask_sz, is_maker=True,
                        maker_bps=maker_bps, taker_bps=taker_bps,
                    )
            elif vp.side == "buy" and hl_price >= ask:
                await self._mm_close_virtual(
                    vp=vp, exit_price=ask, is_maker=True,
                    maker_bps=maker_bps, taker_bps=taker_bps,
                )
            elif vp.side == "sell" and hl_price <= bid:
                await self._mm_close_virtual(
                    vp=vp, exit_price=bid, is_maker=True,
                    maker_bps=maker_bps, taker_bps=taker_bps,
                )

    async def _mm_open_virtual(self, side, price, size_base, is_maker, maker_bps, taker_bps):
        notional = size_base * price
        entry_fee = compute_fee(notional, is_maker, maker_bps, taker_bps)
        self._virtual_positions[self.cfg.symbol] = VirtualPosition(
            strategy=self.strategy.name,
            symbol=self.cfg.symbol,
            side=side,
            entry_price=price,
            size=size_base,
            entry_time=time.time(),
            is_maker_entry=is_maker,
            entry_fee=entry_fee,
        )
        self._virtual_fees_total += entry_fee
        state = self.strategy.get_state() if hasattr(self.strategy, 'get_state') else None
        await self.discord.notify_entry(
            strategy=self.strategy.name, symbol=self.cfg.symbol,
            side=side, price=price, size=notional,
            state=state, balance=100.0,
        )
        await self.db.insert_shadow_fill(
            strategy=self.strategy.name, symbol=self.cfg.symbol,
            side=side, signal_price=price, would_fill_price=price,
            size=size_base, estimated_pnl=0.0, fill_model="mm_sim",
            fee=entry_fee, funding=0.0,
        )

    async def _mm_close_virtual(self, vp, exit_price, is_maker, maker_bps, taker_bps):
        exit_notional = abs(vp.size) * exit_price
        exit_fee = compute_fee(exit_notional, is_maker, maker_bps, taker_bps)
        result = compute_net_pnl(vp, exit_price, exit_fee)

        self._virtual_fees_total += exit_fee
        self._virtual_funding_total += result["funding"]
        self._virtual_pnl_total += result["net_pnl"]
        self._virtual_trades += 1
        if result["net_pnl"] > 0:
            self._virtual_wins += 1

        await self.discord.notify_exit(
            strategy=self.strategy.name, symbol=self.cfg.symbol,
            side=vp.side, price=exit_price, size=exit_notional,
            pnl=result["net_pnl"], hold_time="MM",
            total_pnl=self._virtual_pnl_total,
        )
        await self.db.insert_shadow_fill(
            strategy=self.strategy.name, symbol=self.cfg.symbol,
            side="sell" if vp.side == "buy" else "buy",
            signal_price=exit_price, would_fill_price=exit_price,
            size=abs(vp.size), estimated_pnl=result["net_pnl"],
            fill_model="mm_sim_exit", fee=exit_fee, funding=result["funding"],
        )
        self._virtual_positions.pop(self.cfg.symbol, None)
        log.info(
            f"[mm exit] raw={result['raw_pnl']:.4f} fee={result['total_fee']:.4f} "
            f"funding={result['funding']:.4f} net={result['net_pnl']:.4f}"
        )

    def _seed_funding_gate(self):
        """起動時に直近90日のfunding履歴を FundingGate に一括投入する

        失敗しても起動は続行する（min_samples未満なら素通しなので安全）。
        """
        try:
            lookback_ms = self.cfg.funding_gate.lookback_hours * 3600 * 1000
            start_ms = int(time.time() * 1000) - lookback_ms
            history = fetch_funding_history(self.cfg.symbol, start_ms)
            rates = [r for _, r in history]
            self.funding_gate.seed(rates)
            # 初回fetch(最大1時間後)までゲートが無効化されないよう最新履歴値をキャッシュ
            if rates:
                self._current_funding_rate_1h = rates[-1]
            log.info(
                f"[funding_gate] seeded {len(rates)} funding samples "
                f"(threshold ready: {self.funding_gate.threshold is not None})"
            )
        except Exception as e:
            log.warning(f"[funding_gate] seed failed (continuing warmup): {e}")

    async def _funding_loop(self):
        """1時間ごとに funding rate を取得し、open中の仮想ポジションに適用

        funding/OIの時系列記録（funding_oiテーブル）はコンテナ間の重複を避けるため
        シンボルごとに代表1コンテナ（rsi30）だけが行う。
        """
        interval = self.cfg.fees.funding_fetch_interval_sec
        while self._running:
            await asyncio.sleep(interval)
            ctx = self.hl.get_asset_ctx(self.cfg.symbol)
            if ctx is None:
                continue
            rate = ctx["funding"]
            if self.cfg.strategy == "rsi30":
                try:
                    await self.db.insert_funding_oi(
                        symbol=self.cfg.symbol,
                        funding_rate_1h=rate,
                        open_interest=ctx["open_interest"],
                        mark_price=ctx["mark_price"],
                    )
                except Exception as e:
                    log.error(f"Failed to record funding/OI: {e}")
            if rate is None:
                continue
            self._current_funding_rate_1h = rate
            if self.funding_gate is not None:
                self.funding_gate.update(rate)
            if self.cfg.mode == "dry" and self.cfg.fees.funding_enabled:
                vp = self._virtual_positions.get(self.cfg.symbol)
                if vp is None:
                    continue
                current = self.candle_5m.current
                if not current:
                    continue
                delta = apply_funding(vp, current.close, rate)
                self._virtual_funding_total += delta
                log.info(
                    f"[funding] rate={rate*100:.4f}% applied to {vp.side} "
                    f"notional={abs(vp.size)*current.close:.2f} delta={delta:.4f}"
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

            if self.cfg.mode == "dry":
                summary = await self.db.get_shadow_daily_summary(self.strategy.name)
            else:
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
        "--strategy", choices=list(STRATEGY_ENTRY_TF.keys()),
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
