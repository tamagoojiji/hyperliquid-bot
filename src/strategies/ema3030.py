"""EMA3030 戦略 — 魔法の杖「30-30」（30分足想定）

EMA7 / EMA30 / EMA200 の3本:
エントリー: 上昇配列（ema200 < ema30 < ema7、前回確定値）中に足の安値がEMA30に
          タッチし、終値がEMA30の上で保たれたらロング（指値想定）。ショートは鏡像
損切り    : entry ∓ ATR(14)×1.5（リアルタイム監視・taker）
決済      : 配列が崩れたら足確定でクローズ（alignment_break・taker）。固定TPなし
"""

from src.strategies.base import BaseStrategy, Signal, SignalType
from src.indicators.ema import EMA
from src.indicators.atr import ATR
from src.data.candle_builder import Candle
from src.utils.logger import get_logger

log = get_logger("ema3030")


class EMA3030Strategy(BaseStrategy):
    ATR_SL_MULT = 1.5

    def __init__(self, symbol: str, mode: str, config=None):
        super().__init__(symbol, mode)

        self.ema7 = EMA(period=7)
        self.ema30 = EMA(period=30)
        self.ema200 = EMA(period=200)
        self.atr = ATR(period=14)

        self.order_size_usd = 10.0

        self._has_position = False
        self._position_side: str = ""
        self._entry_price: float = 0.0
        self._stop_loss: float = 0.0
        self._take_profit: float = 0.0

    @property
    def name(self) -> str:
        return "ema3030"

    def ready(self) -> bool:
        return (
            self.ema7.ready
            and self.ema30.ready
            and self.ema200.ready
            and self.atr.ready
        )

    def on_trade(self, price: float, size: float, timestamp: float):
        """リアルタイムでSLを監視（taker決済想定）"""
        if not self._has_position:
            return
        if self._position_side == "buy" and price <= self._stop_loss:
            self._emit_exit("buy", self._stop_loss, "stop_loss", is_maker=False)
            self._close()
        elif self._position_side == "sell" and price >= self._stop_loss:
            self._emit_exit("sell", self._stop_loss, "stop_loss", is_maker=False)
            self._close()

    def on_candle(self, candle: Candle) -> Signal:
        # リペイント防止: タッチ判定は「1本前の確定値」で行うため、更新前に保存
        prev_ema7 = self.ema7.value if self.ema7.ready else None
        prev_ema30 = self.ema30.value if self.ema30.ready else None
        prev_ema200 = self.ema200.value if self.ema200.ready else None

        self.ema7.update(candle.close)
        self.ema30.update(candle.close)
        self.ema200.update(candle.close)
        self.atr.update(candle.high, candle.low, candle.close)

        if not self.ready():
            return Signal(type=SignalType.NONE)

        # ── ポジション保有中: 配列崩れで決済（確定足の値で判定） ──
        if self._has_position:
            e7, e30, e200 = self.ema7.value, self.ema30.value, self.ema200.value
            broken = (
                not (e200 < e30 < e7)
                if self._position_side == "buy"
                else not (e7 < e30 < e200)
            )
            if broken:
                log.info(
                    f"{self.symbol}: alignment break -> close {self._position_side} "
                    f"@{candle.close:.4f} (ema7={e7:.4f} ema30={e30:.4f} ema200={e200:.4f})"
                )
                self._emit_exit(
                    self._position_side, candle.close, "alignment_break", is_maker=False
                )
                self._close()
            return Signal(type=SignalType.NONE)

        if prev_ema7 is None or prev_ema30 is None or prev_ema200 is None:
            return Signal(type=SignalType.NONE)

        align_up = prev_ema200 < prev_ema30 < prev_ema7
        align_down = prev_ema7 < prev_ema30 < prev_ema200
        sl_dist = self.atr.value * self.ATR_SL_MULT

        # ── ロング: 上昇配列中の押し目タッチ（EMA30に触れて上で保った） ──
        if align_up and candle.low <= prev_ema30 and candle.close > prev_ema30:
            entry = candle.close
            sl = entry - sl_dist
            self._open("buy", entry, sl)
            log.info(
                f"BUY {self.symbol} @{entry:.4f} SL={sl:.4f} "
                f"(ema30 pullback, ATR={self.atr.value:.4f})"
            )
            return Signal(
                type=SignalType.BUY,
                is_maker=True,
                price=entry,
                size_usd=self.order_size_usd,
                stop_loss=sl,
                take_profit=0.0,
                reason="EMA30 pullback in bull alignment (7>30>200)",
            )

        # ── ショート: 下降配列中の戻りタッチ ──
        if align_down and candle.high >= prev_ema30 and candle.close < prev_ema30:
            entry = candle.close
            sl = entry + sl_dist
            self._open("sell", entry, sl)
            log.info(
                f"SELL {self.symbol} @{entry:.4f} SL={sl:.4f} "
                f"(ema30 pullback, ATR={self.atr.value:.4f})"
            )
            return Signal(
                type=SignalType.SELL,
                is_maker=True,
                price=entry,
                size_usd=self.order_size_usd,
                stop_loss=sl,
                take_profit=0.0,
                reason="EMA30 pullback in bear alignment (7<30<200)",
            )

        return Signal(type=SignalType.NONE)

    def _open(self, side: str, entry: float, sl: float):
        self._has_position = True
        self._position_side = side
        self._entry_price = entry
        self._stop_loss = sl
        self._take_profit = 0.0

    def _close(self):
        self._has_position = False
        self._position_side = ""
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._take_profit = 0.0

    def get_state(self) -> dict:
        return {
            "strategy": self.name,
            "symbol": self.symbol,
            "has_position": self._has_position,
            "position_side": self._position_side,
            "entry_price": self._entry_price,
            "stop_loss": self._stop_loss,
            "take_profit": self._take_profit,
            "ema7": self.ema7.value if self.ema7.ready else None,
            "ema30": self.ema30.value if self.ema30.ready else None,
            "ema200": self.ema200.value if self.ema200.ready else None,
            "atr": self.atr.value if self.atr.ready else None,
        }
