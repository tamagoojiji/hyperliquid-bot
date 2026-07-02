"""一目均衡表（金矛戦略用の最小実装: 基準線 + 遅行スパンクロス）

基準線 = (過去26本の最高値 + 最安値) / 2
遅行スパン = 終値を26本後方にプロット
「金矛」= 遅行スパンが基準線をクロス
  → 26本前の位置での比較: close[t] と 基準線[t-26] を比べる
"""

from collections import deque


class Ichimoku:
    def __init__(self, kijun_period: int = 26, lag_period: int = 26):
        self.kijun_period = kijun_period
        self.lag_period = lag_period
        self._highs: deque[float] = deque(maxlen=kijun_period)
        self._lows: deque[float] = deque(maxlen=kijun_period)
        # 遅行クロス判定用に基準線の履歴を保持（[t-26]を引くため+1）
        self._kijun_history: deque[float] = deque(maxlen=lag_period + 1)
        self._prev_close: float | None = None
        self._prev_lag_diff: float | None = None  # close - kijun[t-26] の前回値
        self._lag_diff: float | None = None

    def update(self, high: float, low: float, close: float) -> None:
        self._highs.append(high)
        self._lows.append(low)
        if len(self._highs) == self.kijun_period:
            kijun = (max(self._highs) + min(self._lows)) / 2.0
            self._kijun_history.append(kijun)

        # 遅行スパンクロス: close[t] vs 基準線[t - lag_period]
        if len(self._kijun_history) > self.lag_period:
            kijun_lagged = self._kijun_history[0]
            self._prev_lag_diff = self._lag_diff
            self._lag_diff = close - kijun_lagged
        self._prev_close = close

    @property
    def kijun(self) -> float | None:
        """現在の基準線"""
        return self._kijun_history[-1] if self._kijun_history else None

    @property
    def ready(self) -> bool:
        return self._lag_diff is not None and self._prev_lag_diff is not None

    def is_lag_bull_cross(self) -> bool:
        """金矛買い: 遅行スパンが基準線を上抜け"""
        if not self.ready:
            return False
        return self._prev_lag_diff <= 0 < self._lag_diff

    def is_lag_bear_cross(self) -> bool:
        """金矛売り: 遅行スパンが基準線を下抜け"""
        if not self.ready:
            return False
        return self._prev_lag_diff >= 0 > self._lag_diff
