from .ema import EMA


class MACD:
    """MACD（Moving Average Convergence Divergence）"""

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self._fast_ema = EMA(fast)
        self._slow_ema = EMA(slow)
        self._signal_ema = EMA(signal)
        self._slow_period = slow
        self._signal_period = signal
        self._macd_line: float | None = None
        self._signal_line: float | None = None
        self._histogram: float | None = None
        self._prev_macd: float | None = None
        self._prev_signal: float | None = None
        self._count = 0

    def update(self, price: float) -> None:
        self._fast_ema.update(price)
        self._slow_ema.update(price)
        self._count += 1

        if self._fast_ema.ready and self._slow_ema.ready:
            self._prev_macd = self._macd_line
            self._prev_signal = self._signal_line

            self._macd_line = self._fast_ema.value - self._slow_ema.value
            self._signal_ema.update(self._macd_line)

            if self._signal_ema.ready:
                self._signal_line = self._signal_ema.value
                self._histogram = self._macd_line - self._signal_line

    @property
    def macd_line(self) -> float | None:
        return self._macd_line

    @property
    def signal_line(self) -> float | None:
        return self._signal_line

    @property
    def histogram(self) -> float | None:
        return self._histogram

    @property
    def ready(self) -> bool:
        return self._signal_line is not None

    @property
    def value(self) -> float | None:
        return self._macd_line

    def is_golden_cross(self) -> bool:
        """前回 macd < signal で今回 macd >= signal"""
        if (
            self._prev_macd is None
            or self._prev_signal is None
            or self._macd_line is None
            or self._signal_line is None
        ):
            return False
        return self._prev_macd < self._prev_signal and self._macd_line >= self._signal_line

    def is_dead_cross(self) -> bool:
        """前回 macd > signal で今回 macd <= signal"""
        if (
            self._prev_macd is None
            or self._prev_signal is None
            or self._macd_line is None
            or self._signal_line is None
        ):
            return False
        return self._prev_macd > self._prev_signal and self._macd_line <= self._signal_line
