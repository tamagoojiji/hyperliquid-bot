from datetime import datetime, timezone, timedelta

class PivotPoints:
    """日足ベースのピボットポイント計算（UTC 0:00区切り）"""

    def __init__(self):
        self._high: float = 0.0
        self._low: float = float('inf')
        self._close: float = 0.0
        self._current_day: int = -1  # UTC日付

        # 確定済みピボットライン
        self.p: float = 0.0
        self.r1: float = 0.0
        self.r2: float = 0.0
        self.r3: float = 0.0
        self.s1: float = 0.0
        self.s2: float = 0.0
        self.s3: float = 0.0
        self._ready: bool = False

        # 同一ライン制限用
        self._used_lines_today: set = set()  # "s1", "s2", "r1", "r2" etc
        self._daily_loss: float = 0.0

    def update(self, high: float, low: float, close: float, timestamp: float):
        """トレードデータまたはキャンドルで更新。日が変わったらピボット再計算"""
        utc_day = int(timestamp // 86400)

        if self._current_day == -1:
            self._current_day = utc_day
            self._high = high
            self._low = low
            self._close = close
            return

        if utc_day > self._current_day:
            # 日が変わった → 前日のデータでピボット計算
            self._calculate(self._high, self._low, self._close)
            # リセット
            self._high = high
            self._low = low
            self._close = close
            self._current_day = utc_day
            self._used_lines_today = set()
            self._daily_loss = 0.0
            self._ready = True
        else:
            # 同じ日 → high/low/close更新
            self._high = max(self._high, high)
            self._low = min(self._low, low)
            self._close = close

    def update_from_candle(self, candle):
        """CandleオブジェクトでHLCを更新"""
        self.update(candle.high, candle.low, candle.close, candle.timestamp)

    def _calculate(self, h: float, l: float, c: float):
        self.p = (h + l + c) / 3
        self.r1 = 2 * self.p - l
        self.r2 = self.p + (h - l)
        self.r3 = h + 2 * (self.p - l)
        self.s1 = 2 * self.p - h
        self.s2 = self.p - (h - l)
        self.s3 = l - 2 * (h - self.p)

    @property
    def ready(self) -> bool:
        return self._ready and self.p > 0

    def nearest_support(self, price: float) -> tuple[str, float]:
        """現在価格に最も近いサポートラインを返す (名前, 価格)"""
        supports = [("s1", self.s1), ("s2", self.s2), ("s3", self.s3)]
        return min(supports, key=lambda x: abs(price - x[1]))

    def nearest_resistance(self, price: float) -> tuple[str, float]:
        """現在価格に最も近いレジスタンスラインを返す"""
        resistances = [("r1", self.r1), ("r2", self.r2), ("r3", self.r3)]
        return min(resistances, key=lambda x: abs(price - x[1]))

    def next_line_below(self, line_name: str) -> float:
        """指定ラインの1つ下のラインを返す（損切り用）"""
        order = {"r3": self.r2, "r2": self.r1, "r1": self.p,
                 "p": self.s1, "s1": self.s2, "s2": self.s3, "s3": self.s3 - (self.s2 - self.s3)}
        return order.get(line_name, self.s3)

    def next_line_above(self, line_name: str) -> float:
        """指定ラインの1つ上のラインを返す（利確用）"""
        order = {"s3": self.s2, "s2": self.s1, "s1": self.p,
                 "p": self.r1, "r1": self.r2, "r2": self.r3, "r3": self.r3 + (self.r3 - self.r2)}
        return order.get(line_name, self.r3)

    def mark_line_used(self, line_name: str):
        """このラインを今日使用済みにする"""
        self._used_lines_today.add(line_name)

    def is_line_available(self, line_name: str) -> bool:
        """このラインが今日まだ使えるか"""
        return line_name not in self._used_lines_today

    def record_loss(self, amount: float):
        """損失を記録"""
        self._daily_loss += abs(amount)

    def is_daily_limit_reached(self, max_daily_loss: float) -> bool:
        """日次損失上限に達したか"""
        return self._daily_loss >= max_daily_loss

    def get_state(self) -> dict:
        return {
            "p": self.p, "r1": self.r1, "r2": self.r2, "r3": self.r3,
            "s1": self.s1, "s2": self.s2, "s3": self.s3,
            "used_lines": list(self._used_lines_today),
            "daily_loss": self._daily_loss,
        }
