import math
from collections import deque


class BollingerBands:
    def __init__(self, period: int = 20, multiplier: float = 2.0):
        self.period = period
        self.multiplier = multiplier
        self._buf: deque[float] = deque(maxlen=period)

    def update(self, price: float) -> None:
        self._buf.append(price)

    @property
    def ready(self) -> bool:
        return len(self._buf) == self.period

    @property
    def basis(self) -> float | None:
        if not self.ready:
            return None
        return sum(self._buf) / self.period

    @property
    def upper(self) -> float | None:
        if not self.ready:
            return None
        b = self.basis
        return b + self.multiplier * self._std()

    @property
    def lower(self) -> float | None:
        if not self.ready:
            return None
        b = self.basis
        return b - self.multiplier * self._std()

    @property
    def value(self) -> float | None:
        return self.basis

    def _std(self) -> float:
        mean = sum(self._buf) / self.period
        variance = sum((x - mean) ** 2 for x in self._buf) / self.period
        return math.sqrt(variance)
