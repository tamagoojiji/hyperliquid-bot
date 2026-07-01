"""ドンチャンチャネル — 過去N本の最高値/最安値"""

from collections import deque


class DonchianChannel:
    def __init__(self, period: int = 20):
        self.period = period
        self._highs: deque[float] = deque(maxlen=period)
        self._lows: deque[float] = deque(maxlen=period)

    def update(self, high: float, low: float) -> None:
        self._highs.append(high)
        self._lows.append(low)

    @property
    def upper(self) -> float | None:
        return max(self._highs) if self._highs else None

    @property
    def lower(self) -> float | None:
        return min(self._lows) if self._lows else None

    @property
    def ready(self) -> bool:
        return len(self._highs) >= self.period
