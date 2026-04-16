"""ピボット + VWAP ゾーントレード戦略"""

from src.strategies.base import BaseStrategy, Signal, SignalType
from src.indicators.pivot import PivotPoints
from src.indicators.vwap import VWAP
from src.indicators.ema import EMA
from src.indicators.atr import ATR
from src.data.candle_builder import Candle
from src.utils.logger import get_logger

log = get_logger("pivot_vwap")


class PivotVWAPStrategy(BaseStrategy):
    """ピボット + VWAP ゾーントレード

    ゾーン判定:
      価格 > P かつ > VWAP → 買いゾーン（ロングのみ）
      価格 < P かつ < VWAP → 売りゾーン（ショートのみ）
      それ以外 → 中立（エントリーしない）

    エントリー:
      買いゾーンで → S1やVWAPまで押したら買い
      売りゾーンで → R1やVWAPまで戻したら売り
    """

    def __init__(self, symbol: str, mode: str, config=None):
        super().__init__(symbol, mode)

        self.pivot = PivotPoints()
        self.vwap = VWAP()
        self.atr = ATR(period=14)
        self.filter_ema = EMA(period=9)
        self._prev_filter_ema: float = 0.0

        self._prev_candle: Candle | None = None
        self._has_position = False
        self._position_side = ""
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._take_profit = 0.0

        self.order_size_usd = 10.0
        self.max_daily_loss = 20.0
        self.proximity_pct = 0.002  # ライン近接判定 0.2%
        self._daily_pnl = 0.0
        self._daily_day = -1

    @property
    def name(self) -> str:
        return "pivot_vwap"

    def ready(self) -> bool:
        return self.pivot.ready and self.vwap.ready and self.atr.ready

    def on_filter_candle(self, candle: Candle):
        self._prev_filter_ema = self.filter_ema.value if self.filter_ema.ready else 0.0
        self.filter_ema.update(candle.close)

    def on_trade(self, price: float, size: float, timestamp: float):
        self.pivot.update(price, price, price, timestamp)
        if not self._has_position:
            return
        if self._position_side == "buy":
            if price <= self._stop_loss:
                self._close_position("stop_loss", price)
            elif price >= self._take_profit:
                self._close_position("take_profit", price)
        elif self._position_side == "sell":
            if price >= self._stop_loss:
                self._close_position("stop_loss", price)
            elif price <= self._take_profit:
                self._close_position("take_profit", price)

    def on_candle(self, candle: Candle) -> Signal:
        self.vwap.update(candle.high, candle.low, candle.close, candle.volume, candle.timestamp)
        self.atr.update(candle.high, candle.low, candle.close)
        self.pivot.update(candle.high, candle.low, candle.close, candle.timestamp)

        today = int(candle.timestamp // 86400)
        if today != self._daily_day:
            self._daily_pnl = 0.0
            self._daily_day = today

        if not self.ready():
            self._prev_candle = candle
            return Signal(type=SignalType.NONE)

        if self._has_position or self._daily_pnl <= -self.max_daily_loss:
            self._prev_candle = candle
            return Signal(type=SignalType.NONE)

        signal = self._check_signals(candle)
        self._prev_candle = candle
        return signal

    def _check_signals(self, candle: Candle) -> Signal:
        prev = self._prev_candle
        if prev is None:
            return Signal(type=SignalType.NONE)

        price = candle.close
        vwap = self.vwap.value
        pivot_p = self.pivot.p

        # ゾーン判定
        buy_zone = price > pivot_p and price > vwap
        sell_zone = price < pivot_p and price < vwap

        # ── 買いゾーン: S1 or VWAPまで押したら買い ──
        if buy_zone:
            # S1近接 or VWAP近接で押し目
            near_s1 = self._is_near(prev.low, self.pivot.s1)
            near_vwap = self._is_near(prev.low, vwap)

            if (near_s1 or near_vwap) and candle.open > prev.low:
                stop_loss = pivot_p  # Pを割ったらゾーン崩壊
                take_profit = self.pivot.r1
                log.info(f"BUY zone: {self.symbol} @ {price:.2f} P={pivot_p:.2f} VWAP={vwap:.2f}")
                return self._create_signal(candle, "buy", stop_loss, take_profit)

        # ── 売りゾーン: R1 or VWAPまで戻したら売り ──
        if sell_zone:
            near_r1 = self._is_near(prev.high, self.pivot.r1)
            near_vwap = self._is_near(prev.high, vwap)

            if (near_r1 or near_vwap) and candle.open < prev.high:
                stop_loss = pivot_p
                take_profit = self.pivot.s1
                log.info(f"SELL zone: {self.symbol} @ {price:.2f} P={pivot_p:.2f} VWAP={vwap:.2f}")
                return self._create_signal(candle, "sell", stop_loss, take_profit)

        return Signal(type=SignalType.NONE)

    def _is_near(self, price: float, line: float) -> bool:
        if line <= 0:
            return False
        return abs(price - line) / line < self.proximity_pct

    def _create_signal(self, candle, side, stop_loss, take_profit) -> Signal:
        self._has_position = True
        self._position_side = side
        self._entry_price = candle.close
        self._stop_loss = stop_loss
        self._take_profit = take_profit

        return Signal(
            type=SignalType.BUY if side == "buy" else SignalType.SELL,
            price=candle.close, size_usd=self.order_size_usd,
            stop_loss=stop_loss, take_profit=take_profit,
            reason=f"Pivot+VWAP zone {side}: P={self.pivot.p:.2f} VWAP={self.vwap.value:.2f}",
        )

    def _close_position(self, reason: str, price: float):
        pnl = 0.0
        if self._position_side == "buy":
            pnl = (price - self._entry_price) / self._entry_price * self.order_size_usd
        elif self._position_side == "sell":
            pnl = (self._entry_price - price) / self._entry_price * self.order_size_usd
        self._daily_pnl += pnl
        log.info(f"Closed: {self._position_side} {reason} pnl={pnl:.4f}")
        self._has_position = False
        self._position_side = ""
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._take_profit = 0.0

    def get_state(self) -> dict:
        price = self._prev_candle.close if self._prev_candle else 0
        vwap = self.vwap.value if self.vwap.ready else 0
        p = self.pivot.p
        zone = "neutral"
        if price > p and price > vwap:
            zone = "buy"
        elif price < p and price < vwap:
            zone = "sell"

        return {
            "strategy": self.name, "symbol": self.symbol, "zone": zone,
            "has_position": self._has_position, "position_side": self._position_side,
            "entry_price": self._entry_price,
            "stop_loss": self._stop_loss, "take_profit": self._take_profit,
            "pivot_p": p, "vwap": vwap,
            "atr": self.atr.value if self.atr.ready else None,
        }
