"""金矛戦略（日足）

一目均衡表の遅行スパン×基準線クロス（金矛）でarm →
armed中に足の安値が当日基準線にタッチしたら基準線価格で指値約定とみなす。
SL = entry − ATR×1.5 / TP = entry + ATR×3.0（RR2）。
armedは30本経過 or 逆クロスで解除。保有中の逆金矛クロスで手仕舞い。ショートは鏡像。
"""

from src.strategies.base import BaseStrategy, Signal, SignalType
from src.indicators.ichimoku import Ichimoku
from src.indicators.atr import ATR
from src.data.candle_builder import Candle
from src.utils.logger import get_logger

log = get_logger("kinboko")


class KinbokoStrategy(BaseStrategy):
    KIJUN_PERIOD = 26
    LAG_PERIOD = 26
    ATR_PERIOD = 14
    ARM_TIMEOUT = 30    # armedが30本続いたら解除
    SL_ATR_MULT = 1.5
    TP_ATR_MULT = 3.0   # RR2

    def __init__(self, symbol: str, mode: str, config=None):
        super().__init__(symbol, mode)
        self.ichimoku = Ichimoku(self.KIJUN_PERIOD, self.LAG_PERIOD)
        self.atr = ATR(self.ATR_PERIOD)
        self.order_size_usd = 10.0

        self._pending: str = ""   # "" / "long" / "short"
        self._armed_bars = 0

        self._has_position = False
        self._position_side: str = ""
        self._entry_price: float = 0.0
        self._stop_loss: float = 0.0
        self._take_profit: float = 0.0

    @property
    def name(self) -> str:
        return "kinboko"

    def ready(self) -> bool:
        return self.ichimoku.ready and self.atr.ready

    def reset_position_state(self):
        self._has_position = False
        self._position_side = ""
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._take_profit = 0.0
        self._pending_exit = None
        self._pending = ""
        self._armed_bars = 0

    def on_trade(self, price: float, size: float, timestamp: float):
        if not self._has_position:
            return
        # TPは指値(maker)、SLはstop-market(taker)想定
        side = self._position_side
        if side == "buy":
            if price <= self._stop_loss:
                self._emit_exit(side, self._stop_loss, "stop_loss", is_maker=False)
                self._reset_position()
            elif price >= self._take_profit:
                self._emit_exit(side, self._take_profit, "take_profit", is_maker=True)
                self._reset_position()
        elif side == "sell":
            if price >= self._stop_loss:
                self._emit_exit(side, self._stop_loss, "stop_loss", is_maker=False)
                self._reset_position()
            elif price <= self._take_profit:
                self._emit_exit(side, self._take_profit, "take_profit", is_maker=True)
                self._reset_position()

    def on_candle(self, candle: Candle) -> Signal:
        """日足確定時"""
        self.ichimoku.update(candle.high, candle.low, candle.close)
        self.atr.update(candle.high, candle.low, candle.close)

        if not self.ready():
            return Signal(type=SignalType.NONE)

        bull = self.ichimoku.is_lag_bull_cross()
        bear = self.ichimoku.is_lag_bear_cross()

        # ── 保有中: 逆の金矛クロスで手仕舞い ──
        if self._has_position:
            if (self._position_side == "buy" and bear) or (
                self._position_side == "sell" and bull
            ):
                self._emit_exit(
                    self._position_side, candle.close, "kinboko_reverse", is_maker=False
                )
                log.info(
                    f"Kinboko reverse: close {self._position_side} {self.symbol} "
                    f"entry={self._entry_price:.4f} exit={candle.close:.4f}"
                )
                self._reset_position()
                # クローズした足のクロスは下のarm処理で逆方向セットアップになる
            else:
                return Signal(type=SignalType.NONE)

        # ── armed解除判定（逆クロス / 30本経過）──
        if self._pending:
            self._armed_bars += 1
            if (self._pending == "long" and bear) or (self._pending == "short" and bull):
                log.info(f"{self.symbol}: {self._pending} arm cancelled (reverse cross)")
                self._pending = ""
            elif self._armed_bars >= self.ARM_TIMEOUT:
                log.info(f"{self.symbol}: {self._pending} arm expired ({self.ARM_TIMEOUT} bars)")
                self._pending = ""

        # ── 金矛クロスでarm（タッチ判定はクロス確定の翌本以降）──
        if bull:
            self._pending = "long"
            self._armed_bars = 0
            log.info(f"{self.symbol}: kinboko bull cross, pending_long armed "
                     f"(kijun={self.ichimoku.kijun:.4f})")
            return Signal(type=SignalType.NONE)
        if bear:
            self._pending = "short"
            self._armed_bars = 0
            log.info(f"{self.symbol}: kinboko bear cross, pending_short armed "
                     f"(kijun={self.ichimoku.kijun:.4f})")
            return Signal(type=SignalType.NONE)

        if not self._pending:
            return Signal(type=SignalType.NONE)

        # ── armed中: 当日基準線タッチで指値約定とみなす ──
        kijun = self.ichimoku.kijun
        if self._pending == "long" and candle.low <= kijun:
            return self._create_signal("buy", kijun)
        if self._pending == "short" and candle.high >= kijun:
            return self._create_signal("sell", kijun)
        return Signal(type=SignalType.NONE)

    def _create_signal(self, side: str, entry: float) -> Signal:
        atr = self.atr.value
        if side == "buy":
            stop_loss = entry - atr * self.SL_ATR_MULT
            take_profit = entry + atr * self.TP_ATR_MULT
            signal_type = SignalType.BUY
        else:
            stop_loss = entry + atr * self.SL_ATR_MULT
            take_profit = entry - atr * self.TP_ATR_MULT
            signal_type = SignalType.SELL

        self._pending = ""
        self._armed_bars = 0
        self._has_position = True
        self._position_side = side
        self._entry_price = entry
        self._stop_loss = stop_loss
        self._take_profit = take_profit

        log.info(
            f"{side.upper()} {self.symbol} @{entry:.4f} SL={stop_loss:.4f} "
            f"TP={take_profit:.4f} (kijun touch after kinboko cross, RR2)"
        )
        return Signal(
            type=signal_type,
            is_maker=True,
            price=entry,
            size_usd=self.order_size_usd,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reason=f"Kinboko {side}: kijun touch after lag-span cross",
        )

    def _reset_position(self):
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
            "pending": self._pending,
            "armed_bars": self._armed_bars,
            "kijun": self.ichimoku.kijun,
            "atr": self.atr.value if self.atr.ready else None,
        }
