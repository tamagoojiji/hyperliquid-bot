"""バックテスト結果の集計とコンソール出力"""

from datetime import datetime, timezone


def summarize(result: dict) -> dict:
    trades = result["trades"]
    initial = result["initial_balance"]
    final = result["final_balance"]

    n = len(trades)
    if n == 0:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "max_drawdown": 0.0,
            "profit_factor": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "avg_hold_min": 0.0,
            "initial": initial,
            "final": final,
        }

    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    total_pnl = sum(t.pnl_usd for t in trades)
    gross_win = sum(t.pnl_usd for t in wins)
    gross_loss = -sum(t.pnl_usd for t in losses)
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0

    # 最大DD（equity curveから）
    curve = result["equity_curve"]
    peak = initial
    max_dd = 0.0
    for _, bal in curve:
        peak = max(peak, bal)
        dd = peak - bal
        max_dd = max(max_dd, dd)

    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (-gross_loss / len(losses)) if losses else 0.0
    avg_hold_min = sum(t.hold_seconds for t in trades) / n / 60.0

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n * 100.0,
        "total_pnl": total_pnl,
        "max_drawdown": max_dd,
        "profit_factor": pf,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_hold_min": avg_hold_min,
        "initial": initial,
        "final": final,
    }


def _fmt_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def print_summary(strategy_name: str, symbol: str, result: dict, summary: dict) -> None:
    first = result["first_ts"]
    last = result["last_ts"]
    pf_str = f"{summary['profit_factor']:.2f}" if summary["profit_factor"] != float("inf") else "∞"

    print()
    print(f"=== {strategy_name} / {symbol} ===")
    print(f"期間          : {_fmt_dt(first)} → {_fmt_dt(last)} UTC")
    print(f"エントリー数   : {summary['trades']}  (勝 {summary.get('wins', 0)} / 負 {summary.get('losses', 0)})")
    print(f"勝率          : {summary['win_rate']:.1f}%")
    print(f"累計PnL       : {summary['total_pnl']:+.4f} USD  (初期 {summary['initial']:.2f} → 終値 {summary['final']:.4f})")
    print(f"最大DD        : {summary['max_drawdown']:.4f} USD")
    print(f"PF            : {pf_str}")
    print(f"平均利益/損失  : +{summary['avg_win']:.4f} / {summary['avg_loss']:.4f} USD")
    print(f"平均保有時間   : {summary['avg_hold_min']:.1f} 分")
    print()


def print_trade_log(trades: list, max_rows: int = 20) -> None:
    if not trades:
        return
    print("--- 直近トレード（最大{}件） ---".format(max_rows))
    print(f"{'entry_ts':<18}{'side':<6}{'entry':>12}{'exit':>12}{'pnl':>10}  reason")
    for t in trades[-max_rows:]:
        print(
            f"{_fmt_dt(t.entry_ts):<18}"
            f"{t.side:<6}"
            f"{t.entry_price:>12.4f}"
            f"{t.exit_price:>12.4f}"
            f"{t.pnl_usd:>+10.4f}  "
            f"{t.reason}"
        )
    print()
