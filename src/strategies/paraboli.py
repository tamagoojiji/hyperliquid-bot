"""パラボリくん戦略（4時間足）

+2σバンドウォーク5本以上でロングセットアップarmed →
SAR（黄/赤の高い方）への押し目タッチ1回目 + EMA200フィルタでエントリー。
決済: タッチ4回目（touch4） / SAR黄の反転（sar_flip） / SL=entry−ATR×1.5。
ショートは鏡像（−2σバンドウォーク、SARが上、close < EMA200）。
"""

from src.strategies.base import BaseStrategy, Signal, SignalType
from src.indicators.sar import ParabolicSAR
from src.indicators.bollinger import BollingerBands
from src.indicators.ema import EMA
from src.indicators.atr import ATR
from src.data.candle_builder import Candle
from src.utils.logger import get_logger

log = get_logger("paraboli")


class ParaboliStrategy(BaseStrategy):
    SAR_Y = (0.02, 0.02, 0.2)      # SAR黄 (start, increment, max_af)
    SAR_R = (0.025, 0.025, 0.05)   # SAR赤
    BB_PERIOD, BB_MULT = 20, 2.0
    EMA_PERIOD = 200
    ATR_PERIOD = 14
    WALK_BARS = 5       # バンドウォーク成立本数
    EXIT_TOUCH = 4      # タッチ4回目で手仕舞い
    SL_ATR_MULT = 1.5

    def __init__(self, symbol: str, mode: str, config=None):
        super().__init__(symbol, mode)
        self.sar_y = ParabolicSAR(*self.SAR_Y)
        self.sar_r = ParabolicSAR(*self.SAR_R)
        self.bb = BollingerBands(self.BB_PERIOD, self.BB_MULT)
        self.ema = EMA(self.EMA_PERIOD)
        self.atr = ATR(self.ATR_PERIOD)
        self.order_size_usd = 10.0

        self._walk_up = 0
        self._walk_dn = 0
        self._armed: str = ""   # "" / "long" / "short"
        self._touch_count = 0

        self._has_position = False
        self._position_side: str = ""
        self._entry_price: float = 0.0
        self._stop_loss: float = 0.0
        self._take_profit: float = 0.0  # TPなし（touch4/sar_flipで決済）

    @property
    def name(self) -> str:
        return "paraboli"

    def ready(self) -> bool:
        return (
            self.sar_y.ready and self.sar_r.ready
            and self.bb.ready and self.ema.ready and self.atr.ready
        )

    def reset_position_state(self):
        self._has_position = False
        self._position_side = ""
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._take_profit = 0.0
        self._pending_exit = None
        self._armed = ""
        self._touch_count = 0

    def on_trade(self, price: float, size: float, timestamp: float):
        if not self._has_position:
            return
        # SLのみon_trade監視（stop-market想定=taker）。利確はon_candle側
        if self._position_side == "buy" and price <= self._stop_loss:
            self._emit_exit("buy", self._stop_loss, "stop_loss", is_maker=False)
            self._reset_position()
        elif self._position_side == "sell" and price >= self._stop_loss:
            self._emit_exit("sell", self._stop_loss, "stop_loss", is_maker=False)
            self._reset_position()

    def on_candle(self, candle: Candle) -> Signal:
        """4時間足確定時"""
        # 1本前の確定値（リペイント防止）
        prev_upper = self.bb.upper if self.bb.ready else None
        prev_lower = self.bb.lower if self.bb.ready else None
        prev_sar_y = self.sar_y.value if self.sar_y.ready else None
        prev_sar_r = self.sar_r.value if self.sar_r.ready else None
        prev_y_uptrend = self.sar_y.is_uptrend

        self.sar_y.update(candle.high, candle.low)
        self.sar_r.update(candle.high, candle.low)
        self.bb.update(candle.close)
        self.ema.update(candle.close)
        self.atr.update(candle.high, candle.low, candle.close)

        if not self.ready():
            return Signal(type=SignalType.NONE)

        y_flipped = prev_sar_y is not None and self.sar_y.is_uptrend != prev_y_uptrend

        # バンドウォークカウント（前回確定バンドとの比較。途切れたら0）
        if prev_upper is not None and candle.close > prev_upper:
            self._walk_up += 1
        else:
            self._walk_up = 0
        if prev_lower is not None and candle.close < prev_lower:
            self._walk_dn += 1
        else:
            self._walk_dn = 0

        # SARタッチ判定（1本前の確定SAR値を使用）
        touched = False
        if self._armed and prev_sar_y is not None and prev_sar_r is not None:
            if self._armed == "long":
                ref = max(prev_sar_y, prev_sar_r)
                touched = candle.low <= ref and candle.close > ref
            else:
                ref = min(prev_sar_y, prev_sar_r)
                touched = candle.high >= ref and candle.close < ref

        # 注: このSAR実装ではヒゲタッチ（low<=SAR, closeは上で回復）でも反転する。
        # closeが回復した反転は「タッチ」として扱い、反転リセット/決済は
        # タッチ不成立（closeがSARの向こう側で確定）の場合のみ発動する。

        # ── 保有中: 決済判定 ──
        if self._has_position:
            if touched:
                self._touch_count += 1
            if self._touch_count >= self.EXIT_TOUCH:
                self._exit_on_candle(candle.close, "touch4")
            elif y_flipped and not touched and (
                (self._position_side == "buy" and not self.sar_y.is_uptrend)
                or (self._position_side == "sell" and self.sar_y.is_uptrend)
            ):
                self._exit_on_candle(candle.close, "sar_flip")
            return Signal(type=SignalType.NONE)

        # SAR黄反転（タッチ不成立）でセットアップ/カウントをリセット
        if y_flipped and self._armed and not touched:
            log.info(f"{self.symbol}: SAR flip, {self._armed} setup reset")
            self._armed = ""
            self._touch_count = 0

        # バンドウォーク成立でarm
        if self._walk_up >= self.WALK_BARS and self._armed != "long":
            self._armed = "long"
            self._touch_count = 0
            log.info(f"{self.symbol}: long setup armed (band walk {self._walk_up} bars)")
        elif self._walk_dn >= self.WALK_BARS and self._armed != "short":
            self._armed = "short"
            self._touch_count = 0
            log.info(f"{self.symbol}: short setup armed (band walk {self._walk_dn} bars)")

        if not self._armed or not touched:
            return Signal(type=SignalType.NONE)

        self._touch_count += 1
        if self._touch_count != 1:
            return Signal(type=SignalType.NONE)

        # タッチ1回目 + EMA200フィルタでエントリー
        if self._armed == "long" and candle.close > self.ema.value:
            return self._create_signal("buy", candle.close)
        if self._armed == "short" and candle.close < self.ema.value:
            return self._create_signal("sell", candle.close)
        return Signal(type=SignalType.NONE)

    def _create_signal(self, side: str, entry: float) -> Signal:
        atr = self.atr.value
        if side == "buy":
            stop_loss = entry - atr * self.SL_ATR_MULT
            signal_type = SignalType.BUY
        else:
            stop_loss = entry + atr * self.SL_ATR_MULT
            signal_type = SignalType.SELL

        self._has_position = True
        self._position_side = side
        self._entry_price = entry
        self._stop_loss = stop_loss
        self._take_profit = 0.0

        log.info(
            f"{side.upper()} {self.symbol} @{entry:.4f} SL={stop_loss:.4f} "
            f"(SAR touch1, EMA200={self.ema.value:.4f})"
        )
        return Signal(
            type=signal_type,
            is_maker=True,
            price=entry,
            size_usd=self.order_size_usd,
            stop_loss=stop_loss,
            take_profit=0.0,
            reason=f"Paraboli {side}: band walk + SAR touch1",
        )

    def _exit_on_candle(self, price: float, reason: str):
        self._emit_exit(self._position_side, price, reason, is_maker=False)
        log.info(
            f"Position closed: {self._position_side} {reason} {self.symbol} "
            f"entry={self._entry_price:.4f} exit={price:.4f} touches={self._touch_count}"
        )
        self._reset_position()

    def _reset_position(self):
        self._has_position = False
        self._position_side = ""
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._take_profit = 0.0
        self._armed = ""
        self._touch_count = 0

    def get_state(self) -> dict:
        return {
            "strategy": self.name,
            "symbol": self.symbol,
            "has_position": self._has_position,
            "position_side": self._position_side,
            "entry_price": self._entry_price,
            "stop_loss": self._stop_loss,
            "take_profit": self._take_profit,
            "armed": self._armed,
            "touch_count": self._touch_count,
            "walk_up": self._walk_up,
            "walk_dn": self._walk_dn,
            "sar_y": self.sar_y.value if self.sar_y.ready else None,
            "sar_r": self.sar_r.value if self.sar_r.ready else None,
            "sar_y_uptrend": self.sar_y.is_uptrend,
            "bb_upper": self.bb.upper if self.bb.ready else None,
            "bb_lower": self.bb.lower if self.bb.ready else None,
            "ema200": self.ema.value if self.ema.ready else None,
            "atr": self.atr.value if self.atr.ready else None,
        }
