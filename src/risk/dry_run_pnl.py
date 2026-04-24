"""ドライラン損益計算 — 手数料 + funding を反映した仮想ポジション追跡

- 各エントリーで `VirtualPosition` を作成し、entry fee を即時記録
- 1時間ごとに open 中の仮想ポジションへ funding を適用（ロング=rate支払い、ショート=受取）
- 決済時に exit fee を加え、net_pnl = raw_pnl − entry_fee − exit_fee − funding を確定
"""

from dataclasses import dataclass, field
from typing import Literal


Side = Literal["buy", "sell"]


@dataclass
class VirtualPosition:
    strategy: str
    symbol: str
    side: Side                      # エントリー方向
    entry_price: float
    size: float                     # ベース通貨量（BTC数量等）
    entry_time: float               # epoch seconds
    is_maker_entry: bool            # True=maker手数料, False=taker
    entry_fee: float = 0.0
    accumulated_funding: float = 0.0  # 累計 funding コスト（USD建て、符号付き）

    @property
    def notional(self) -> float:
        return abs(self.size) * self.entry_price


def compute_fee(notional_usd: float, is_maker: bool, maker_bps: float, taker_bps: float) -> float:
    """片道手数料（USD、正の値）"""
    bps = maker_bps if is_maker else taker_bps
    return notional_usd * bps / 10000.0


def apply_funding(
    position: VirtualPosition,
    current_price: float,
    funding_rate_1h: float,
) -> float:
    """1時間分の funding をポジションに適用し、増分コストを返す

    Hyperliquid 仕様:
      - funding > 0 → ロングが支払い、ショートが受取
      - funding < 0 → その逆
      - 1時間ごとに notional × rate を精算
    """
    current_notional = abs(position.size) * current_price
    # ロングなら rate 分を支払い（コストを正で記録 = PnLから減算）
    # ショートなら rate 分を受取（コストを負で記録）
    sign = 1.0 if position.side == "buy" else -1.0
    delta = current_notional * funding_rate_1h * sign
    position.accumulated_funding += delta
    return delta


def compute_net_pnl(
    position: VirtualPosition,
    exit_price: float,
    exit_fee: float,
) -> dict:
    """決済時の純損益を計算

    Returns:
        {raw_pnl, entry_fee, exit_fee, funding, net_pnl}
    """
    if position.side == "buy":
        raw_pnl = (exit_price - position.entry_price) * position.size
    else:
        raw_pnl = (position.entry_price - exit_price) * position.size

    total_fee = position.entry_fee + exit_fee
    net = raw_pnl - total_fee - position.accumulated_funding

    return {
        "raw_pnl": raw_pnl,
        "entry_fee": position.entry_fee,
        "exit_fee": exit_fee,
        "total_fee": total_fee,
        "funding": position.accumulated_funding,
        "net_pnl": net,
    }
