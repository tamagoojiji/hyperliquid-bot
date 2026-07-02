"""Parabolic SAR（Wilder標準）

AF（加速因子）は start から increment ずつ増え max_af が上限。
価格がSARを跨いだらトレンド反転し、SARを直前トレンドの極値へリセットする。
"""


class ParabolicSAR:
    def __init__(self, start: float = 0.02, increment: float = 0.02, max_af: float = 0.2):
        self.start = start
        self.increment = increment
        self.max_af = max_af

        self._sar: float | None = None
        self._ep: float = 0.0          # extreme point
        self._af: float = start
        self._is_uptrend: bool = True
        self._prev_high: float | None = None
        self._prev_low: float | None = None
        self._prev2_high: float | None = None
        self._prev2_low: float | None = None
        self._count = 0

    def update(self, high: float, low: float) -> None:
        self._count += 1
        if self._sar is None:
            if self._prev_high is None:
                self._prev_high, self._prev_low = high, low
                return
            # 2本目で初期化: 上昇スタートと仮定し、SAR=直近安値
            self._is_uptrend = high >= self._prev_high
            if self._is_uptrend:
                self._sar = min(low, self._prev_low)
                self._ep = high
            else:
                self._sar = max(high, self._prev_high)
                self._ep = low
            self._af = self.start
            self._prev2_high, self._prev2_low = self._prev_high, self._prev_low
            self._prev_high, self._prev_low = high, low
            return

        # SAR更新
        sar = self._sar + self._af * (self._ep - self._sar)
        if self._is_uptrend:
            # SARは直近2本の安値を上回らない
            sar = min(sar, self._prev_low)
            if self._prev2_low is not None:
                sar = min(sar, self._prev2_low)
            if low < sar:
                # 反転（下降へ）
                self._is_uptrend = False
                sar = self._ep
                self._ep = low
                self._af = self.start
            else:
                if high > self._ep:
                    self._ep = high
                    self._af = min(self._af + self.increment, self.max_af)
        else:
            sar = max(sar, self._prev_high)
            if self._prev2_high is not None:
                sar = max(sar, self._prev2_high)
            if high > sar:
                # 反転（上昇へ）
                self._is_uptrend = True
                sar = self._ep
                self._ep = high
                self._af = self.start
            else:
                if low < self._ep:
                    self._ep = low
                    self._af = min(self._af + self.increment, self.max_af)

        self._sar = sar
        self._prev2_high, self._prev2_low = self._prev_high, self._prev_low
        self._prev_high, self._prev_low = high, low

    @property
    def value(self) -> float | None:
        return self._sar

    @property
    def is_uptrend(self) -> bool:
        return self._is_uptrend

    @property
    def ready(self) -> bool:
        return self._count >= 5 and self._sar is not None
