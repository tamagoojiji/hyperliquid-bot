"""窓埋めゲーム戦略（HL価格代用版 / 5分足）

CME清算値の代わりに毎日 05:00 JST (= 20:00 UTC) のHL価格を基準にする。
20:00〜22:00 UTC のウィンドウで基準から0.4%以上乖離したら基準方向へ逆張り、
TP=基準価格（窓埋め・maker指値）、SL=エントリーからさらに0.8%逆行（taker）。
22:00 UTC で強制クローズ。1日1エントリー。
"""

from src.strategies.base import BaseStrategy, Signal, SignalType
from src.data.candle_builder import Candle
from src.utils.logger import get_logger

log = get_logger("gap_fill")


class GapFillStrategy(BaseStrategy):
    BASE_SECONDS = 72000        # 20:00 UTC = 05:00 JST の足開始秒
    WINDOW_END_SECONDS = 79200  # 22:00 UTC
    GAP_THRESHOLD = 0.004       # 基準からの乖離 0.4%
    SL_PCT = 0.008              # さらに0.8%逆行で損切り

    def __init__(self, symbol: str, mode: str, config=None):
        super().__init__(symbol, mode)
        self.order_size_usd = 10.0

        self._current_day: int = -1
        self._base_price: float = 0.0
        self._entered_today: bool = False

        self._has_position: bool = False
        self._position_side: str = ""
        self._entry_price: float = 0.0
        self._stop_loss: float = 0.0
        self._take_profit: float = 0.0

    @property
    def name(self) -> str:
        return "gap_fill"

    def ready(self) -> bool:
        return True

    def reset_position_state(self):
        """幻ポジション破棄（_entered_todayは維持: 1日1回制限を守る）"""
        self._reset_position()
        self._pending_exit = None

    def on_trade(self, price: float, size: float, timestamp: float):
        if not self._has_position:
            return
        # TP=基準価格の指値(maker)、SLはstop-market(taker)想定
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
        """5分足確定時"""
        day = int(candle.timestamp // 86400)
        sod = int(candle.timestamp) % 86400  # UTC日内の経過秒

        if day != self._current_day:
            # 日替わり: 保険としてポジションが残っていれば強制クローズ
            if self._has_position:
                self._force_close(candle.close)
            self._current_day = day
            self._base_price = 0.0
            self._entered_today = False
            log.info(f"{self.symbol}: new UTC day {day}, gap state reset")

        # 20:00 UTC ちょうどの足のopenを基準価格として記録
        if sod == self.BASE_SECONDS:
            self._base_price = candle.open
            log.info(f"{self.symbol}: base price fixed @{candle.open:.4f} (20:00 UTC)")

        # 22:00 UTC 以降の最初の足でポジションがあれば強制クローズ
        if sod >= self.WINDOW_END_SECONDS:
            if self._has_position:
                self._force_close(candle.close)
            return Signal(type=SignalType.NONE)

        if (
            self._has_position
            or self._entered_today
            or self._base_price <= 0.0
            or sod < self.BASE_SECONDS
        ):
            return Signal(type=SignalType.NONE)

        # ウィンドウ内（20:00〜22:00 UTC）: 基準からの乖離で逆張り
        deviation = (candle.close - self._base_price) / self._base_price
        if deviation >= self.GAP_THRESHOLD:
            return self._create_signal("sell", candle.close, deviation)
        if deviation <= -self.GAP_THRESHOLD:
            return self._create_signal("buy", candle.close, deviation)
        return Signal(type=SignalType.NONE)

    def _create_signal(self, side: str, entry: float, deviation: float) -> Signal:
        if side == "sell":
            stop_loss = entry * (1.0 + self.SL_PCT)
            signal_type = SignalType.SELL
        else:
            stop_loss = entry * (1.0 - self.SL_PCT)
            signal_type = SignalType.BUY
        take_profit = self._base_price  # 窓埋め = 基準価格へ回帰

        self._has_position = True
        self._position_side = side
        self._entry_price = entry
        self._stop_loss = stop_loss
        self._take_profit = take_profit
        self._entered_today = True

        log.info(
            f"{side.upper()} {self.symbol} @{entry:.4f} SL={stop_loss:.4f} TP={take_profit:.4f} "
            f"(gap={deviation * 100:+.2f}% base={self._base_price:.4f})"
        )
        return Signal(
            type=signal_type,
            is_maker=False,
            price=entry,
            size_usd=self.order_size_usd,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reason=f"Gap fill {side}: deviation={deviation * 100:+.2f}% vs 20:00 UTC base",
        )

    def _force_close(self, price: float):
        self._emit_exit(self._position_side, price, "window_end", is_maker=False)
        log.info(
            f"Force close (window end): {self._position_side} {self.symbol} "
            f"entry={self._entry_price:.4f} exit={price:.4f}"
        )
        self._reset_position()

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
            "base_price": self._base_price,
            "entered_today": self._entered_today,
            "current_day": self._current_day,
        }
