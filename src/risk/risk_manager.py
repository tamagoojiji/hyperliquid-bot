from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class FillRecord:
    timestamp: str
    price: float
    size: float
    side: str
    fee: float
    pnl: float


class RiskManager:
    def __init__(self, max_loss_usd: float, max_position_usd: float):
        self.max_loss = max_loss_usd
        self.max_position = max_position_usd
        self._fills: deque[FillRecord] = deque(maxlen=1000)
        self._realized_pnl: float = 0.0
        self._total_fees: float = 0.0

    def record_fill(self, price: float, size: float, side: str, fee: float, pnl: float):
        """PnL追跡（手数料込み）"""
        record = FillRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            price=price,
            size=size,
            side=side,
            fee=fee,
            pnl=pnl,
        )
        self._fills.append(record)
        self._realized_pnl += pnl
        self._total_fees += fee

    @property
    def net_pnl(self) -> float:
        return self._realized_pnl - self._total_fees

    def should_stop(self) -> bool:
        """累計損失が閾値超えたらTrue"""
        return self.net_pnl <= -self.max_loss

    def can_open(self, symbol: str, size_usd: float, current_position_usd: float) -> bool:
        """最大ポジション制限チェック"""
        return abs(current_position_usd) + size_usd <= self.max_position

    def get_stats(self) -> dict:
        """取引回数、勝率、PnL等"""
        total = len(self._fills)
        if total == 0:
            return {
                "trade_count": 0,
                "win_count": 0,
                "loss_count": 0,
                "win_rate": 0.0,
                "realized_pnl": 0.0,
                "total_fees": 0.0,
                "net_pnl": 0.0,
                "max_drawdown": 0.0,
                "avg_pnl": 0.0,
            }

        win_count = sum(1 for f in self._fills if f.pnl > 0)
        loss_count = sum(1 for f in self._fills if f.pnl < 0)

        # max drawdown
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for f in self._fills:
            cumulative += f.pnl - f.fee
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        return {
            "trade_count": total,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": round(win_count / total * 100, 2) if total > 0 else 0.0,
            "realized_pnl": round(self._realized_pnl, 4),
            "total_fees": round(self._total_fees, 4),
            "net_pnl": round(self.net_pnl, 4),
            "max_drawdown": round(max_dd, 4),
            "avg_pnl": round(self._realized_pnl / total, 4) if total > 0 else 0.0,
        }
