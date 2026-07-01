"""ドンチャンブレイク + ATRトレーリングストップ（日足スイング戦略）

エントリー: 日足終値が過去20日高値を上抜け→ロング / 20日安値を下抜け→ショート
          （判定は当日足を含まない直前20日のチャネルに対して行う）
決済    : ATR(14) × 3 のトレーリングストップのみ（固定TPなし、伸びるだけ伸ばす）
          ロング: stop = max(stop, 日足終値 − ATR×3) を毎日切り上げ
          ショートは鏡像。ストップはリアルタイム価格で監視（taker決済想定）
"""

from src.strategies.base import BaseStrategy, Signal, SignalType
from src.indicators.donchian import DonchianChannel
from src.indicators.atr import ATR
from src.data.candle_builder import Candle
from src.config import DonchianConfig
from src.utils.logger import get_logger

log = get_logger("donchian")


class DonchianStrategy(BaseStrategy):
    def __init__(self, symbol: str, mode: str, config: DonchianConfig | None = None):
        super().__init__(symbol, mode)
        self.cfg = config or DonchianConfig()

        self.channel = DonchianChannel(period=self.cfg.channel_period)
        self.atr = ATR(period=self.cfg.atr_period)

        self._has_position = False
        self._position_side: str = ""
        self._entry_price: float = 0.0
        self._trail_stop: float = 0.0

    @property
    def name(self) -> str:
        return "donchian"

    def reset_position_state(self):
        super().reset_position_state()
        self._trail_stop = 0.0

    def ready(self) -> bool:
        return self.channel.ready and self.atr.ready

    def on_trade(self, price: float, size: float, timestamp: float):
        """リアルタイムでトレーリングストップを監視"""
        if not self._has_position:
            return
        if self._position_side == "buy" and price <= self._trail_stop:
            self._emit_exit("buy", self._trail_stop, "trailing_stop", is_maker=False)
            self._close_position()
        elif self._position_side == "sell" and price >= self._trail_stop:
            self._emit_exit("sell", self._trail_stop, "trailing_stop", is_maker=False)
            self._close_position()

    def on_candle(self, candle: Candle) -> Signal:
        """日足確定時: ブレイク判定 + トレーリングストップ更新"""
        # 当日足を含まない直前チャネルでブレイク判定する
        prev_upper = self.channel.upper
        prev_lower = self.channel.lower
        was_ready = self.ready()

        self.channel.update(candle.high, candle.low)
        self.atr.update(candle.high, candle.low, candle.close)

        if self._has_position:
            self._update_trail(candle.close)
            return Signal(type=SignalType.NONE)

        if not was_ready or prev_upper is None or prev_lower is None:
            return Signal(type=SignalType.NONE)

        if candle.close > prev_upper:
            return self._create_signal(candle, "buy")
        if candle.close < prev_lower:
            return self._create_signal(candle, "sell")
        return Signal(type=SignalType.NONE)

    def _update_trail(self, close: float):
        trail_dist = self.atr.value * self.cfg.atr_trail_multiplier
        if self._position_side == "buy":
            new_stop = close - trail_dist
            if new_stop > self._trail_stop:
                self._trail_stop = new_stop
                log.info(f"{self.symbol}: trail stop raised to {self._trail_stop:.2f}")
        else:
            new_stop = close + trail_dist
            if new_stop < self._trail_stop:
                self._trail_stop = new_stop
                log.info(f"{self.symbol}: trail stop lowered to {self._trail_stop:.2f}")

    def _create_signal(self, candle: Candle, side: str) -> Signal:
        entry = candle.close
        trail_dist = self.atr.value * self.cfg.atr_trail_multiplier
        stop = entry - trail_dist if side == "buy" else entry + trail_dist

        self._has_position = True
        self._position_side = side
        self._entry_price = entry
        self._trail_stop = stop

        log.info(
            f"{side.upper()} signal: {self.symbol} @ {entry:.2f} "
            f"channel=[{self.channel.lower:.2f}, {self.channel.upper:.2f}] "
            f"trail_stop={stop:.2f} ATR={self.atr.value:.2f}"
        )
        return Signal(
            type=SignalType.BUY if side == "buy" else SignalType.SELL,
            price=entry,
            size_usd=self.cfg.order_size_usd,
            stop_loss=stop,
            take_profit=0.0,  # 固定TPなし（トレーリングのみ）
            reason=f"Donchian {self.cfg.channel_period}d breakout {side}",
            is_maker=False,   # ブレイク方向への成行エントリー
        )

    def _close_position(self):
        log.info(
            f"Position closed: {self._position_side} trailing_stop "
            f"entry={self._entry_price:.2f} exit={self._trail_stop:.2f}"
        )
        self._has_position = False
        self._position_side = ""
        self._entry_price = 0.0
        self._trail_stop = 0.0

    def get_state(self) -> dict:
        return {
            "strategy": self.name,
            "symbol": self.symbol,
            "has_position": self._has_position,
            "position_side": self._position_side,
            "entry_price": self._entry_price,
            "stop_loss": self._trail_stop,
            "take_profit": 0.0,
            "channel_upper": self.channel.upper,
            "channel_lower": self.channel.lower,
            "atr": self.atr.value if self.atr.ready else None,
        }
