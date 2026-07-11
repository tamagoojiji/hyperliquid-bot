"""FundingGate — 極端funding時の新規ロング抑制フィルター

funding rate がローリングp90以上のとき、新規ロングのエントリーを抑制する。
ショートには一切干渉しない（高fundingはショートに追い風のため、呼び出し側で素通し）。
検証根拠: docs/検証-funding予測可能性-2026-07-11.md
"""

from collections import deque


class FundingGate:
    def __init__(self, percentile: float = 90.0, lookback_hours: int = 2160,
                 min_samples: int = 720, long_action: str = "half"):
        if long_action not in ("half", "block"):
            raise ValueError(f"invalid long_action: {long_action}")
        self.percentile = percentile
        self.min_samples = min_samples
        self.long_action = long_action
        self._rates: deque[float] = deque(maxlen=lookback_hours)
        self.triggered_count = 0

    def seed(self, rates: list[float]) -> None:
        """起動時に履歴を一括投入する"""
        for r in rates:
            self._rates.append(r)

    def update(self, rate: float) -> None:
        """毎時1件追加する"""
        self._rates.append(rate)

    @property
    def threshold(self) -> float | None:
        """サンプル数が min_samples 未満なら None、それ以外は p90 を返す

        純Pythonでパーセンタイルを計算（sorted + 線形補間、numpy不使用）。
        """
        n = len(self._rates)
        if n < self.min_samples:
            return None
        data = sorted(self._rates)
        if n == 1:
            return data[0]
        rank = (self.percentile / 100.0) * (n - 1)
        lo = int(rank)
        hi = min(lo + 1, n - 1)
        frac = rank - lo
        return data[lo] + (data[hi] - data[lo]) * frac

    def check(self, current_rate: float | None) -> tuple[bool, float, str]:
        """ロングシグナル専用のゲート判定

        戻り値 (allowed, size_multiplier, reason)。
        """
        thr = self.threshold
        if current_rate is None or thr is None:
            return (True, 1.0, "warmup")
        if current_rate >= thr:
            self.triggered_count += 1
            if self.long_action == "block":
                return (False, 0.0, "blocked")
            return (True, 0.5, "halved")
        return (True, 1.0, "pass")
