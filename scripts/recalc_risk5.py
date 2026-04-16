"""リスク5%でfull_mm SOLの成績を再計算"""
import sqlite3

db = sqlite3.connect("/app/data/bot.db")
rows = db.execute("""
    SELECT strategy, symbol, side, signal_price, size, estimated_pnl, fill_model, timestamp
    FROM shadow_fills
    WHERE fill_model IN ('mm_sim', 'mm_sim_exit')
      AND strategy = 'full_mm' AND symbol = 'SOL'
    ORDER BY timestamp
""").fetchall()

trades = []
pending = None
for r in rows:
    strat, sym, side, price, size, pnl, model, ts = r
    if model == "mm_sim":
        pending = {"side": side, "price": price, "size": size}
    elif model == "mm_sim_exit" and pending:
        entry_vol = pending["size"] * pending["price"]
        if entry_vol > 100:  # バグ前データのみ
            trades.append({
                "entry_price": pending["price"],
                "exit_price": price,
                "side": pending["side"],
                "vol": entry_vol,
                "pnl": pnl,
            })
        pending = None

if not trades:
    print("No trades found")
    exit()

# リスク5%でのスケール計算
# 元: 8 SOL × ~$84.8 = ~$679（レバ6.8x）
# リスク5%: $100 × 5% = $5が損切り時の損失
# SOL ATR ≒ $0.50、損切り = ATR × 1.5 = $0.75
# ポジションサイズ = $5 / $0.75 = 6.67 SOL
# ポジションUSD = 6.67 × $84.8 = $565（レバ5.65x）
avg_vol = sum(t["vol"] for t in trades) / len(trades)
risk5_vol = 565.0
scale = risk5_vol / avg_vol

total_pnl = 0.0
print(f"full_mm SOL リスク5%再計算 ({len(trades)}取引、約4時間)")
print(f"元vol平均: ${avg_vol:.0f}(レバ6.8x) → リスク5%vol: ${risk5_vol:.0f}(レバ5.65x)")
print()
print(f"{'#':>3} {'方向':>6} {'損益':>12} {'損益%':>8} {'累計':>12} {'累計%':>8}")
print("-" * 58)

for i, t in enumerate(trades):
    d = "ロング" if t["side"] == "buy" else "ショート"
    adj_pnl = t["pnl"] * scale
    total_pnl += adj_pnl
    pct = adj_pnl / 100 * 100
    total_pct = total_pnl / 100 * 100
    print(f"{i+1:>3} {d:>6} ${adj_pnl:>10.4f} {pct:>7.2f}% ${total_pnl:>10.4f} {total_pct:>7.2f}%")

wins = sum(1 for t in trades if t["pnl"] > 0)
losses = len(trades) - wins
print("-" * 58)
print(f"合計: ${total_pnl:.2f} ({total_pnl:.2f}%)")
print(f"勝率: {wins}勝 {losses}敗 ({wins/len(trades)*100:.0f}%)")
print(f"平均利益: ${sum(t['pnl']*scale for t in trades if t['pnl']>0)/max(wins,1):.4f}")
print(f"平均損失: ${sum(t['pnl']*scale for t in trades if t['pnl']<0)/max(losses,1):.4f}")
db.close()
