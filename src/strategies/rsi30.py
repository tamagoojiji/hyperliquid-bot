"""RSI 30-30 戦略 — RSI Channel + BB + EMA のミーンリバージョン"""

from collections import deque

from src.strategies.base import BaseStrategy, ExitEvent, Signal, SignalType
from src.indicators.rsi_channel import RSIChannel
from src.indicators.bollinger import BollingerBands
from src.indicators.ema import EMA
from src.indicators.atr import ATR
from src.data.candle_builder import Candle
from src.config import RSI30Config
from src.utils.logger import get_logger

log = get_logger("rsi30")


class RSI30Strategy(BaseStrategy):
    """RSI 30-30: 30分足シングルタイムフレーム・ミーンリバージョン戦略

    全インジケータを30分足で計算。
    エントリー条件（買い）:
      1. 価格がRSI Channel下バンド以下に到達
      2. 次の足で始値 > 前の足の安値（反転の兆候）
      3. その足の終値 > EMA 9
      4. BB下バンド付近
      5. 200EMAが上向き（方向フィルター）
    """

    def __init__(self, symbol: str, mode: str, config: RSI30Config | None = None):
        super().__init__(symbol, mode)
        self.cfg = config or RSI30Config()

        # 30分足インジケーター
        self.rsi = RSIChannel(
            period=self.cfg.rsi_period,
            ob_level=self.cfg.rsi_ob_level,
            os_level=self.cfg.rsi_os_level,
            ema_smooth=self.cfg.rsi_ema_smooth,
        )
        self.bb = BollingerBands(
            period=self.cfg.bb_period,
            multiplier=self.cfg.bb_multiplier,
        )
        self.ema = EMA(period=self.cfg.ema_period)
        self.atr = ATR(period=self.cfg.atr_period)

        # 方向フィルター用の200EMA（同じ30分足）
        self.trend_ema = EMA(period=self.cfg.trend_ema_period)
        self._prev_trend_ema: float = 0.0

        # 状態追跡
        self._prev_candle: Candle | None = None
        self._signal_armed_buy = False   # RSIバンド到達フラグ
        self._signal_armed_sell = False
        self._prev_low: float = 0.0
        self._prev_high: float = float("inf")

        # ポジション追跡（戦略内部用）
        self._has_position = False
        self._position_side: str = ""
        self._entry_price: float = 0.0
        self._stop_loss: float = 0.0
        self._take_profit: float = 0.0

    @property
    def name(self) -> str:
        return "rsi30"

    def ready(self) -> bool:
        return (
            self.rsi.ready
            and self.bb.ready
            and self.ema.ready
            and self.atr.ready
            and self.trend_ema.ready
        )

    def on_trade(self, price: float, size: float, timestamp: float):
        """リアルタイムトレードデータで利確/損切りチェック"""
        if not self._has_position:
            return

        side = self._position_side
        if side == "buy":
            if price <= self._stop_loss:
                self._emit_exit(side, self._stop_loss, "stop_loss", is_maker=False)
                self._close_position("stop_loss")
            elif price >= self._take_profit:
                self._emit_exit(side, self._take_profit, "take_profit", is_maker=True)
                self._close_position("take_profit")
        elif side == "sell":
            if price >= self._stop_loss:
                self._emit_exit(side, self._stop_loss, "stop_loss", is_maker=False)
                self._close_position("stop_loss")
            elif price <= self._take_profit:
                self._emit_exit(side, self._take_profit, "take_profit", is_maker=True)
                self._close_position("take_profit")

    def _emit_exit(self, side: str, price: float, reason: str, is_maker: bool):
        self._pending_exit = ExitEvent(
            side=side, exit_price=price, reason=reason, is_maker=is_maker
        )

    def on_candle(self, candle: Candle) -> Signal:
        """30分足キャンドル確定時にシグナル判定"""
        # 200EMA傾き判定用に更新前の値を保存
        self._prev_trend_ema = self.trend_ema.value if self.trend_ema.ready else 0.0

        # インジケーター更新（全て30分足）
        self.rsi.update(candle.close)
        self.bb.update(candle.close)
        self.ema.update(candle.close)
        self.atr.update(candle.high, candle.low, candle.close)
        self.trend_ema.update(candle.close)

        if not self.ready():
            self._prev_candle = candle
            return Signal(type=SignalType.NONE)

        # ポジション持ってたらエントリーしない
        if self._has_position:
            self._prev_candle = candle
            return Signal(type=SignalType.NONE)

        signal = self._check_signals(candle)
        self._prev_candle = candle
        return signal

    def _check_signals(self, candle: Candle) -> Signal:
        """エントリーシグナル判定"""
        prev = self._prev_candle
        if prev is None:
            return Signal(type=SignalType.NONE)

        # ── 買いシグナル ──
        # Step 1: 前の足で RSI Channel 下バンド以下に到達
        if prev.close <= self.rsi.os_price:
            self._signal_armed_buy = True
            self._prev_low = prev.low

        if self._signal_armed_buy:
            # Step 2: 現在の足の始値 > 前の足の安値（反転の兆候）
            if candle.open <= self._prev_low:
                self._signal_armed_buy = False
            # Step 3: 終値 > 9EMA（反転確認）
            elif candle.close <= self.ema.value:
                pass  # まだ条件未達、次の足で再チェック
            # Step 4: BB下バンド付近（終値がBB下バンド + BB幅の20%以内）
            elif candle.close > self.bb.lower + (self.bb.upper - self.bb.lower) * 0.2:
                self._signal_armed_buy = False
            # Step 5: 200EMAが上向き（方向フィルター）
            elif not self._filter_bullish():
                self._signal_armed_buy = False
            else:
                # 全条件クリア
                self._signal_armed_buy = False
                return self._create_buy_signal(candle)

        # ── 売りシグナル ──
        if prev.close >= self.rsi.ob_price:
            self._signal_armed_sell = True
            self._prev_high = prev.high

        if self._signal_armed_sell:
            if candle.open >= self._prev_high:
                self._signal_armed_sell = False
            elif candle.close >= self.ema.value:
                pass
            elif candle.close < self.bb.upper - (self.bb.upper - self.bb.lower) * 0.2:
                self._signal_armed_sell = False
            elif not self._filter_bearish():
                self._signal_armed_sell = False
            else:
                self._signal_armed_sell = False
                return self._create_sell_signal(candle)

        return Signal(type=SignalType.NONE)

    def _filter_bullish(self) -> bool:
        """200EMAが上向きか"""
        if not self.trend_ema.ready or self._prev_trend_ema == 0:
            return False
        return self.trend_ema.value > self._prev_trend_ema

    def _filter_bearish(self) -> bool:
        """200EMAが下向きか"""
        if not self.trend_ema.ready or self._prev_trend_ema == 0:
            return False
        return self.trend_ema.value < self._prev_trend_ema

    def _create_buy_signal(self, candle: Candle) -> Signal:
        """買いシグナル生成"""
        atr_val = self.atr.value
        sl_distance = atr_val * self.cfg.atr_sl_multiplier
        tp_distance = sl_distance * self.cfg.rr_ratio

        stop_loss = candle.close - sl_distance
        take_profit = candle.close + tp_distance

        # 直近スイング安値との比較（より広い方を採用）
        if self._prev_candle:
            swing_sl = self._prev_candle.low * 0.999  # 少し余裕
            if swing_sl < stop_loss:
                stop_loss = swing_sl
                tp_distance = (candle.close - stop_loss) * self.cfg.rr_ratio
                take_profit = candle.close + tp_distance

        log.info(
            f"BUY signal: {self.symbol} @ {candle.close:.2f} "
            f"SL={stop_loss:.2f} TP={take_profit:.2f} "
            f"ATR={atr_val:.2f} RSI={self.rsi.rsi_value:.1f}"
        )

        self._has_position = True
        self._position_side = "buy"
        self._entry_price = candle.close
        self._stop_loss = stop_loss
        self._take_profit = take_profit

        return Signal(
            type=SignalType.BUY,
            price=candle.close,
            size_usd=self.cfg.order_size_usd,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reason=f"RSI30 buy: RSI={self.rsi.rsi_value:.1f} BB_lower={self.bb.lower:.2f}",
        )

    def _create_sell_signal(self, candle: Candle) -> Signal:
        """売りシグナル生成"""
        atr_val = self.atr.value
        sl_distance = atr_val * self.cfg.atr_sl_multiplier
        tp_distance = sl_distance * self.cfg.rr_ratio

        stop_loss = candle.close + sl_distance
        take_profit = candle.close - tp_distance

        if self._prev_candle:
            swing_sl = self._prev_candle.high * 1.001
            if swing_sl > stop_loss:
                stop_loss = swing_sl
                tp_distance = (stop_loss - candle.close) * self.cfg.rr_ratio
                take_profit = candle.close - tp_distance

        log.info(
            f"SELL signal: {self.symbol} @ {candle.close:.2f} "
            f"SL={stop_loss:.2f} TP={take_profit:.2f} "
            f"ATR={atr_val:.2f} RSI={self.rsi.rsi_value:.1f}"
        )

        self._has_position = True
        self._position_side = "sell"
        self._entry_price = candle.close
        self._stop_loss = stop_loss
        self._take_profit = take_profit

        return Signal(
            type=SignalType.SELL,
            price=candle.close,
            size_usd=self.cfg.order_size_usd,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reason=f"RSI30 sell: RSI={self.rsi.rsi_value:.1f} BB_upper={self.bb.upper:.2f}",
        )

    def _close_position(self, reason: str):
        """ポジションクローズ"""
        log.info(f"Position closed: {self._position_side} {reason}")
        self._has_position = False
        self._position_side = ""
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._take_profit = 0.0

    def get_state(self) -> dict:
        """現在の状態を返す（Discord通知・ログ用）"""
        return {
            "strategy": self.name,
            "symbol": self.symbol,
            "has_position": self._has_position,
            "position_side": self._position_side,
            "entry_price": self._entry_price,
            "stop_loss": self._stop_loss,
            "take_profit": self._take_profit,
            "rsi": self.rsi.rsi_value if self.rsi.ready else None,
            "rsi_ob_price": self.rsi.ob_price if self.rsi.ready else None,
            "rsi_os_price": self.rsi.os_price if self.rsi.ready else None,
            "bb_upper": self.bb.upper if self.bb.ready else None,
            "bb_lower": self.bb.lower if self.bb.ready else None,
            "ema": self.ema.value if self.ema.ready else None,
            "trend_ema": self.trend_ema.value if self.trend_ema.ready else None,
            "atr": self.atr.value if self.atr.ready else None,
        }
