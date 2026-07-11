"""Hyperliquid funding履歴の予測可能性検証（リサーチ2026-07-11の自前OOS検証）.

検証項目:
  1. 基本統計（水準・年率換算・正の割合・分布）
  2. 系列相関（ACF、有意水準 ±1.96/sqrt(N)）
  3. 符号持続性・極端値の持続
  4. OOS 1期先予測: AR(1)/AR(24) vs 無変化モデル（RMSE・MAE・方向精度）
  5. フィルター設計用: 現在のfunding分位 → 次24時間の累積funding

使い方: python3 scripts/funding_analysis.py [--coins BTC,SOL] [--months 24]
info APIのみ使用（認証不要）。結果はstdoutにmarkdownで出力。
"""

import argparse
import json
import sys
import time
import urllib.request

import numpy as np
import pandas as pd

API = "https://api.hyperliquid.xyz/info"


def fetch_funding(coin: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    rows = []
    t = start_ms
    calls = 0
    while t < end_ms:
        body = json.dumps(
            {"type": "fundingHistory", "coin": coin, "startTime": t, "endTime": end_ms}
        ).encode()
        req = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json"})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    batch = json.load(r)
                break
            except Exception as e:
                if attempt == 4:
                    raise
                time.sleep(2 * (attempt + 1))
        calls += 1
        if not batch:
            break
        rows.extend(batch)
        last = batch[-1]["time"]
        if last <= t:
            break
        t = last + 1
        time.sleep(0.25)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["fundingRate"] = df["fundingRate"].astype(float)
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    df = df.drop_duplicates(subset="time").set_index("time").sort_index()
    print(f"  fetched {coin}: {len(df)} hours ({df.index[0]} .. {df.index[-1]}, {calls} api calls)",
          file=sys.stderr)
    return df


def acf(x: np.ndarray, lags: list[int]) -> dict[int, float]:
    x = x - x.mean()
    denom = (x * x).sum()
    return {k: float((x[:-k] * x[k:]).sum() / denom) for k in lags}


def ar_fit_predict(y: np.ndarray, p: int, train_n: int, refit_every: int = 168) -> np.ndarray:
    """ウォークフォワード1期先予測。重み最小二乗をrefit_everyごとに再推定（拡大窓）."""
    n = len(y)
    preds = np.full(n, np.nan)
    coef = None
    for t in range(train_n, n):
        if coef is None or (t - train_n) % refit_every == 0:
            yy = y[p:t]
            X = np.column_stack([y[p - k - 1 : t - k - 1] for k in range(p)])
            X = np.column_stack([np.ones(len(X)), X])
            coef, *_ = np.linalg.lstsq(X, yy, rcond=None)
        x_now = np.concatenate([[1.0], y[t - 1 : t - 1 - p : -1] if p > 1 else [y[t - 1]]])
        preds[t] = x_now @ coef
    return preds


def evaluate(y: np.ndarray, preds: np.ndarray, train_n: int) -> dict:
    idx = slice(train_n, len(y))
    actual = y[idx]
    pred = preds[idx]
    prev = y[train_n - 1 : len(y) - 1]
    rmse = float(np.sqrt(np.mean((pred - actual) ** 2)))
    mae = float(np.mean(np.abs(pred - actual)))
    # 方向精度: 予測変化と実現変化の符号一致（変化ゼロは除外）
    pc, ac = pred - prev, actual - prev
    mask = (pc != 0) & (ac != 0)
    dir_acc = float((np.sign(pc[mask]) == np.sign(ac[mask])).mean()) if mask.any() else np.nan
    # 水準の符号精度
    m2 = actual != 0
    sign_acc = float((np.sign(pred[m2]) == np.sign(actual[m2])).mean())
    return {"rmse": rmse, "mae": mae, "dir_acc": dir_acc, "sign_acc": sign_acc}


def analyze(coin: str, df: pd.DataFrame) -> None:
    f = df["fundingRate"].to_numpy()
    n = len(f)
    ann = f.mean() * 24 * 365
    print(f"\n## {coin}（{n}時間 = 約{n/24/30.4:.1f}ヶ月, {df.index[0]:%Y-%m-%d} .. {df.index[-1]:%Y-%m-%d}）")
    print(f"\n### 1. 基本統計")
    print(f"- 平均 {f.mean()*100:.5f}%/h（年率換算 {ann*100:+.2f}%）、中央値 {np.median(f)*100:.5f}%/h")
    print(f"- 正の割合 {100*(f>0).mean():.1f}%（アンカー0.00125%/hちょうど: {100*(np.isclose(f,0.0000125)).mean():.1f}%）")
    q = np.percentile(f, [1, 5, 25, 50, 75, 95, 99]) * 100
    print(f"- 分位[%/h] p1={q[0]:.5f} p5={q[1]:.5f} p25={q[2]:.5f} p50={q[3]:.5f} p75={q[4]:.5f} p95={q[5]:.5f} p99={q[6]:.5f}")

    print(f"\n### 2. 系列相関（有意水準 ±{1.96/np.sqrt(n):.4f}）")
    a = acf(f, [1, 2, 3, 6, 12, 24, 48, 168])
    print("| lag(h) | " + " | ".join(str(k) for k in a) + " |")
    print("|---|" + "---|" * len(a))
    print("| ACF | " + " | ".join(f"{v:.3f}" for v in a.values()) + " |")

    print(f"\n### 3. 符号・極端値の持続")
    sign_persist = float((np.sign(f[1:]) == np.sign(f[:-1]))[f[:-1] != 0].mean())
    print(f"- 符号持続 P(sign_t+1 = sign_t) = {sign_persist:.3f}")
    hi = f >= np.percentile(f, 90)
    lo = f <= np.percentile(f, 10)
    nxt24 = pd.Series(f).rolling(24).sum().shift(-24).to_numpy()
    m = ~np.isnan(nxt24)
    print(f"- 上位10%時 → 次24hの累積funding平均 {nxt24[hi & m].mean()*100:+.4f}%（無条件 {nxt24[m].mean()*100:+.4f}%）")
    print(f"- 下位10%時 → 次24hの累積funding平均 {nxt24[lo & m].mean()*100:+.4f}%")
    print(f"- 上位10%が24h後も上位10%に留まる確率 {float(pd.Series(hi).shift(-24)[hi & m].mean()):.3f}")

    print(f"\n### 4. OOS 1期先予測（後半30%をテスト、週次refitウォークフォワード）")
    train_n = int(n * 0.7)
    nochange = np.full(n, np.nan)
    nochange[1:] = f[:-1]
    rows = {"無変化 (f̂=f_t)": evaluate(f, nochange, train_n)}
    for p, label in [(1, "AR(1)"), (24, "AR(24)")]:
        rows[label] = evaluate(f, ar_fit_predict(f, p, train_n), train_n)
    print("| モデル | RMSE(×1e-5) | MAE(×1e-5) | 変化の方向精度 | 水準の符号精度 |")
    print("|---|---|---|---|---|")
    base = rows["無変化 (f̂=f_t)"]["rmse"]
    for k, v in rows.items():
        imp = f"（RMSE {100*(1-v['rmse']/base):+.1f}%）" if k != "無変化 (f̂=f_t)" else ""
        print(f"| {k} | {v['rmse']*1e5:.3f}{imp} | {v['mae']*1e5:.3f} | "
              f"{v['dir_acc']*100:.1f}% | {v['sign_acc']*100:.1f}% |")

    print(f"\n### 5. フィルター設計用: 現在の水準バケット → 次24h累積funding [%]")
    anchor = 0.0000125  # 0.00125%/h（8hあたり0.01%の1/8）
    p90 = np.percentile(f, 90)
    bins = [-np.inf, 0.0, anchor * 1.01, p90, np.inf]
    labels = ["負（f<0）", "アンカー以下", f"高い（〜p90={p90*100:.4f}%/h）", "極端（上位10%）"]
    qs = pd.cut(pd.Series(f[m]), bins=bins, labels=labels)
    tbl = pd.Series(nxt24[m]).groupby(qs, observed=True).agg(["mean", "median", "count"])
    for name, r in tbl.iterrows():
        print(f"- {name}: 平均 {r['mean']*100:+.4f}% / 中央値 {r['median']*100:+.4f}% (n={int(r['count'])})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default="BTC,SOL")
    ap.add_argument("--months", type=int, default=24)
    args = ap.parse_args()
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.months * 30 * 24 * 3600 * 1000
    print(f"# Hyperliquid funding予測可能性 検証結果（{pd.Timestamp.now(tz='UTC'):%Y-%m-%d}）")
    for coin in args.coins.split(","):
        df = fetch_funding(coin, start_ms, end_ms)
        if df.empty:
            print(f"\n## {coin}: データなし")
            continue
        analyze(coin, df)


if __name__ == "__main__":
    main()
