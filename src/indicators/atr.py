class ATR:
    def __init__(self, period: int = 14):
        self.period = period
        self._prev_close: float | None = None
        self._atr: float | None = None
        self._count = 0

    def update(self, high: float, low: float, close: float) -> None:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(
                high - low,
                abs(high - self._prev_close),
                abs(low - self._prev_close),
            )
        self._prev_close = close
        self._count += 1

        if self._atr is None:
            self._atr = tr
        else:
            # Wilder's smoothing
            self._atr = (self._atr * (self.period - 1) + tr) / self.period

    @property
    def value(self) -> float | None:
        return self._atr

    @property
    def ready(self) -> bool:
        return self._count >= self.period
