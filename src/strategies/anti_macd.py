"""Anti-MACD 戦略（リンダ・ラシュキ「Anti」セットアップ / 30分足）

ステートマシン（買い側。売り側は _dir による鏡像分岐）:
  S0: swing high/low切り下げ→買いセットアップ（切り上げ→売り）
  S1: GCかつmacd_lineが直近20本の最大値以上 → S2（A=直近20本最安値）
  S2: B=期間最高値。DC時にsignal傾き>0かつmacd傾き<0 → S3（不成立DCはS0へ）
  S3: C=期間最安値。次のGCで買いエントリー。30本経過でS0へ
決済: SL = C×0.999（taker） / TP = C + (B−A)×1.272（maker指値）
"""

from collections import deque

from src.strategies.base import BaseStrategy, Signal, SignalType
from src.indicators.macd import MACD
from src.indicators.atr import ATR
from src.data.candle_builder import Candle
from src.utils.logger import get_logger

log = get_logger("anti_macd")


class AntiMACDStrategy(BaseStrategy):
    MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
    ATR_PERIOD = 14
    SWING_WING = 2      # フラクタル左右本数
    LOOKBACK = 20       # macd最大値・A価格の参照本数
    S3_TIMEOUT = 30     # S3が30本続いたらS0へリセット
    SL_OFFSET = 0.001   # SL = C×(1∓0.001)
    TP_FIB = 1.272

    def __init__(self, symbol: str, mode: str, config=None):
        super().__init__(symbol, mode)
        self.macd = MACD(self.MACD_FAST, self.MACD_SLOW, self.MACD_SIGNAL)
        self.atr = ATR(self.ATR_PERIOD)
        self.order_size_usd = 10.0

        # スイング検出（左右SWING_WING本より高い高値/低い安値のフラクタル）
        self._win: deque[tuple[float, float]] = deque(maxlen=self.SWING_WING * 2 + 1)
        self._swing_highs: deque[float] = deque(maxlen=2)
        self._swing_lows: deque[float] = deque(maxlen=2)
        self._macd_vals: deque[float] = deque(maxlen=self.LOOKBACK)
        self._highs: deque[float] = deque(maxlen=self.LOOKBACK)
        self._lows: deque[float] = deque(maxlen=self.LOOKBACK)

        self._prev_signal_hist: float | None = None  # 2本前signal（S2の傾き判定用）

        self._state = 0
        self._dir = ""      # "buy" / "sell"
        self._a = 0.0       # 起点価格（買い: 直近20本最安値）
        self._b = 0.0       # S2中の極値（買い: 最高値）
        self._c = 0.0       # S3中の極値（買い: 最安値）
        self._s3_bars = 0

        self._has_position = False
        self._position_side: str = ""
        self._entry_price: float = 0.0
        self._stop_loss: float = 0.0
        self._take_profit: float = 0.0

    @property
    def name(self) -> str:
        return "anti_macd"

    def ready(self) -> bool:
        return (
            self.macd.ready
            and self.atr.ready
            and len(self._macd_vals) == self.LOOKBACK
        )

    def reset_position_state(self):
        self._has_position = False
        self._position_side = ""
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._take_profit = 0.0
        self._pending_exit = None
        self._reset_machine()

    def on_trade(self, price: float, size: float, timestamp: float):
        if not self._has_position:
            return
        # TPは指値(maker)、SLはstop-market(taker)想定
        side = self._position_side
        if side == "buy":
            if price <= self._stop_loss:
                self._emit_exit(side, self._stop_loss, "stop_loss", is_maker=False)
                self._close()
            elif price >= self._take_profit:
                self._emit_exit(side, self._take_profit, "take_profit", is_maker=True)
                self._close()
        elif side == "sell":
            if price >= self._stop_loss:
                self._emit_exit(side, self._stop_loss, "stop_loss", is_maker=False)
                self._close()
            elif price <= self._take_profit:
                self._emit_exit(side, self._take_profit, "take_profit", is_maker=True)
                self._close()

    def on_candle(self, candle: Candle) -> Signal:
        # 傾き判定用に更新前（=1本前・2本前の確定値）を保存
        prev_macd = self.macd.macd_line
        prev_signal = self.macd.signal_line
        prev2_signal = self._prev_signal_hist

        self.macd.update(candle.close)
        self._prev_signal_hist = prev_signal
        self.atr.update(candle.high, candle.low, candle.close)
        self._update_swings(candle)
        self._highs.append(candle.high)
        self._lows.append(candle.low)
        if self.macd.macd_line is not None:
            self._macd_vals.append(self.macd.macd_line)

        if not self.ready() or self._has_position:
            return Signal(type=SignalType.NONE)

        macd_now = self.macd.macd_line
        signal_now = self.macd.signal_line
        gc = self.macd.is_golden_cross()
        dc = self.macd.is_dead_cross()

        if self._state == 0:
            self._check_s0()
        elif self._state == 1:
            self._check_s1(candle, gc, dc, macd_now)
        elif self._state == 2:
            self._check_s2(candle, gc, dc, macd_now, prev_macd, prev_signal, prev2_signal)
        elif self._state == 3:
            return self._check_s3(candle, gc, dc)
        return Signal(type=SignalType.NONE)

    def _update_swings(self, candle: Candle):
        """フラクタル: 中央の足が左右SWING_WING本より高い高値/低い安値ならスイング確定"""
        self._win.append((candle.high, candle.low))
        if len(self._win) < self._win.maxlen:
            return
        mid = self.SWING_WING
        highs = [h for h, _ in self._win]
        lows = [l for _, l in self._win]
        if all(highs[mid] > highs[i] for i in range(len(highs)) if i != mid):
            self._swing_highs.append(highs[mid])
        if all(lows[mid] < lows[i] for i in range(len(lows)) if i != mid):
            self._swing_lows.append(lows[mid])

    def _check_s0(self):
        if len(self._swing_highs) < 2 or len(self._swing_lows) < 2:
            return
        sh, sl = self._swing_highs, self._swing_lows
        if sh[1] < sh[0] and sl[1] < sl[0]:
            self._state, self._dir = 1, "buy"
        elif sh[1] > sh[0] and sl[1] > sl[0]:
            self._state, self._dir = 1, "sell"
        if self._state == 1:
            log.info(f"{self.symbol}: S0->S1 trend confirmed ({self._dir} setup)")

    def _check_s1(self, candle: Candle, gc: bool, dc: bool, macd_now: float):
        # クロス時のmacd_lineが直近20本（当該足含む）の最大値以上 = 20本高値
        if self._dir == "buy" and gc and macd_now >= max(self._macd_vals):
            self._a = min(self._lows)
            self._b = candle.high
            self._state = 2
            log.info(f"{self.symbol}: S1->S2 GC at {self.LOOKBACK}-bar MACD high, A={self._a:.4f}")
        elif self._dir == "sell" and dc and macd_now <= min(self._macd_vals):
            self._a = max(self._highs)
            self._b = candle.low
            self._state = 2
            log.info(f"{self.symbol}: S1->S2 DC at {self.LOOKBACK}-bar MACD low, A={self._a:.4f}")

    def _check_s2(self, candle, gc, dc, macd_now, prev_macd, prev_signal, prev2_signal):
        # signal傾きはクロス前の足で判定する（EMA型signalはクロス当日に必ず
        # macd側へ折れるため「DC時にsignal上向き」は当日値では恒久不成立）。
        if prev_macd is None or prev_signal is None or prev2_signal is None:
            return
        signal_slope = prev_signal - prev2_signal
        macd_slope = macd_now - prev_macd
        if self._dir == "buy":
            self._b = max(self._b, candle.high)
            if dc:
                if signal_slope > 0 and macd_slope < 0:
                    self._state, self._c, self._s3_bars = 3, candle.low, 0
                    log.info(f"{self.symbol}: S2->S3 pullback DC, B={self._b:.4f}")
                else:
                    self._reset_machine()
        else:
            self._b = min(self._b, candle.low)
            if gc:
                if signal_slope < 0 and macd_slope > 0:
                    self._state, self._c, self._s3_bars = 3, candle.high, 0
                    log.info(f"{self.symbol}: S2->S3 pullback GC, B={self._b:.4f}")
                else:
                    self._reset_machine()

    def _check_s3(self, candle: Candle, gc: bool, dc: bool) -> Signal:
        self._s3_bars += 1
        if self._dir == "buy":
            self._c = min(self._c, candle.low)
            if gc:
                return self._enter(candle)
        else:
            self._c = max(self._c, candle.high)
            if dc:
                return self._enter(candle)
        if self._s3_bars >= self.S3_TIMEOUT:
            log.info(f"{self.symbol}: S3 timeout ({self.S3_TIMEOUT} bars), reset to S0")
            self._reset_machine()
        return Signal(type=SignalType.NONE)

    def _enter(self, candle: Candle) -> Signal:
        entry = candle.close
        side = self._dir
        if side == "buy":
            sl = self._c * (1.0 - self.SL_OFFSET)
            tp = self._c + (self._b - self._a) * self.TP_FIB
            stype = SignalType.BUY
            valid = sl < entry < tp
        else:
            sl = self._c * (1.0 + self.SL_OFFSET)
            tp = self._c - (self._a - self._b) * self.TP_FIB
            stype = SignalType.SELL
            valid = tp < entry < sl
        a, b, c = self._a, self._b, self._c
        self._reset_machine()  # エントリー後（不成立含む）はS0へ
        if not valid:
            log.info(f"{self.symbol}: Anti {side} skipped, invalid SL/TP "
                     f"(entry={entry:.4f} SL={sl:.4f} TP={tp:.4f})")
            return Signal(type=SignalType.NONE)
        self._has_position = True
        self._position_side = side
        self._entry_price = entry
        self._stop_loss = sl
        self._take_profit = tp
        log.info(
            f"{side.upper()} {self.symbol} @{entry:.4f} SL={sl:.4f} TP={tp:.4f} "
            f"(Anti A={a:.4f} B={b:.4f} C={c:.4f})"
        )
        return Signal(
            type=stype,
            is_maker=False,
            price=entry,
            size_usd=self.order_size_usd,
            stop_loss=sl,
            take_profit=tp,
            reason=f"Anti {side}: re-cross after pullback / TP=1.272 ext",
        )

    def _reset_machine(self):
        self._state = 0
        self._dir = ""
        self._a = self._b = self._c = 0.0
        self._s3_bars = 0

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
            "state": self._state,
            "direction": self._dir,
            "a": self._a,
            "b": self._b,
            "c": self._c,
            "s3_bars": self._s3_bars,
            "macd": self.macd.macd_line,
            "macd_signal": self.macd.signal_line,
            "atr": self.atr.value if self.atr.ready else None,
            "swing_highs": list(self._swing_highs),
            "swing_lows": list(self._swing_lows),
        }
