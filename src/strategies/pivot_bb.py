"""ピボット + ボリバン ダブルフィルター戦略"""

from src.strategies.base import BaseStrategy, Signal, SignalType
from src.indicators.pivot import PivotPoints
from src.indicators.bollinger import BollingerBands
from src.indicators.ema import EMA
from src.indicators.atr import ATR
from src.data.candle_builder import Candle
from src.utils.logger import get_logger

log = get_logger("pivot_bb")


class PivotBBStrategy(BaseStrategy):
    """ピボット + ボリバン ダブルフィルター

    ピボットS1とBB下バンドが重なるゾーンでのみ買い（高精度逆張り）
    ピボットR1とBB上バンドが重なるゾーンでのみ売り
    """

    def __init__(self, symbol: str, mode: str, config=None):
        super().__init__(symbol, mode)

        self.pivot = PivotPoints()
        self.bb = BollingerBands(period=20, multiplier=2.0)
        self.ema = EMA(period=9)
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
        self.overlap_threshold_pct = 0.003  # ピボットとBBが0.3%以内で「重なり」判定
        self._daily_pnl = 0.0
        self._daily_day = -1

    @property
    def name(self) -> str:
        return "pivot_bb"

    def ready(self) -> bool:
        return self.pivot.ready and self.bb.ready and self.atr.ready and self.filter_ema.ready

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
        self.bb.update(candle.close)
        self.ema.update(candle.close)
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

        # ── 買い: ピボットS1 + BB下バンドが重なり + 反転確認 ──
        if (self._is_overlapping(self.pivot.s1, self.bb.lower)
                and self._is_near(price, self.pivot.s1)
                and candle.open > prev.low
                and self._filter_bullish()):

            stop_loss = max(self.pivot.s2, self.bb.lower - self.atr.value)
            take_profit_pivot = self.pivot.p
            take_profit_bb = self.bb.basis
            take_profit = min(take_profit_pivot, take_profit_bb)

            log.info(f"BUY: {self.symbol} @ {price:.2f} S1={self.pivot.s1:.2f} "
                     f"BB_lower={self.bb.lower:.2f} overlap")
            return self._create_signal(candle, "buy", stop_loss, take_profit)

        # ── 売り: ピボットR1 + BB上バンドが重なり + 反転確認 ──
        if (self._is_overlapping(self.pivot.r1, self.bb.upper)
                and self._is_near(price, self.pivot.r1)
                and candle.open < prev.high
                and self._filter_bearish()):

            stop_loss = min(self.pivot.r2, self.bb.upper + self.atr.value)
            take_profit_pivot = self.pivot.p
            take_profit_bb = self.bb.basis
            take_profit = max(take_profit_pivot, take_profit_bb)

            log.info(f"SELL: {self.symbol} @ {price:.2f} R1={self.pivot.r1:.2f} "
                     f"BB_upper={self.bb.upper:.2f} overlap")
            return self._create_signal(candle, "sell", stop_loss, take_profit)

        return Signal(type=SignalType.NONE)

    def _is_overlapping(self, pivot_line: float, bb_line: float) -> bool:
        """ピボットラインとBBラインが閾値以内で重なっているか"""
        if pivot_line <= 0 or bb_line <= 0:
            return False
        diff_pct = abs(pivot_line - bb_line) / pivot_line
        return diff_pct < self.overlap_threshold_pct

    def _is_near(self, price: float, line: float) -> bool:
        if line <= 0:
            return False
        return abs(price - line) / line < 0.002  # 0.2%以内

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
            reason=f"Pivot+BB {side}: S1/R1+BB overlap",
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

    def _filter_bullish(self) -> bool:
        if not self.filter_ema.ready or self._prev_filter_ema == 0:
            return False
        return self.filter_ema.value > self._prev_filter_ema

    def _filter_bearish(self) -> bool:
        if not self.filter_ema.ready or self._prev_filter_ema == 0:
            return False
        return self.filter_ema.value < self._prev_filter_ema

    def get_state(self) -> dict:
        return {
            "strategy": self.name, "symbol": self.symbol,
            "has_position": self._has_position, "position_side": self._position_side,
            "entry_price": self._entry_price,
            "stop_loss": self._stop_loss, "take_profit": self._take_profit,
            "pivot_s1": self.pivot.s1, "pivot_r1": self.pivot.r1,
            "bb_upper": self.bb.upper if self.bb.ready else None,
            "bb_lower": self.bb.lower if self.bb.ready else None,
            "atr": self.atr.value if self.atr.ready else None,
        }
