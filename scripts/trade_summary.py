"""トレード履歴サマリー"""
import sqlite3

db = sqlite3.connect("/app/data/bot.db")

rows = db.execute("""
    SELECT strategy, symbol, side, signal_price, size, estimated_pnl, fill_model, timestamp
    FROM shadow_fills
    WHERE fill_model IN ('mm_sim', 'mm_sim_exit')
    ORDER BY strategy, symbol, timestamp
""").fetchall()

trades = {}

for r in rows:
    strat, sym, side, price, size, pnl, model, ts = r
    key = (strat, sym)
    if key not in trades:
        trades[key] = {"pending": None, "completed": []}

    if model == "mm_sim":
        trades[key]["pending"] = {"side": side, "price": price, "size": size, "ts": ts}
    elif model == "mm_sim_exit" and trades[key]["pending"]:
        entry = trades[key]["pending"]
        vol_usd = entry["size"] * entry["price"]
        pnl_pct = pnl / 100 * 100  # vs $100
        trades[key]["completed"].append({
            "entry_side": entry["side"],
            "entry_price": entry["price"],
            "exit_price": price,
            "size": entry["size"],
            "vol_usd": vol_usd,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "entry_ts": entry["ts"],
            "exit_ts": ts,
        })
        trades[key]["pending"] = None

for key in sorted(trades.keys()):
    strat, sym = key
    completed = trades[key]["completed"]
    if not completed:
        continue
    print(f"\n=== {strat} {sym} ({len(completed)} trades) ===")
    print(f"{'#':>3} {'方向':>8} {'ボリューム':>14} {'損益':>14} {'損益%':>10}")
    total_pnl = 0
    for i, t in enumerate(completed):
        direction = "ロング" if t["entry_side"] == "buy" else "ショート"
        total_pnl += t["pnl"]
        print(f"{i+1:>3} {direction:>8} ${t['vol_usd']:>12.2f} ${t['pnl']:>12.4f} {t['pnl_pct']:>9.4f}%")
    wins = sum(1 for t in completed if t["pnl"] > 0)
    losses = sum(1 for t in completed if t["pnl"] <= 0)
    print(f"--- 合計: ${total_pnl:.4f} | 勝率: {wins}/{len(completed)} ({wins/len(completed)*100:.0f}%) ---")

db.close()
