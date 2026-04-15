class EMA:
    def __init__(self, period: int):
        self.period = period
        self.multiplier = 2.0 / (period + 1)
        self._value: float | None = None
        self._count = 0

    def update(self, price: float) -> None:
        self._count += 1
        if self._value is None:
            self._value = price
        else:
            self._value = (price - self._value) * self.multiplier + self._value

    @property
    def value(self) -> float | None:
        return self._value

    @property
    def ready(self) -> bool:
        return self._count >= self.period
