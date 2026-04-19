"""Session Breakout 戦略 — UTC 0:00-8:00 レンジ / 8:00以降ブレイクで順張り"""

from datetime import datetime, timezone

from src.strategies.base import BaseStrategy, Signal, SignalType
from src.data.candle_builder import Candle
from src.config import SessionBOConfig
from src.utils.logger import get_logger

log = get_logger("session_bo")


class SessionBreakoutStrategy(BaseStrategy):
    """UTC 0:00〜8:00 のレンジをブレイクした方向に順張り。1日1回エントリー。

    エントリー条件（買い）:
      1. UTC 0〜8時のレンジが確定している
      2. UTC 8時以降、5分足の終値がレンジ高値を上抜け
      3. その日まだエントリーしていない

    決済条件:
      - 損切り: レンジ反対側
      - 利確1: エントリー + レンジ幅 × 1（半分決済）
      - 利確2: エントリー + レンジ幅 × 2（残り決済）
      - 強制決済: UTC 23:59（日またぎ禁止）
    """

    def __init__(self, symbol: str, mode: str, config: SessionBOConfig | None = None):
        super().__init__(symbol, mode)
        self.cfg = config or SessionBOConfig()

        self._current_day: int = -1
        self._range_high: float = 0.0
        self._range_low: float = 0.0
        self._range_ready: bool = False
        self._entered_today: bool = False

        self._has_position: bool = False
        self._position_side: str = ""
        self._entry_price: float = 0.0
        self._stop_loss: float = 0.0
        self._take_profit_1: float = 0.0
        self._take_profit_2: float = 0.0
        self._tp1_hit: bool = False
        self._remaining_ratio: float = 1.0

    @property
    def name(self) -> str:
        return "session_bo"

    def ready(self) -> bool:
        return True

    def _utc_day(self, timestamp: float) -> int:
        return int(timestamp // 86400)

    def _utc_hour(self, timestamp: float) -> int:
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return dt.hour

    def _reset_day(self, day: int):
        self._current_day = day
        self._range_high = 0.0
        self._range_low = float("inf")
        self._range_ready = False
        self._entered_today = False
        log.info(f"{self.symbol}: new UTC day {day}, range reset")

    def on_trade(self, price: float, size: float, timestamp: float):
        if not self._has_position:
            return

        if self._position_side == "buy":
            if price <= self._stop_loss:
                self._close_position("stop_loss", price)
            elif not self._tp1_hit and price >= self._take_profit_1:
                self._partial_close("take_profit_1", self._take_profit_1)
            elif self._tp1_hit and price >= self._take_profit_2:
                self._close_position("take_profit_2", price)
        elif self._position_side == "sell":
            if price >= self._stop_loss:
                self._close_position("stop_loss", price)
            elif not self._tp1_hit and price <= self._take_profit_1:
                self._partial_close("take_profit_1", self._take_profit_1)
            elif self._tp1_hit and price <= self._take_profit_2:
                self._close_position("take_profit_2", price)

    def on_candle(self, candle: Candle) -> Signal:
        """5分足確定時"""
        day = self._utc_day(candle.timestamp)
        hour = self._utc_hour(candle.timestamp)

        if day != self._current_day:
            if self._has_position:
                self._force_close(candle.close)
            self._reset_day(day)

        if self.cfg.range_start_hour_utc <= hour < self.cfg.range_end_hour_utc:
            if candle.high > self._range_high:
                self._range_high = candle.high
            if candle.low < self._range_low:
                self._range_low = candle.low
            return Signal(type=SignalType.NONE)

        if hour >= self.cfg.range_end_hour_utc and not self._range_ready:
            if self._range_high > 0 and self._range_low < float("inf"):
                self._range_ready = True
                log.info(
                    f"{self.symbol}: range ready "
                    f"high={self._range_high:.2f} low={self._range_low:.2f} "
                    f"width={self._range_high - self._range_low:.2f}"
                )

        if hour >= self.cfg.session_end_hour_utc and self._has_position:
            return Signal(type=SignalType.NONE)

        if self._has_position or self._entered_today or not self._range_ready:
            return Signal(type=SignalType.NONE)

        if hour < self.cfg.range_end_hour_utc:
            return Signal(type=SignalType.NONE)

        if candle.close > self._range_high:
            return self._create_signal(candle, "buy")

        if candle.close < self._range_low:
            return self._create_signal(candle, "sell")

        return Signal(type=SignalType.NONE)

    def _create_signal(self, candle: Candle, side: str) -> Signal:
        range_width = self._range_high - self._range_low
        entry = candle.close

        if side == "buy":
            stop_loss = self._range_low
            tp1 = entry + range_width * self.cfg.tp1_range_mult
            tp2 = entry + range_width * self.cfg.tp2_range_mult
            signal_type = SignalType.BUY
        else:
            stop_loss = self._range_high
            tp1 = entry - range_width * self.cfg.tp1_range_mult
            tp2 = entry - range_width * self.cfg.tp2_range_mult
            signal_type = SignalType.SELL

        self._has_position = True
        self._position_side = side
        self._entry_price = entry
        self._stop_loss = stop_loss
        self._take_profit_1 = tp1
        self._take_profit_2 = tp2
        self._tp1_hit = False
        self._remaining_ratio = 1.0
        self._entered_today = True

        log.info(
            f"{side.upper()} signal: {self.symbol} @ {entry:.2f} "
            f"range=[{self._range_low:.2f}, {self._range_high:.2f}] "
            f"SL={stop_loss:.2f} TP1={tp1:.2f} TP2={tp2:.2f}"
        )

        return Signal(
            type=signal_type,
            price=entry,
            size_usd=self.cfg.order_size_usd,
            stop_loss=stop_loss,
            take_profit=tp2,
            reason=f"Session breakout {side}: range_width={range_width:.2f}",
        )

    def _partial_close(self, reason: str, price: float):
        closed_ratio = self.cfg.tp1_close_ratio
        self._tp1_hit = True
        self._remaining_ratio = 1.0 - closed_ratio
        self._stop_loss = self._entry_price

        log.info(
            f"Partial close: {self._position_side} {reason} "
            f"ratio={closed_ratio} price={price:.2f} SL moved to entry={self._entry_price:.2f}"
        )

    def _close_position(self, reason: str, price: float):
        log.info(
            f"Position closed: {self._position_side} {reason} "
            f"entry={self._entry_price:.2f} exit={price:.2f}"
        )
        self._reset_position()

    def _force_close(self, price: float):
        log.info(
            f"Force close (day change): {self._position_side} "
            f"entry={self._entry_price:.2f} exit={price:.2f}"
        )
        self._reset_position()

    def _reset_position(self):
        self._has_position = False
        self._position_side = ""
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._take_profit_1 = 0.0
        self._take_profit_2 = 0.0
        self._tp1_hit = False
        self._remaining_ratio = 1.0

    def get_state(self) -> dict:
        return {
            "strategy": self.name,
            "symbol": self.symbol,
            "has_position": self._has_position,
            "position_side": self._position_side,
            "entry_price": self._entry_price,
            "stop_loss": self._stop_loss,
            "take_profit": self._take_profit_2,
            "take_profit_1": self._take_profit_1,
            "tp1_hit": self._tp1_hit,
            "range_high": self._range_high,
            "range_low": self._range_low if self._range_low != float("inf") else 0.0,
            "range_ready": self._range_ready,
            "entered_today": self._entered_today,
        }
