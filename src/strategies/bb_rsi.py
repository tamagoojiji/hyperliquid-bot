"""BB+RSIChannel 戦略

エントリー: BB下限タッチ→ロング / BB上限タッチ→ショート
損切り    : ロング→RSI=30 価格レベル(os_price) / ショート→RSI=70 価格レベル(ob_price)
利確      : リスク幅 × 2.0（RR=2固定。3回中1回勝てばイーブン設計）
"""

from src.strategies.base import BaseStrategy, ExitEvent, Signal, SignalType
from src.indicators.bollinger import BollingerBands
from src.indicators.rsi_channel import RSIChannel
from src.indicators.ema import EMA
from src.data.candle_builder import Candle
from src.utils.logger import get_logger

log = get_logger("bb_rsi")


class BBRSIStrategy(BaseStrategy):
    def __init__(self, symbol: str, mode: str, config=None):
        super().__init__(symbol, mode)

        self.bb = BollingerBands(period=20, multiplier=2.0)
        self.rsi = RSIChannel(period=14, ob_level=70.0, os_level=30.0, ema_smooth=1)
        self.trend_ema = EMA(period=200)
        self._prev_trend_ema: float = 0.0

        self.order_size_usd = 10.0
        self.rr_ratio = 2.0

        self._has_position = False
        self._position_side: str = ""
        self._entry_price: float = 0.0
        self._stop_loss: float = 0.0
        self._take_profit: float = 0.0

    @property
    def name(self) -> str:
        return "bb_rsi"

    def ready(self) -> bool:
        return self.bb.ready and self.rsi.ready and self.trend_ema.ready

    def on_trade(self, price: float, size: float, timestamp: float):
        if not self._has_position:
            return
        # SL/TPともRSI=30/70価格に指値で置く想定 → 両方とも maker fill
        side = self._position_side
        if side == "buy":
            if price <= self._stop_loss:
                self._emit_exit(side, self._stop_loss, "stop_loss", is_maker=True)
                self._close()
            elif price >= self._take_profit:
                self._emit_exit(side, self._take_profit, "take_profit", is_maker=True)
                self._close()
        elif side == "sell":
            if price >= self._stop_loss:
                self._emit_exit(side, self._stop_loss, "stop_loss", is_maker=True)
                self._close()
            elif price <= self._take_profit:
                self._emit_exit(side, self._take_profit, "take_profit", is_maker=True)
                self._close()

    def _emit_exit(self, side: str, price: float, reason: str, is_maker: bool):
        self._pending_exit = ExitEvent(
            side=side, exit_price=price, reason=reason, is_maker=is_maker
        )

    def _close(self):
        self._has_position = False
        self._position_side = ""
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._take_profit = 0.0

    def on_candle(self, candle: Candle) -> Signal:
        # 200EMA傾き判定のため、更新前の値を保存
        self._prev_trend_ema = self.trend_ema.value if self.trend_ema.ready else 0.0

        self.bb.update(candle.close)
        self.rsi.update(candle.close)
        self.trend_ema.update(candle.close)

        if not self.ready():
            return Signal(type=SignalType.NONE)
        if self._has_position:
            return Signal(type=SignalType.NONE)

        trend_up = self._prev_trend_ema > 0 and self.trend_ema.value > self._prev_trend_ema
        trend_down = self._prev_trend_ema > 0 and self.trend_ema.value < self._prev_trend_ema

        # ── 買い: 上昇トレンド中のみ、BB下限を足の安値が触れた ──
        if trend_up and candle.low <= self.bb.lower:
            entry = candle.close
            sl = self.rsi.os_price
            if sl is not None and sl < entry:
                risk = entry - sl
                tp = entry + risk * self.rr_ratio
                self._open("buy", entry, sl, tp)
                log.info(
                    f"BUY {self.symbol} @{entry:.4f} SL={sl:.4f} TP={tp:.4f} "
                    f"(risk={risk:.4f}, BB_lower={self.bb.lower:.4f})"
                )
                return Signal(
                    type=SignalType.BUY,
                    price=entry,
                    size_usd=self.order_size_usd,
                    stop_loss=sl,
                    take_profit=tp,
                    reason="BB lower touch + uptrend / SL=RSI30 price",
                )

        # ── 売り: 下降トレンド中のみ、BB上限を足の高値が触れた ──
        if trend_down and candle.high >= self.bb.upper:
            entry = candle.close
            sl = self.rsi.ob_price
            if sl is not None and sl > entry:
                risk = sl - entry
                tp = entry - risk * self.rr_ratio
                self._open("sell", entry, sl, tp)
                log.info(
                    f"SELL {self.symbol} @{entry:.4f} SL={sl:.4f} TP={tp:.4f} "
                    f"(risk={risk:.4f}, BB_upper={self.bb.upper:.4f})"
                )
                return Signal(
                    type=SignalType.SELL,
                    price=entry,
                    size_usd=self.order_size_usd,
                    stop_loss=sl,
                    take_profit=tp,
                    reason="BB upper touch + downtrend / SL=RSI70 price",
                )

        return Signal(type=SignalType.NONE)

    def _open(self, side: str, entry: float, sl: float, tp: float):
        self._has_position = True
        self._position_side = side
        self._entry_price = entry
        self._stop_loss = sl
        self._take_profit = tp

    def get_state(self) -> dict:
        return {
            "strategy": self.name,
            "symbol": self.symbol,
            "has_position": self._has_position,
            "position_side": self._position_side,
            "entry_price": self._entry_price,
            "stop_loss": self._stop_loss,
            "take_profit": self._take_profit,
            "bb_upper": self.bb.upper if self.bb.ready else None,
            "bb_lower": self.bb.lower if self.bb.ready else None,
            "rsi": self.rsi.rsi_value if self.rsi.ready else None,
            "rsi_ob_price": self.rsi.ob_price if self.rsi.ready else None,
            "rsi_os_price": self.rsi.os_price if self.rsi.ready else None,
        }
