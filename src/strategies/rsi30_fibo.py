"""フィボナッチ + RSI 30-30 押し目買い強化戦略"""

from src.strategies.base import BaseStrategy, Signal, SignalType
from src.indicators.rsi_channel import RSIChannel
from src.indicators.bollinger import BollingerBands
from src.indicators.ema import EMA
from src.indicators.atr import ATR
from src.indicators.fibonacci import FibonacciRetracement
from src.data.candle_builder import Candle
from src.utils.logger import get_logger

log = get_logger("rsi30_fibo")


class RSI30FiboStrategy(BaseStrategy):
    """RSI 30-30 + フィボナッチフィルター

    既存RSI 30-30のエントリー条件 + フィボ38.2%〜61.8%ゾーンで重なった時のみエントリー
    → 回数は減るが勝率UP
    """

    def __init__(self, symbol: str, mode: str, config=None):
        super().__init__(symbol, mode)

        self.rsi = RSIChannel(period=14, ob_level=70, os_level=30)
        self.bb = BollingerBands(period=20, multiplier=2.0)
        self.ema = EMA(period=9)
        self.atr = ATR(period=14)
        self.fibo = FibonacciRetracement(swing_lookback=20)
        self.filter_ema = EMA(period=9)
        self._prev_filter_ema: float = 0.0

        self._prev_candle: Candle | None = None
        self._signal_armed_buy = False
        self._signal_armed_sell = False
        self._prev_low = 0.0
        self._prev_high = float("inf")

        self._has_position = False
        self._position_side = ""
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._take_profit = 0.0

        self.order_size_usd = 10.0
        self.atr_sl_multiplier = 1.5
        self.rr_ratio = 2.0
        self.max_daily_loss = 20.0
        self._daily_pnl = 0.0
        self._daily_day = -1

    @property
    def name(self) -> str:
        return "rsi30_fibo"

    def ready(self) -> bool:
        return (self.rsi.ready and self.bb.ready and self.ema.ready
                and self.atr.ready and self.fibo.ready and self.filter_ema.ready)

    def on_filter_candle(self, candle: Candle):
        self._prev_filter_ema = self.filter_ema.value if self.filter_ema.ready else 0.0
        self.filter_ema.update(candle.close)

    def on_trade(self, price: float, size: float, timestamp: float):
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
        self.rsi.update(candle.close)
        self.bb.update(candle.close)
        self.ema.update(candle.close)
        self.atr.update(candle.high, candle.low, candle.close)
        self.fibo.update(candle.high, candle.low, candle.close)

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

        # ── 買い: RSI Channel下バンド + フィボ38.2〜61.8ゾーン ──
        if prev.close <= self.rsi.os_price:
            self._signal_armed_buy = True
            self._prev_low = prev.low

        if self._signal_armed_buy:
            in_fibo = self.fibo.is_in_zone(candle.close, 0.382, 0.618)
            if candle.open <= self._prev_low:
                self._signal_armed_buy = False
            elif candle.close <= self.ema.value:
                pass
            elif not in_fibo:
                self._signal_armed_buy = False  # フィボゾーン外 → スキップ
            elif not self._filter_bullish():
                self._signal_armed_buy = False
            else:
                self._signal_armed_buy = False
                return self._create_buy_signal(candle)

        # ── 売り: RSI Channel上バンド + フィボ38.2〜61.8ゾーン ──
        if prev.close >= self.rsi.ob_price:
            self._signal_armed_sell = True
            self._prev_high = prev.high

        if self._signal_armed_sell:
            in_fibo = self.fibo.is_in_zone(candle.close, 0.382, 0.618)
            if candle.open >= self._prev_high:
                self._signal_armed_sell = False
            elif candle.close >= self.ema.value:
                pass
            elif not in_fibo:
                self._signal_armed_sell = False
            elif not self._filter_bearish():
                self._signal_armed_sell = False
            else:
                self._signal_armed_sell = False
                return self._create_sell_signal(candle)

        return Signal(type=SignalType.NONE)

    def _create_buy_signal(self, candle: Candle) -> Signal:
        atr_val = self.atr.value
        # 損切り: フィボ61.8%超え or ATR×1.5
        fibo_sl = self.fibo.get_level_price(0.618)
        atr_sl = candle.close - atr_val * self.atr_sl_multiplier
        stop_loss = min(fibo_sl, atr_sl) if fibo_sl > 0 else atr_sl

        # 利確: フィボ0%(直近高値) or RR2.0
        fibo_tp = self.fibo.get_level_price(0.0)
        rr_tp = candle.close + abs(candle.close - stop_loss) * self.rr_ratio
        take_profit = min(fibo_tp, rr_tp) if fibo_tp > 0 else rr_tp

        log.info(f"BUY: {self.symbol} @ {candle.close:.2f} SL={stop_loss:.2f} TP={take_profit:.2f} "
                 f"RSI={self.rsi.rsi_value:.1f} Fibo zone")

        self._has_position = True
        self._position_side = "buy"
        self._entry_price = candle.close
        self._stop_loss = stop_loss
        self._take_profit = take_profit

        return Signal(type=SignalType.BUY, price=candle.close, size_usd=self.order_size_usd,
                      stop_loss=stop_loss, take_profit=take_profit,
                      reason=f"RSI30+Fibo buy: RSI={self.rsi.rsi_value:.1f}")

    def _create_sell_signal(self, candle: Candle) -> Signal:
        atr_val = self.atr.value
        fibo_sl = self.fibo.get_level_price(0.382)
        atr_sl = candle.close + atr_val * self.atr_sl_multiplier
        stop_loss = max(fibo_sl, atr_sl) if fibo_sl > 0 else atr_sl

        fibo_tp = self.fibo.get_level_price(1.0)
        rr_tp = candle.close - abs(stop_loss - candle.close) * self.rr_ratio
        take_profit = max(fibo_tp, rr_tp) if fibo_tp > 0 else rr_tp

        log.info(f"SELL: {self.symbol} @ {candle.close:.2f} SL={stop_loss:.2f} TP={take_profit:.2f} "
                 f"RSI={self.rsi.rsi_value:.1f} Fibo zone")

        self._has_position = True
        self._position_side = "sell"
        self._entry_price = candle.close
        self._stop_loss = stop_loss
        self._take_profit = take_profit

        return Signal(type=SignalType.SELL, price=candle.close, size_usd=self.order_size_usd,
                      stop_loss=stop_loss, take_profit=take_profit,
                      reason=f"RSI30+Fibo sell: RSI={self.rsi.rsi_value:.1f}")

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
            "rsi": self.rsi.rsi_value if self.rsi.ready else None,
            "fibo_382": self.fibo.get_level_price(0.382) if self.fibo.ready else None,
            "fibo_618": self.fibo.get_level_price(0.618) if self.fibo.ready else None,
            "atr": self.atr.value if self.atr.ready else None,
        }
