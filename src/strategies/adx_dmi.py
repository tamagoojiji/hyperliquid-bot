"""ADX/DMI 戦略 — 魔法の杖「adx」NEO流（4時間足想定）

アーミング: -DI > +DI の状態を経る → armed_long（売りは鏡像）
発砲      : armed中に初めて -DI < ADX < +DI の並びになった足で成行ロング
          （EMA200フィルター: close > EMA200。1アーミングサイクル1回のみ）
損切り    : entry ∓ ATR(14)×1.5（リアルタイム監視・taker）
決済      : ADXが2本連続で低下（adx_turn）／逆方向DIクロス（di_cross）。固定TPなし
※ タッチ判定が無いため、全インジは確定足で更新後の値で判定する
"""

from collections import deque

from src.strategies.base import BaseStrategy, Signal, SignalType
from src.indicators.adx import ADX
from src.indicators.ema import EMA
from src.indicators.atr import ATR
from src.data.candle_builder import Candle
from src.utils.logger import get_logger

log = get_logger("adx_dmi")


class ADXDMIStrategy(BaseStrategy):
    ATR_SL_MULT = 1.5

    def __init__(self, symbol: str, mode: str, config=None):
        super().__init__(symbol, mode)

        self.adx = ADX(period=14)
        self.ema200 = EMA(period=200)
        self.atr = ATR(period=14)

        self.order_size_usd = 10.0

        # ADX 2本連続低下の検出用（直近3本の確定ADX）
        self._adx_hist: deque[float] = deque(maxlen=3)
        self._armed_long = False
        self._armed_short = False

        self._has_position = False
        self._position_side: str = ""
        self._entry_price: float = 0.0
        self._stop_loss: float = 0.0
        self._take_profit: float = 0.0

    @property
    def name(self) -> str:
        return "adx_dmi"

    def ready(self) -> bool:
        return self.adx.ready and self.ema200.ready and self.atr.ready

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
        self.adx.update(candle.high, candle.low, candle.close)
        self.ema200.update(candle.close)
        self.atr.update(candle.high, candle.low, candle.close)

        if not self.ready():
            return Signal(type=SignalType.NONE)

        adx = self.adx.value
        plus_di = self.adx.plus_di
        minus_di = self.adx.minus_di
        if adx is None or plus_di is None or minus_di is None:
            return Signal(type=SignalType.NONE)

        self._adx_hist.append(adx)

        # ── アーミング（毎足更新: 逆DI優勢の状態を経たら武装） ──
        if minus_di > plus_di:
            self._armed_long = True
        elif plus_di > minus_di:
            self._armed_short = True

        # ── 発砲判定（並びが初めて成立した足でアームを消費。1サイクル1回） ──
        fire_long = self._armed_long and (minus_di < adx < plus_di)
        if fire_long:
            self._armed_long = False
        fire_short = self._armed_short and (minus_di > adx > plus_di)
        if fire_short:
            self._armed_short = False

        # ── ポジション保有中: adx_turn / di_cross で決済 ──
        if self._has_position:
            adx_turn = (
                len(self._adx_hist) == 3
                and self._adx_hist[2] < self._adx_hist[1] < self._adx_hist[0]
            )
            di_cross = (
                minus_di > plus_di
                if self._position_side == "buy"
                else plus_di > minus_di
            )
            if adx_turn or di_cross:
                reason = "adx_turn" if adx_turn else "di_cross"
                log.info(
                    f"{self.symbol}: {reason} -> close {self._position_side} "
                    f"@{candle.close:.4f} (ADX={adx:.1f} +DI={plus_di:.1f} -DI={minus_di:.1f})"
                )
                self._emit_exit(self._position_side, candle.close, reason, is_maker=False)
                self._close()
            return Signal(type=SignalType.NONE)

        sl_dist = self.atr.value * self.ATR_SL_MULT

        # ── 買い発砲: -DI < ADX < +DI + EMA200の上（順張り成行） ──
        if fire_long and candle.close > self.ema200.value:
            entry = candle.close
            sl = entry - sl_dist
            self._open("buy", entry, sl)
            log.info(
                f"BUY {self.symbol} @{entry:.4f} SL={sl:.4f} "
                f"(ADX={adx:.1f} +DI={plus_di:.1f} -DI={minus_di:.1f})"
            )
            return Signal(
                type=SignalType.BUY,
                is_maker=False,
                price=entry,
                size_usd=self.order_size_usd,
                stop_loss=sl,
                take_profit=0.0,
                reason="DMI fire long (-DI < ADX < +DI) above EMA200",
            )

        # ── 売り発砲: 鏡像 ──
        if fire_short and candle.close < self.ema200.value:
            entry = candle.close
            sl = entry + sl_dist
            self._open("sell", entry, sl)
            log.info(
                f"SELL {self.symbol} @{entry:.4f} SL={sl:.4f} "
                f"(ADX={adx:.1f} +DI={plus_di:.1f} -DI={minus_di:.1f})"
            )
            return Signal(
                type=SignalType.SELL,
                is_maker=False,
                price=entry,
                size_usd=self.order_size_usd,
                stop_loss=sl,
                take_profit=0.0,
                reason="DMI fire short (-DI > ADX > +DI) below EMA200",
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
            "adx": self.adx.value if self.adx.ready else None,
            "plus_di": self.adx.plus_di,
            "minus_di": self.adx.minus_di,
            "armed_long": self._armed_long,
            "armed_short": self._armed_short,
            "ema200": self.ema200.value if self.ema200.ready else None,
            "atr": self.atr.value if self.atr.ready else None,
        }
