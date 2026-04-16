from src.strategies.base import BaseStrategy, Signal, SignalType
from src.indicators.pivot import PivotPoints
from src.indicators.ema import EMA
from src.indicators.rsi_channel import RSIChannel
from src.data.candle_builder import Candle
from src.config import RSI30Config
from src.utils.logger import get_logger

log = get_logger("pivot_bounce")


class PivotBounceStrategy(BaseStrategy):
    def __init__(self, symbol: str, mode: str, config=None):
        super().__init__(symbol, mode)
        self.cfg = config or RSI30Config()

        # インジケーター
        self.pivot = PivotPoints()
        self.rsi = RSIChannel(period=14, ob_level=70, os_level=30)
        self.ema = EMA(period=9)  # エントリー足のEMA
        self.filter_ema = EMA(period=9)  # 30分足フィルター
        self._prev_filter_ema: float = 0.0

        # 状態
        self._prev_candle: Candle | None = None
        self._signal_armed_buy: bool = False
        self._signal_armed_sell: bool = False
        self._armed_line_buy: str = ""
        self._armed_line_sell: str = ""

        # ポジション追跡
        self._has_position: bool = False
        self._position_side: str = ""
        self._entry_price: float = 0.0
        self._stop_loss: float = 0.0
        self._take_profit: float = 0.0
        self._entry_line: str = ""

        # リスク設定
        self.order_size_usd: float = 10.0
        self.max_daily_loss: float = 20.0
        self.rsi_buy_threshold: float = 35.0
        self.rsi_sell_threshold: float = 65.0
        self.line_proximity_pct: float = 0.001  # ラインの0.1%以内で「到達」判定

    @property
    def name(self) -> str:
        return "pivot_bounce"

    def ready(self) -> bool:
        return self.pivot.ready and self.rsi.ready and self.filter_ema.ready

    def on_filter_candle(self, candle: Candle):
        """30分足確定時"""
        self._prev_filter_ema = self.filter_ema.value if self.filter_ema.ready else 0.0
        self.filter_ema.update(candle.close)

    def on_trade(self, price: float, size: float, timestamp: float):
        """リアルタイムSL/TP監視"""
        # ピボット日足データ更新
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
        self.rsi.update(candle.close)
        self.ema.update(candle.close)
        self.pivot.update(candle.high, candle.low, candle.close, candle.timestamp)

        if not self.ready():
            self._prev_candle = candle
            return Signal(type=SignalType.NONE)

        if self._has_position:
            self._prev_candle = candle
            return Signal(type=SignalType.NONE)

        # 日次損失上限チェック
        if self.pivot.is_daily_limit_reached(self.max_daily_loss):
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
        rsi_val = self.rsi.rsi_value
        if rsi_val is None:
            return Signal(type=SignalType.NONE)

        # ── 買いシグナル ──
        # Step 1: 前足でS1付近に到達
        if self._is_near_line(prev.low, self.pivot.s1):
            line = "s1"
            if self.pivot.is_line_available(line) and self._filter_bullish():
                self._signal_armed_buy = True
                self._armed_line_buy = line
        elif self._is_near_line(prev.low, self.pivot.s2):
            line = "s2"
            if self.pivot.is_line_available(line) and self._filter_bullish():
                self._signal_armed_buy = True
                self._armed_line_buy = line

        if self._signal_armed_buy:
            # Step 2: RSI < 35
            if rsi_val >= self.rsi_buy_threshold:
                self._signal_armed_buy = False  # RSI条件未達
            # Step 3: 反転確認（始値 > 前足安値）
            elif candle.open <= prev.low:
                self._signal_armed_buy = False  # まだ下落中
            else:
                # 全条件クリア
                self._signal_armed_buy = False
                return self._create_buy_signal(candle, self._armed_line_buy)

        # ── 売りシグナル ──
        if self._is_near_line(prev.high, self.pivot.r1):
            line = "r1"
            if self.pivot.is_line_available(line) and self._filter_bearish():
                self._signal_armed_sell = True
                self._armed_line_sell = line
        elif self._is_near_line(prev.high, self.pivot.r2):
            line = "r2"
            if self.pivot.is_line_available(line) and self._filter_bearish():
                self._signal_armed_sell = True
                self._armed_line_sell = line

        if self._signal_armed_sell:
            if rsi_val <= self.rsi_sell_threshold:
                self._signal_armed_sell = False
            elif candle.open >= prev.high:
                self._signal_armed_sell = False
            else:
                self._signal_armed_sell = False
                return self._create_sell_signal(candle, self._armed_line_sell)

        return Signal(type=SignalType.NONE)

    def _is_near_line(self, price: float, line: float) -> bool:
        """価格がラインの近く（0.1%以内）か"""
        if line <= 0:
            return False
        return abs(price - line) / line < self.line_proximity_pct

    def _filter_bullish(self) -> bool:
        if not self.filter_ema.ready or self._prev_filter_ema == 0:
            return False
        return self.filter_ema.value > self._prev_filter_ema

    def _filter_bearish(self) -> bool:
        if not self.filter_ema.ready or self._prev_filter_ema == 0:
            return False
        return self.filter_ema.value < self._prev_filter_ema

    def _create_buy_signal(self, candle: Candle, line: str) -> Signal:
        stop_loss = self.pivot.next_line_below(line)
        take_profit = self.pivot.next_line_above(line)

        log.info(
            f"BUY signal: {self.symbol} @ {candle.close:.2f} "
            f"line={line} SL={stop_loss:.2f} TP={take_profit:.2f} "
            f"RSI={self.rsi.rsi_value:.1f}"
        )

        self._has_position = True
        self._position_side = "buy"
        self._entry_price = candle.close
        self._stop_loss = stop_loss
        self._take_profit = take_profit
        self._entry_line = line

        return Signal(
            type=SignalType.BUY,
            price=candle.close,
            size_usd=self.order_size_usd,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reason=f"Pivot bounce buy at {line}: RSI={self.rsi.rsi_value:.1f}",
        )

    def _create_sell_signal(self, candle: Candle, line: str) -> Signal:
        stop_loss = self.pivot.next_line_above(line)
        take_profit = self.pivot.next_line_below(line)

        log.info(
            f"SELL signal: {self.symbol} @ {candle.close:.2f} "
            f"line={line} SL={stop_loss:.2f} TP={take_profit:.2f} "
            f"RSI={self.rsi.rsi_value:.1f}"
        )

        self._has_position = True
        self._position_side = "sell"
        self._entry_price = candle.close
        self._stop_loss = stop_loss
        self._take_profit = take_profit
        self._entry_line = line

        return Signal(
            type=SignalType.SELL,
            price=candle.close,
            size_usd=self.order_size_usd,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reason=f"Pivot bounce sell at {line}: RSI={self.rsi.rsi_value:.1f}",
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
            "rsi": self.rsi.rsi_value if self.rsi.ready else None,
            "pivot": self.pivot.get_state() if self.pivot.ready else None,
        }
