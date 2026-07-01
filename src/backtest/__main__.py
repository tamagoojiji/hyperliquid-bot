"""バックテストCLI

使い方:
  python -m src.backtest --strategy rsi30 --symbol BTC
  python -m src.backtest --strategy bb_rsi --symbol ETH --timeframe 1h --trade-log
"""

import argparse

from src.backtest.historical import fetch_candles, aggregate_to_30m
from src.backtest.engine import run_backtest
from src.backtest.metrics import summarize, print_summary, print_trade_log
from src.config import FeeConfig


# 戦略名 → (クラス, エントリー足, フィルター足 or None)
#
# このレジストリには ExitEvent を確実に発行する戦略のみ載せる
# （engine.run_backtest は consume_exit_event() を唯一の決済ソースとする）。
# 2026-07-02: 全directional戦略が _close_position で ExitEvent を発行するようになった。
# session_bo の部分利確はTP1加重平均価格の単一exitとして表現される。
_STRATEGY_REGISTRY: dict[str, tuple[str, str, str | None]] = {
    "rsi30":        ("src.strategies.rsi30.RSI30Strategy", "30m", None),
    "bb_rsi":       ("src.strategies.bb_rsi.BBRSIStrategy", "5m", None),
    "pivot_bounce": ("src.strategies.pivot_bounce.PivotBounceStrategy", "5m", "30m"),
    "breakout":     ("src.strategies.breakout.BreakoutStrategy", "5m", "30m"),
    "macd_vwap":    ("src.strategies.macd_vwap.MACDVWAPStrategy", "5m", "30m"),
    "rsi30_fibo":   ("src.strategies.rsi30_fibo.RSI30FiboStrategy", "5m", "30m"),
    "pivot_bb":     ("src.strategies.pivot_bb.PivotBBStrategy", "5m", "30m"),
    "pivot_vwap":   ("src.strategies.pivot_vwap.PivotVWAPStrategy", "5m", "30m"),
    "session_bo":   ("src.strategies.session_bo.SessionBreakoutStrategy", "5m", None),
    "donchian":     ("src.strategies.donchian.DonchianStrategy", "1d", None),
}


def _load_class(path: str):
    module_path, cls_name = path.rsplit(".", 1)
    mod = __import__(module_path, fromlist=[cls_name])
    return getattr(mod, cls_name)


def main():
    parser = argparse.ArgumentParser(description="Hyperliquid戦略バックテスト")
    parser.add_argument("--strategy", required=True, choices=list(_STRATEGY_REGISTRY.keys()))
    parser.add_argument("--symbol", required=True, help="例: BTC / SOL / ETH")
    parser.add_argument("--timeframe", help="エントリー足 override (例: 30m / 1h / 4h / 1d)")
    parser.add_argument("--initial", type=float, default=100.0, help="初期資金 USD")
    parser.add_argument("--trade-log", action="store_true", help="直近トレードを表示")
    args = parser.parse_args()

    cls_path, default_entry_tf, filter_tf = _STRATEGY_REGISTRY[args.strategy]
    entry_tf = args.timeframe or default_entry_tf
    StrategyCls = _load_class(cls_path)
    strategy = StrategyCls(symbol=args.symbol, mode="dry")

    print(f"[fetch] {args.symbol} {entry_tf} 上限まで取得中...")
    entry_candles = fetch_candles(args.symbol, entry_tf)
    print(f"[fetch] entry candles: {len(entry_candles)} 本")

    filter_candles = None
    if filter_tf == "30m":
        print(f"[fetch] {args.symbol} 30m フィルター足取得中...")
        filter_candles = fetch_candles(args.symbol, "30m")
        print(f"[fetch] filter candles: {len(filter_candles)} 本")

    fee = FeeConfig.from_env()
    print(f"[run ] strategy={args.strategy} maker={fee.maker_bps}bps taker={fee.taker_bps}bps")

    result = run_backtest(
        strategy,
        entry_candles,
        filter_candles=filter_candles,
        maker_bps=fee.maker_bps,
        taker_bps=fee.taker_bps,
        initial_balance=args.initial,
    )
    summary = summarize(result)
    print_summary(args.strategy, args.symbol, result, summary)
    if args.trade_log:
        print_trade_log(result["trades"])


if __name__ == "__main__":
    main()
