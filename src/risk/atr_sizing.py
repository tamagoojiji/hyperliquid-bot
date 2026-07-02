"""タートルズ流ATRポジションサイジング（魔法の杖「atr-資金管理」）

「1ATR逆行 = 総資産のrisk_pct%」となる数量でエントリーし、
損切りは2ATR逆行（= 最大損失は総資産の risk_pct×2 %）。
"""


def atr_position_notional(
    equity_usd: float,
    atr: float,
    price: float,
    risk_pct: float = 1.0,
    min_notional: float = 10.0,
    max_notional: float = 30.0,
) -> float:
    """ATRベースの注文ノーショナル（USD）を返す

    数量(base) = (equity × risk_pct%) / ATR
    notional = 数量 × price を [min_notional, max_notional] にクランプ。
    HLの最小注文額($10)を下回る計算結果はmin_notionalに切り上げるため、
    小資金では実効リスクが理論値より大きくなる点に注意。
    入力が不正（ATR/価格/資金が0以下）の場合は 0.0 を返す＝発注しない。
    """
    if atr <= 0 or price <= 0 or equity_usd <= 0:
        return 0.0
    risk_usd = equity_usd * risk_pct / 100.0
    size_base = risk_usd / atr
    notional = size_base * price
    return max(min_notional, min(notional, max_notional))


def atr_stop_distance(atr: float, multiplier: float = 2.0) -> float:
    """損切り距離 = ATR × multiplier（タートルズ標準は2）"""
    return atr * multiplier
