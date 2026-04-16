"""MACD + VWAP トレンドフォロー戦略"""

from src.strategies.base import BaseStrategy, Signal, SignalType
from src.indicators.macd import MACD
from src.indicators.vwap import VWAP
from src.indicators.ema import EMA
from src.indicators.atr import ATR
from src.data.candle_builder import Candle
from src.utils.logger import get_logger

log = get_logger("macd_vwap")


class MACDVWAPStrategy(BaseStrategy):
    """MACD + VWAP トレンドフォロー

    エントリー（ロング）: MACDゴールデンクロス + 価格>VWAP + 30分EMA上向き
    エントリー（ショート）: MACDデッドクロス + 価格<VWAP + 30分EMA下向き
    損切り: VWAP割れ or ATR×1.5
    利確: MACDの反転クロス
    """

    def __init__(self, symbol: str, mode: str, config=None):
        super().__init__(symbol, mode)

        self.macd = MACD(fast=12, slow=26, signal=9)
        self.vwap = VWAP()
        self.atr = ATR(period=14)
        self.filter_ema = EMA(period=9)
        self._prev_filter_ema: float = 0.0

        self._prev_candle: Candle | None = None
        self._has_position = False
        self._position_side = ""
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._entry_vwap = 0.0

        self.order_size_usd = 10.0
        self.max_daily_loss = 20.0
        self.atr_sl_multiplier = 1.5
        self._daily_pnl = 0.0
        self._daily_day = -1

    @property
    def name(self) -> str:
        return "macd_vwap"

    def ready(self) -> bool:
        return self.macd.ready and self.vwap.ready and self.atr.ready and self.filter_ema.ready

    def on_filter_candle(self, candle: Candle):
        self._prev_filter_ema = self.filter_ema.value if self.filter_ema.ready else 0.0
        self.filter_ema.update(candle.close)

    def on_trade(self, price: float, size: float, timestamp: float):
        if not self._has_position:
            return

        # VWAP割れ損切り
        if self._position_side == "buy" and price < self._stop_loss:
            self._close_position("stop_loss", price)
        elif self._position_side == "sell" and price > self._stop_loss:
            self._close_position("stop_loss", price)

    def on_candle(self, candle: Candle) -> Signal:
        self.macd.update(candle.close)
        self.vwap.update(candle.high, candle.low, candle.close, candle.volume, candle.timestamp)
        self.atr.update(candle.high, candle.low, candle.close)

        # 日次リセット
        today = int(candle.timestamp // 86400)
        if today != self._daily_day:
            self._daily_pnl = 0.0
            self._daily_day = today

        if not self.ready():
            self._prev_candle = candle
            return Signal(type=SignalType.NONE)

        # 日次損失上限
        if self._daily_pnl <= -self.max_daily_loss:
            self._prev_candle = candle
            return Signal(type=SignalType.NONE)

        # ポジション持ち中 → MACD反転クロスで利確
        if self._has_position:
            if self._position_side == "buy" and self.macd.is_dead_cross():
                self._close_position("macd_cross", candle.close)
            elif self._position_side == "sell" and self.macd.is_golden_cross():
                self._close_position("macd_cross", candle.close)
            self._prev_candle = candle
            return Signal(type=SignalType.NONE)

        signal = self._check_signals(candle)
        self._prev_candle = candle
        return signal

    def _check_signals(self, candle: Candle) -> Signal:
        price = candle.close
        vwap = self.vwap.value

        # ロング: MACDゴールデンクロス + 価格>VWAP + 30分EMA上向き
        if (self.macd.is_golden_cross()
                and price > vwap
                and self._filter_bullish()):
            atr_sl = self.atr.value * self.atr_sl_multiplier
            stop_loss = max(vwap, price - atr_sl)  # VWAPかATR損切りの近い方
            return self._create_signal(candle, "buy", stop_loss)

        # ショート: MACDデッドクロス + 価格<VWAP + 30分EMA下向き
        if (self.macd.is_dead_cross()
                and price < vwap
                and self._filter_bearish()):
            atr_sl = self.atr.value * self.atr_sl_multiplier
            stop_loss = min(vwap, price + atr_sl)
            return self._create_signal(candle, "sell", stop_loss)

        return Signal(type=SignalType.NONE)

    def _create_signal(self, candle: Candle, side: str, stop_loss: float) -> Signal:
        # 利確はMACDクロスで動的決済なのでTP=0
        log.info(
            f"{side.upper()} signal: {self.symbol} @ {candle.close:.2f} "
            f"SL={stop_loss:.2f} MACD={self.macd.macd_line:.4f} VWAP={self.vwap.value:.2f}"
        )
        self._has_position = True
        self._position_side = side
        self._entry_price = candle.close
        self._stop_loss = stop_loss
        self._entry_vwap = self.vwap.value

        return Signal(
            type=SignalType.BUY if side == "buy" else SignalType.SELL,
            price=candle.close,
            size_usd=self.order_size_usd,
            stop_loss=stop_loss,
            take_profit=0.0,  # MACDクロスで動的決済
            reason=f"MACD+VWAP {side}: MACD={self.macd.macd_line:.4f}",
        )

    def _close_position(self, reason: str, price: float):
        pnl = 0.0
        if self._position_side == "buy":
            pnl = (price - self._entry_price) / self._entry_price * self.order_size_usd
        elif self._position_side == "sell":
            pnl = (self._entry_price - price) / self._entry_price * self.order_size_usd
        self._daily_pnl += pnl

        log.info(
            f"Position closed: {self._position_side} {reason} "
            f"entry={self._entry_price:.2f} exit={price:.2f} pnl={pnl:.4f}"
        )
        self._has_position = False
        self._position_side = ""
        self._entry_price = 0.0
        self._stop_loss = 0.0

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
            "entry_price": self._entry_price, "stop_loss": self._stop_loss,
            "take_profit": 0.0,
            "macd": self.macd.macd_line if self.macd.ready else None,
            "macd_signal": self.macd.signal_line if self.macd.ready else None,
            "vwap": self.vwap.value if self.vwap.ready else None,
            "atr": self.atr.value if self.atr.ready else None,
        }
