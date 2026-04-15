class RSIChannel:
    def __init__(
        self,
        period: int = 14,
        ob_level: float = 70.0,
        os_level: float = 30.0,
        ema_smooth: int = 1,
    ):
        self.period = period
        self.ob_level = ob_level
        self.os_level = os_level
        self.ema_smooth = ema_smooth
        self._ema_mult = 2.0 / (ema_smooth + 1)

        self._prev_close: float | None = None
        self._sumu: float = 0.0
        self._sumd: float = 0.0
        self._prev_sumu: float = 0.0
        self._prev_sumd: float = 0.0
        self._rsi: float | None = None
        self._ob_raw: float | None = None
        self._os_raw: float | None = None
        self._ob_ema: float | None = None
        self._os_ema: float | None = None
        self._count = 0

    def update(self, close: float) -> None:
        if self._prev_close is None:
            self._prev_close = close
            self._count += 1
            return

        # u / d
        u = max(close - self._prev_close, 0.0)
        d = max(self._prev_close - close, 0.0)

        # Wilder's smoothing (nz: prev value, default 0)
        prev_sumu = self._sumu  # sumu[1]
        prev_sumd = self._sumd  # sumd[1]

        self._sumu = (u + (self.period - 1) * prev_sumu) / self.period
        self._sumd = (d + (self.period - 1) * prev_sumd) / self.period

        # RSI
        if self._sumd == 0.0:
            self._rsi = 100.0
        else:
            rs = self._sumu / self._sumd
            self._rsi = 100.0 - 100.0 / (1.0 + rs)

        # OB / OS band calculation
        # diffupob: price delta needed if RSI <= ob to push RSI up to ob
        diffupob = (
            self._sumd * ((100.0 / (100.0 - self.ob_level)) - 1.0)
        ) * self.period - (self.period - 1) * prev_sumu

        diffdnob = (
            self._sumu / ((100.0 / (100.0 - self.ob_level)) - 1.0)
        ) * self.period - (self.period - 1) * prev_sumd

        diffupos = (
            self._sumd * ((100.0 / (100.0 - self.os_level)) - 1.0)
        ) * self.period - (self.period - 1) * prev_sumu

        diffdnos = (
            self._sumu / ((100.0 / (100.0 - self.os_level)) - 1.0)
        ) * self.period - (self.period - 1) * prev_sumd

        # oblev / oslev raw
        if self._rsi <= self.ob_level:
            ob_raw = close + diffupob
        else:
            ob_raw = close - diffdnob

        if self._rsi <= self.os_level:
            os_raw = close + diffupos
        else:
            os_raw = close - diffdnos

        # EMA smoothing
        if self._ob_ema is None:
            self._ob_ema = ob_raw
            self._os_ema = os_raw
        else:
            self._ob_ema = (ob_raw - self._ob_ema) * self._ema_mult + self._ob_ema
            self._os_ema = (os_raw - self._os_ema) * self._ema_mult + self._os_ema

        self._prev_close = close
        self._count += 1

    @property
    def ob_price(self) -> float | None:
        return self._ob_ema

    @property
    def os_price(self) -> float | None:
        return self._os_ema

    @property
    def mid_price(self) -> float | None:
        if self._ob_ema is None or self._os_ema is None:
            return None
        return (self._ob_ema + self._os_ema) / 2.0

    @property
    def rsi_value(self) -> float | None:
        return self._rsi

    @property
    def value(self) -> float | None:
        return self.mid_price

    @property
    def ready(self) -> bool:
        return self._count >= self.period
