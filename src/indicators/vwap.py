import datetime


class VWAP:
    """VWAP（Volume Weighted Average Price）UTC 0:00リセット"""

    def __init__(self):
        self._cumulative_volume = 0.0
        self._cumulative_tp_volume = 0.0
        self._current_day = -1
        self._vwap = 0.0

    def update(self, high: float, low: float, close: float, volume: float, timestamp: float) -> None:
        utc_day = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc).timetuple().tm_yday

        if utc_day != self._current_day:
            self._cumulative_volume = 0.0
            self._cumulative_tp_volume = 0.0
            self._current_day = utc_day
            self._vwap = 0.0

        typical_price = (high + low + close) / 3.0
        self._cumulative_tp_volume += typical_price * volume
        self._cumulative_volume += volume

        if self._cumulative_volume > 0.0:
            self._vwap = self._cumulative_tp_volume / self._cumulative_volume

    @property
    def value(self) -> float:
        return self._vwap

    @property
    def ready(self) -> bool:
        return self._cumulative_volume > 0.0
