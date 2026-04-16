from src.strategies.base import BaseStrategy, Signal, SignalType
from src.indicators.pivot import PivotPoints
from src.indicators.ema import EMA
from src.indicators.atr import ATR
from src.data.candle_builder import Candle
from src.utils.logger import get_logger
from collections import deque

log = get_logger("breakout")


class BreakoutStrategy(BaseStrategy):
    def __init__(self, symbol: str, mode: str, config=None):
        super().__init__(symbol, mode)

        # インジケーター
        self.pivot = PivotPoints()
        self.atr = ATR(period=14)
        self.filter_ema = EMA(period=9)  # 30分足フィルター
        self._prev_filter_ema: float = 0.0

        # 出来高追跡
        self._volume_buf: deque = deque(maxlen=100)
        self._volume_avg: float = 0.0

        # ATR追跡（ブレイクアウト判定用）
        self._atr_buf: deque = deque(maxlen=100)
        self._atr_avg: float = 0.0

        # ブレイクアウト検出
        self._prev_candle: Candle | None = None
        self._has_position: bool = False
        self._position_side: str = ""
        self._entry_price: float = 0.0
        self._stop_loss: float = 0.0
        self._take_profit: float = 0.0
        self._entry_line: str = ""

        # 設定
        self.order_size_usd: float = 10.0
        self.max_daily_loss: float = 20.0
        self.volume_spike_mult: float = 2.0
        self.atr_spike_mult: float = 2.0

    @property
    def name(self) -> str:
        return "breakout"

    def ready(self) -> bool:
        return self.pivot.ready and self.atr.ready and self.filter_ema.ready and self._atr_avg > 0

    def on_filter_candle(self, candle: Candle):
        """30分足確定時"""
        self._prev_filter_ema = self.filter_ema.value if self.filter_ema.ready else 0.0
        self.filter_ema.update(candle.close)

    def on_trade(self, price: float, size: float, timestamp: float):
        """リアルタイムSL/TP監視"""
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
        """5分足確定時のシグナル判定"""
        # インジケーター更新
        self.atr.update(candle.high, candle.low, candle.close)
        self.pivot.update(candle.high, candle.low, candle.close, candle.timestamp)

        # 出来高とATRのバッファ更新
        self._volume_buf.append(candle.volume)
        if self.atr.ready:
            self._atr_buf.append(self.atr.value)

        # 平均更新
        if len(self._volume_buf) >= 20:
            self._volume_avg = sum(self._volume_buf) / len(self._volume_buf)
        if len(self._atr_buf) >= 20:
            self._atr_avg = sum(self._atr_buf) / len(self._atr_buf)

        if not self.ready():
            self._prev_candle = candle
            return Signal(type=SignalType.NONE)

        if self._has_position:
            self._prev_candle = candle
            return Signal(type=SignalType.NONE)

        # 日次損失上限
        if self.pivot.is_daily_limit_reached(self.max_daily_loss):
            self._prev_candle = candle
            return Signal(type=SignalType.NONE)

        signal = self._check_breakout(candle)
        self._prev_candle = candle
        return signal

    def _check_breakout(self, candle: Candle) -> Signal:
        """ブレイクアウト判定"""
        prev = self._prev_candle
        if prev is None:
            return Signal(type=SignalType.NONE)

        # 出来高スパイク判定
        vol_spike = candle.volume > self._volume_avg * self.volume_spike_mult if self._volume_avg > 0 else False

        # ATRスパイク判定
        atr_spike = self.atr.value > self._atr_avg * self.atr_spike_mult if self._atr_avg > 0 else False

        if not (vol_spike and atr_spike):
            return Signal(type=SignalType.NONE)

        # === S1下抜けブレイクアウト（ショート）===
        if (prev.close > self.pivot.s1 and candle.close < self.pivot.s1
                and self.pivot.is_line_available("s1_break")
                and self._filter_bearish()):
            stop_loss = self.pivot.s1
            tp_by_line = self.pivot.s2
            tp_by_atr = candle.close - self.atr.value * 2.0
            take_profit = max(tp_by_line, tp_by_atr)
            return self._create_signal(candle, "sell", "s1_break", stop_loss, take_profit)

        # === R1上抜けブレイクアウト（ロング）===
        if (prev.close < self.pivot.r1 and candle.close > self.pivot.r1
                and self.pivot.is_line_available("r1_break")
                and self._filter_bullish()):
            stop_loss = self.pivot.r1
            tp_by_line = self.pivot.r2
            tp_by_atr = candle.close + self.atr.value * 2.0
            take_profit = min(tp_by_line, tp_by_atr)
            return self._create_signal(candle, "buy", "r1_break", stop_loss, take_profit)

        # === S2下抜けブレイクアウト（ショート）===
        if (prev.close > self.pivot.s2 and candle.close < self.pivot.s2
                and self.pivot.is_line_available("s2_break")
                and self._filter_bearish()):
            stop_loss = self.pivot.s2
            tp_by_line = self.pivot.s3
            tp_by_atr = candle.close - self.atr.value * 2.0
            take_profit = max(tp_by_line, tp_by_atr)
            return self._create_signal(candle, "sell", "s2_break", stop_loss, take_profit)

        # === R2上抜けブレイクアウト（ロング）===
        if (prev.close < self.pivot.r2 and candle.close > self.pivot.r2
                and self.pivot.is_line_available("r2_break")
                and self._filter_bullish()):
            stop_loss = self.pivot.r2
            tp_by_line = self.pivot.r3
            tp_by_atr = candle.close + self.atr.value * 2.0
            take_profit = min(tp_by_line, tp_by_atr)
            return self._create_signal(candle, "buy", "r2_break", stop_loss, take_profit)

        return Signal(type=SignalType.NONE)

    def _create_signal(self, candle: Candle, side: str, line: str,
                       stop_loss: float, take_profit: float) -> Signal:
        signal_type = SignalType.BUY if side == "buy" else SignalType.SELL

        log.info(
            f"{side.upper()} signal: {self.symbol} @ {candle.close:.2f} "
            f"line={line} SL={stop_loss:.2f} TP={take_profit:.2f} "
            f"ATR={self.atr.value:.2f} ATR_avg={self._atr_avg:.2f} "
            f"vol={candle.volume:.2f} vol_avg={self._volume_avg:.2f}"
        )

        self._has_position = True
        self._position_side = side
        self._entry_price = candle.close
        self._stop_loss = stop_loss
        self._take_profit = take_profit
        self._entry_line = line

        return Signal(
            type=signal_type,
            price=candle.close,
            size_usd=self.order_size_usd,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reason=f"Breakout {side} at {line}: ATR={self.atr.value:.2f} vol={candle.volume:.2f}",
        )

    def _close_position(self, reason: str, price: float):
        pnl = 0.0
        if self._position_side == "buy":
            pnl = (price - self._entry_price) / self._entry_price * self.order_size_usd
        elif self._position_side == "sell":
            pnl = (self._entry_price - price) / self._entry_price * self.order_size_usd

        if reason == "stop_loss":
            self.pivot.mark_line_used(self._entry_line)
            self.pivot.record_loss(abs(pnl))

        log.info(
            f"Position closed: {self._position_side} {reason} "
            f"entry={self._entry_price:.2f} exit={price:.2f} pnl={pnl:.4f} "
            f"line={self._entry_line}"
        )

        self._has_position = False
        self._position_side = ""
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._take_profit = 0.0
        self._entry_line = ""

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
            "strategy": self.name,
            "symbol": self.symbol,
            "has_position": self._has_position,
            "position_side": self._position_side,
            "entry_price": self._entry_price,
            "stop_loss": self._stop_loss,
            "take_profit": self._take_profit,
            "entry_line": self._entry_line,
            "atr": self.atr.value if self.atr.ready else None,
            "atr_avg": self._atr_avg,
            "volume_avg": self._volume_avg,
            "pivot": self.pivot.get_state() if self.pivot.ready else None,
        }
