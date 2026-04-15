from dataclasses import dataclass, field


@dataclass
class Position:
    symbol: str
    size: float = 0.0       # +ロング / -ショート
    entry_price: float = 0.0
    unrealized_pnl: float = 0.0


class PositionTracker:
    def __init__(self):
        self.positions: dict[str, Position] = {}

    def apply_fill(self, symbol: str, side: str, price: float, size: float):
        """楽観更新: fill時に即座にlocal positionを更新
        加重平均エントリー価格を計算"""
        pos = self.get(symbol)
        fill_size = size if side.upper() == "BUY" else -size

        if pos.size == 0.0:
            # 新規ポジション
            pos.size = fill_size
            pos.entry_price = price
        elif (pos.size > 0 and fill_size > 0) or (pos.size < 0 and fill_size < 0):
            # 同方向 → 加重平均
            total_cost = abs(pos.size) * pos.entry_price + abs(fill_size) * price
            pos.size += fill_size
            pos.entry_price = total_cost / abs(pos.size) if pos.size != 0 else 0.0
        else:
            # 反対方向 → 部分/全決済 or ドテン
            new_size = pos.size + fill_size
            if abs(new_size) < 1e-12:
                # 全決済
                pos.size = 0.0
                pos.entry_price = 0.0
            elif (new_size > 0) == (pos.size > 0):
                # 部分決済（方向変わらず）
                pos.size = new_size
                # entry_priceは変わらない
            else:
                # ドテン（方向反転）
                pos.size = new_size
                pos.entry_price = price

    def sync_from_exchange(
        self, symbol: str, size: float, entry_price: float, unrealized_pnl: float
    ):
        """定期同期: サーバーからのposition取得で上書き"""
        pos = self.get(symbol)
        pos.size = size
        pos.entry_price = entry_price
        pos.unrealized_pnl = unrealized_pnl

    def get(self, symbol: str) -> Position:
        if symbol not in self.positions:
            self.positions[symbol] = Position(symbol=symbol)
        return self.positions[symbol]
