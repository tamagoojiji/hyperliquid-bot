"""ADX (Average Directional Index) — トレンド/レンジのレジーム判定用

Wilder標準: +DM/-DM/TRをWilder平滑 → +DI/-DI → DX → DXをWilder平滑してADX。
ADX >= 25 でトレンド相場、未満でレンジ相場とみなすのが一般的。
"""


class ADX:
    def __init__(self, period: int = 14):
        self.period = period
        self._prev_high: float | None = None
        self._prev_low: float | None = None
        self._prev_close: float | None = None
        self._smooth_tr: float = 0.0
        self._smooth_plus_dm: float = 0.0
        self._smooth_minus_dm: float = 0.0
        self._adx: float | None = None
        self._count = 0

    def update(self, high: float, low: float, close: float) -> None:
        if self._prev_high is None:
            self._prev_high = high
            self._prev_low = low
            self._prev_close = close
            return

        up_move = high - self._prev_high
        down_move = self._prev_low - low
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr = max(
            high - low,
            abs(high - self._prev_close),
            abs(low - self._prev_close),
        )

        self._prev_high = high
        self._prev_low = low
        self._prev_close = close
        self._count += 1

        p = self.period
        if self._count <= p:
            # 最初のperiod本は単純加算でシード
            self._smooth_tr += tr
            self._smooth_plus_dm += plus_dm
            self._smooth_minus_dm += minus_dm
            if self._count < p:
                return
        else:
            # Wilder's smoothing
            self._smooth_tr = self._smooth_tr - self._smooth_tr / p + tr
            self._smooth_plus_dm = self._smooth_plus_dm - self._smooth_plus_dm / p + plus_dm
            self._smooth_minus_dm = self._smooth_minus_dm - self._smooth_minus_dm / p + minus_dm

        if self._smooth_tr <= 0:
            return
        plus_di = 100.0 * self._smooth_plus_dm / self._smooth_tr
        minus_di = 100.0 * self._smooth_minus_dm / self._smooth_tr
        di_sum = plus_di + minus_di
        if di_sum <= 0:
            return
        dx = 100.0 * abs(plus_di - minus_di) / di_sum

        if self._adx is None:
            self._adx = dx
        else:
            self._adx = (self._adx * (p - 1) + dx) / p

    @property
    def value(self) -> float | None:
        return self._adx

    @property
    def ready(self) -> bool:
        # DXのWilder平滑が安定するまで約2period本必要
        return self._count >= self.period * 2 and self._adx is not None
