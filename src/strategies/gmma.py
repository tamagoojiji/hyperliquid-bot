"""GMMA 戦略 — 短期EMA群（イワシ）と長期EMA群（クジラ）の完全分離 + EMA200プルバック（1時間足想定）

エントリー: 完全分離（min(短期群) > max(長期群)、前回確定値）中に足の安値がEMA200に
          タッチし、終値がEMA200の上で保たれたらロング（指値想定）。
          1分離サイクルにつき1回だけエントリー（分離が崩れたらフラグリセット）。
          ショートは鏡像
損切り    : entry ∓ ATR(14)×1.5（リアルタイム監視・taker）
決済      : 分離消滅で足確定クローズ（separation_lost・taker）。固定TPなし
"""

from src.strategies.base import BaseStrategy, Signal, SignalType
from src.indicators.ema import EMA
from src.indicators.atr import ATR
from src.data.candle_builder import Candle
from src.utils.logger import get_logger

log = get_logger("gmma")


class GMMAStrategy(BaseStrategy):
    SHORT_PERIODS = [10, 16, 20, 24, 30]
    LONG_PERIODS = [60, 70, 80, 90, 120, 144]
    ATR_SL_MULT = 1.5

    def __init__(self, symbol: str, mode: str, config=None):
        super().__init__(symbol, mode)

        self.short_emas = [EMA(period=p) for p in self.SHORT_PERIODS]
        self.long_emas = [EMA(period=p) for p in self.LONG_PERIODS]
        self.ema200 = EMA(period=200)
        self.atr = ATR(period=14)

        self.order_size_usd = 10.0

        self._has_position = False
        self._position_side: str = ""
        self._entry_price: float = 0.0
        self._stop_loss: float = 0.0
        self._take_profit: float = 0.0
        # 1分離サイクル1回エントリー用フラグ
        self._long_taken_this_cycle = False
        self._short_taken_this_cycle = False

    @property
    def name(self) -> str:
        return "gmma"

    def reset_position_state(self):
        super().reset_position_state()
        # 幻シグナルはサイクル消費もなかったことにする（恒久ブロック防止）
        self._long_taken_this_cycle = False
        self._short_taken_this_cycle = False

    def ready(self) -> bool:
        return (
            all(e.ready for e in self.short_emas)
            and all(e.ready for e in self.long_emas)
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
        # リペイント防止: タッチ・分離判定は「1本前の確定値」で行うため、更新前に保存
        was_ready = self.ready()
        prev_short = [e.value for e in self.short_emas] if was_ready else None
        prev_long = [e.value for e in self.long_emas] if was_ready else None
        prev_ema200 = self.ema200.value if was_ready else None

        for e in self.short_emas:
            e.update(candle.close)
        for e in self.long_emas:
            e.update(candle.close)
        self.ema200.update(candle.close)
        self.atr.update(candle.high, candle.low, candle.close)

        if not self.ready():
            return Signal(type=SignalType.NONE)

        # ── ポジション保有中: 分離消滅で決済（確定足の値で判定） ──
        if self._has_position:
            cur_short = [e.value for e in self.short_emas]
            cur_long = [e.value for e in self.long_emas]
            lost = (
                min(cur_short) <= max(cur_long)
                if self._position_side == "buy"
                else max(cur_short) >= min(cur_long)
            )
            if lost:
                log.info(
                    f"{self.symbol}: separation lost -> close {self._position_side} "
                    f"@{candle.close:.4f}"
                )
                self._emit_exit(
                    self._position_side, candle.close, "separation_lost", is_maker=False
                )
                self._close()
            return Signal(type=SignalType.NONE)

        if prev_short is None or prev_long is None or prev_ema200 is None:
            return Signal(type=SignalType.NONE)

        separation_up = min(prev_short) > max(prev_long)
        separation_down = max(prev_short) < min(prev_long)

        # 分離が崩れたらサイクルフラグをリセット
        if not separation_up:
            self._long_taken_this_cycle = False
        if not separation_down:
            self._short_taken_this_cycle = False

        sl_dist = self.atr.value * self.ATR_SL_MULT

        # ── ロング: 完全分離中のEMA200プルバック1回目タッチ ──
        if (
            separation_up
            and not self._long_taken_this_cycle
            and candle.low <= prev_ema200
            and candle.close > prev_ema200
        ):
            entry = candle.close
            sl = entry - sl_dist
            self._open("buy", entry, sl)
            self._long_taken_this_cycle = True
            log.info(
                f"BUY {self.symbol} @{entry:.4f} SL={sl:.4f} "
                f"(GMMA separation + EMA200 pullback, ATR={self.atr.value:.4f})"
            )
            return Signal(
                type=SignalType.BUY,
                is_maker=True,
                price=entry,
                size_usd=self.order_size_usd,
                stop_loss=sl,
                take_profit=0.0,
                reason="GMMA full separation up + EMA200 pullback touch",
            )

        # ── ショート: 鏡像 ──
        if (
            separation_down
            and not self._short_taken_this_cycle
            and candle.high >= prev_ema200
            and candle.close < prev_ema200
        ):
            entry = candle.close
            sl = entry + sl_dist
            self._open("sell", entry, sl)
            self._short_taken_this_cycle = True
            log.info(
                f"SELL {self.symbol} @{entry:.4f} SL={sl:.4f} "
                f"(GMMA separation + EMA200 pullback, ATR={self.atr.value:.4f})"
            )
            return Signal(
                type=SignalType.SELL,
                is_maker=True,
                price=entry,
                size_usd=self.order_size_usd,
                stop_loss=sl,
                take_profit=0.0,
                reason="GMMA full separation down + EMA200 pullback touch",
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
        r = self.ready()
        return {
            "strategy": self.name,
            "symbol": self.symbol,
            "has_position": self._has_position,
            "position_side": self._position_side,
            "entry_price": self._entry_price,
            "stop_loss": self._stop_loss,
            "take_profit": self._take_profit,
            "short_min": min(e.value for e in self.short_emas) if r else None,
            "short_max": max(e.value for e in self.short_emas) if r else None,
            "long_min": min(e.value for e in self.long_emas) if r else None,
            "long_max": max(e.value for e in self.long_emas) if r else None,
            "ema200": self.ema200.value if self.ema200.ready else None,
            "atr": self.atr.value if self.atr.ready else None,
        }
