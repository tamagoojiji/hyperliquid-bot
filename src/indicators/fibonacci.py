from collections import deque


class FibonacciRetracement:
    """フィボナッチリトレースメント - 直近のスイングHigh/Lowから自動計算"""

    LEVELS = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)

    def __init__(self, swing_lookback: int = 20):
        self._swing_lookback = swing_lookback
        self._highs: deque[float] = deque(maxlen=200)
        self._lows: deque[float] = deque(maxlen=200)
        self._swing_high = 0.0
        self._swing_low = 0.0
        self.levels: dict[float, float] = {}
        self._count = 0

    def update(self, high: float, low: float, close: float) -> None:
        self._highs.append(high)
        self._lows.append(low)
        self._count += 1

        if self._count >= self._swing_lookback:
            recent_highs = list(self._highs)[-self._swing_lookback:]
            recent_lows = list(self._lows)[-self._swing_lookback:]
            self._swing_high = max(recent_highs)
            self._swing_low = min(recent_lows)
            self._calc_levels()

    def _calc_levels(self) -> None:
        diff = self._swing_high - self._swing_low
        if diff <= 0.0:
            self.levels = {}
            return
        self.levels = {
            level: self._swing_high - diff * level for level in self.LEVELS
        }

    def is_in_zone(self, price: float, level_low: float = 0.382, level_high: float = 0.618) -> bool:
        """価格がフィボの指定ゾーン内か"""
        if not self.levels:
            return False
        price_low = self.get_level_price(level_high)  # level_highの方が価格は低い
        price_high = self.get_level_price(level_low)
        return price_low <= price <= price_high

    @property
    def ready(self) -> bool:
        return len(self.levels) > 0

    def get_level_price(self, level: float) -> float:
        """指定レベル(0.382等)の価格を返す"""
        if not self.levels:
            return 0.0
        if level in self.levels:
            return self.levels[level]
        diff = self._swing_high - self._swing_low
        return self._swing_high - diff * level
