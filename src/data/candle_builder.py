from dataclasses import dataclass
from collections import deque


@dataclass
class Candle:
    timestamp: float  # 足の開始時刻（UNIX秒）
    open: float
    high: float
    low: float
    close: float
    volume: float


class CandleBuilder:
    def __init__(self, interval_seconds: int, max_candles: int = 500):
        self.interval = interval_seconds
        self.candles: deque[Candle] = deque(maxlen=max_candles)
        self._current: Candle | None = None

    def _bucket_start(self, timestamp: float) -> float:
        """タイムスタンプが属する足の開始時刻を返す"""
        return (int(timestamp) // self.interval) * self.interval

    def update(self, price: float, size: float, timestamp: float) -> Candle | None:
        """トレードデータで更新。新しい足が確定したらCandleを返す、それ以外はNone"""
        bucket = self._bucket_start(timestamp)
        completed: Candle | None = None

        if self._current is None:
            # 初回: 新しい足を開始
            self._current = Candle(
                timestamp=bucket,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=size,
            )
            return None

        if bucket > self._current.timestamp:
            # 現在の足が確定 → candlesに追加
            completed = self._current
            self.candles.append(completed)
            # 新しい足を開始
            self._current = Candle(
                timestamp=bucket,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=size,
            )
            return completed

        # 同じ足の更新
        self._current.high = max(self._current.high, price)
        self._current.low = min(self._current.low, price)
        self._current.close = price
        self._current.volume += size
        return None

    @property
    def current(self) -> Candle | None:
        return self._current

    def load_single(self, candle: Candle):
        """過去キャンドル1本をロード（ウォームアップ用）"""
        self.candles.append(candle)
        self._current = candle

    def load_history(self, candles: list[Candle]):
        """REST APIから取得した過去データを一括ロード"""
        for candle in candles:
            self.candles.append(candle)
        if self.candles:
            self._current = self.candles[-1]
