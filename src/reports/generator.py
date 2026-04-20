"""レポート生成 v4: 3枚PNG構成・6銘柄詳細分析

PNG1: 相場概況・銘柄別インジケーター総覧
PNG2: 銘柄別マルチTFローソク（5m/30m/1h/4hから2つ、約定▲▼付き）
PNG3: 戦略分析・取引0件診断・まとめ
"""

import sqlite3
import os
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.font_manager as fm
from matplotlib.colors import LinearSegmentedColormap
import numpy as np

JST = timezone(timedelta(hours=9))
DB_PATH = os.environ.get('BOT_DB_PATH', '/app/data/bot.db')
INITIAL_BALANCE = 100.0

SYMBOLS = ['BTC', 'SOL', 'ETH', 'XRP', 'BNB', 'AVAX']
STRATEGIES = ['full_mm', 'simple_mm', 'macd_vwap']
STRATEGY_JP = {
    'full_mm': 'full_mm（多段MM）',
    'simple_mm': 'simple_mm（単段MM）',
    'macd_vwap': 'macd_vwap（トレンド）',
}
STRATEGY_COND = {
    'full_mm': {
        'tf': 'Tick / 継続quote',
        'cond': 'BBOに追従した多段指値quote（L0/L1/L2）。Fill toxicity/orderflow解析と在庫スキュー制御を併用。',
        'thresholds': 'spread_bps銘柄別, max_skew=4.0bps, ボラ補正0.6-3.0x',
    },
    'simple_mm': {
        'tf': 'Tick / 継続quote',
        'cond': '単段の指値quote。固定スプレッドと標準スキュー。',
        'thresholds': 'spread_bps銘柄別, 単一レベル, ボラ補正0.6-3.0x',
    },
    'macd_vwap': {
        'tf': '5分足 (フィルター: 30分EMA)',
        'cond': 'MACDゴールデン/デッドクロス + 価格とVWAPの位置関係 + 30分EMA方向フィルター。',
        'thresholds': 'MACD(12,26,9), 最大保有 8〜16本(銘柄別)で強制クローズ',
    },
}
BINANCE_MAP = {
    'BTC': 'BTCUSDT', 'SOL': 'SOLUSDT', 'ETH': 'ETHUSDT',
    'XRP': 'XRPUSDT', 'BNB': 'BNBUSDT', 'AVAX': 'AVAXUSDT',
}
TREND_JP = {'UP': '上昇', 'DOWN': '下落', 'RANGE': 'レンジ'}
TREND_ARROW = {'UP': '↑', 'DOWN': '↓', 'RANGE': '→'}

_JP_FONTS = ['Noto Sans CJK JP', 'IPAGothic', 'DejaVu Sans']
for f in _JP_FONTS:
    if any(f in n.name for n in fm.fontManager.ttflist):
        plt.rcParams['font.family'] = f
        break


# ======== DB クエリ ========
def _q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def _query_stats(conn, start_iso, end_iso):
    rows = _q(conn, """
        SELECT strategy, symbol,
               COUNT(*),
               SUM(CASE WHEN estimated_pnl!=0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN estimated_pnl>0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN estimated_pnl<0 THEN 1 ELSE 0 END),
               ROUND(SUM(estimated_pnl),4)
        FROM shadow_fills
        WHERE timestamp >= ? AND timestamp < ?
        GROUP BY strategy, symbol
    """, (start_iso, end_iso))
    out = []
    for strategy, symbol, fills, closes, wins, losses, pnl in rows:
        pnl = pnl or 0.0
        closes = closes or 0
        wins = wins or 0
        losses = losses or 0
        wr = (wins / closes * 100) if closes > 0 else 0.0
        out.append({
            'strategy': strategy, 'symbol': symbol,
            'fills': fills, 'closes': closes,
            'wins': wins, 'losses': losses, 'win_rate': wr,
            'pnl': pnl, 'pnl_pct': pnl / INITIAL_BALANCE * 100,
            'balance': INITIAL_BALANCE + pnl,
        })
    out.sort(key=lambda x: x['pnl'], reverse=True)
    return out


def _query_ls_by_symbol(conn, start_iso, end_iso):
    """(strategy, symbol) ごとに Long / Short の内訳を返す。PNG1カード用。

    Long:  エントリー= mm_sim/touch × buy, 決済= mm_sim_exit × sell
    Short: エントリー= mm_sim/touch × sell, 決済= mm_sim_exit × buy
    """
    rows = _q(conn, """
        SELECT strategy, symbol,
               SUM(CASE WHEN fill_model IN ('mm_sim','touch') AND side='buy'  THEN 1 ELSE 0 END),
               SUM(CASE WHEN fill_model='mm_sim_exit' AND side='sell' THEN 1 ELSE 0 END),
               SUM(CASE WHEN fill_model='mm_sim_exit' AND side='sell' AND estimated_pnl>0 THEN 1 ELSE 0 END),
               ROUND(SUM(CASE WHEN fill_model='mm_sim_exit' AND side='sell' THEN estimated_pnl ELSE 0 END), 4),
               SUM(CASE WHEN fill_model IN ('mm_sim','touch') AND side='sell' THEN 1 ELSE 0 END),
               SUM(CASE WHEN fill_model='mm_sim_exit' AND side='buy'  THEN 1 ELSE 0 END),
               SUM(CASE WHEN fill_model='mm_sim_exit' AND side='buy'  AND estimated_pnl>0 THEN 1 ELSE 0 END),
               ROUND(SUM(CASE WHEN fill_model='mm_sim_exit' AND side='buy'  THEN estimated_pnl ELSE 0 END), 4)
        FROM shadow_fills
        WHERE timestamp >= ? AND timestamp < ?
        GROUP BY strategy, symbol
    """, (start_iso, end_iso))
    out = {}
    for (strategy, symbol, l_ent, l_cls, l_win, l_pnl,
         s_ent, s_cls, s_win, s_pnl) in rows:
        l_pnl = l_pnl or 0.0
        s_pnl = s_pnl or 0.0
        l_cls = l_cls or 0
        s_cls = s_cls or 0
        out[(strategy, symbol)] = {
            'long_entries': l_ent or 0, 'long_closes': l_cls,
            'long_wins': l_win or 0,
            'long_win_rate': (l_win / l_cls * 100) if l_cls > 0 else 0.0,
            'long_pnl': l_pnl, 'long_pnl_pct': l_pnl / INITIAL_BALANCE * 100,
            'short_entries': s_ent or 0, 'short_closes': s_cls,
            'short_wins': s_win or 0,
            'short_win_rate': (s_win / s_cls * 100) if s_cls > 0 else 0.0,
            'short_pnl': s_pnl, 'short_pnl_pct': s_pnl / INITIAL_BALANCE * 100,
        }
    return out


def _query_fills(conn, start_iso, end_iso, symbol):
    rows = _q(conn, """
        SELECT timestamp, strategy, side, would_fill_price
        FROM shadow_fills
        WHERE timestamp >= ? AND timestamp < ? AND symbol = ?
    """, (start_iso, end_iso, symbol))
    out = []
    for t, st, side, price in rows:
        try:
            dt = datetime.fromisoformat(t.replace('Z', '+00:00'))
        except ValueError:
            continue
        out.append((dt, st, side, price))
    return out


# ======== Binance OHLC ========
def _fetch_klines(symbol_pair, start_ms, end_ms, interval='5m'):
    url = (f"https://fapi.binance.com/fapi/v1/klines?"
           f"symbol={symbol_pair}&interval={interval}"
           f"&startTime={start_ms}&endTime={end_ms}&limit=1500")
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, Exception):
        return []


def _klines_to_arrays(klines):
    if not klines:
        return None
    return {
        'time': [datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc) for k in klines],
        'open': np.array([float(k[1]) for k in klines]),
        'high': np.array([float(k[2]) for k in klines]),
        'low': np.array([float(k[3]) for k in klines]),
        'close': np.array([float(k[4]) for k in klines]),
        'volume': np.array([float(k[5]) for k in klines]),
    }


# ======== インジケーター ========
def _rsi(closes, period=14):
    if len(closes) <= period:
        return np.array([])
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g = gains[:period].mean()
    avg_l = losses[:period].mean()
    rsi = [100 - 100 / (1 + avg_g / max(avg_l, 1e-10))]
    for i in range(period, len(deltas)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rsi.append(100 - 100 / (1 + avg_g / max(avg_l, 1e-10)))
    return np.array(rsi)


def _bb(closes, period=20, k=2.0):
    if len(closes) < period:
        return None
    mid = np.array([closes[i:i + period].mean() for i in range(len(closes) - period + 1)])
    std = np.array([closes[i:i + period].std() for i in range(len(closes) - period + 1)])
    return {'mid': mid, 'upper': mid + k * std, 'lower': mid - k * std,
            'start_idx': period - 1}


def _ema(values, period):
    if len(values) == 0:
        return np.array([])
    alpha = 2 / (period + 1)
    ema = [values[0]]
    for v in values[1:]:
        ema.append(alpha * v + (1 - alpha) * ema[-1])
    return np.array(ema)


def _macd(closes, fast=12, slow=26, sig=9):
    if len(closes) < slow:
        return None
    ema_f = _ema(closes, fast)
    ema_s = _ema(closes, slow)
    line = ema_f - ema_s
    signal = _ema(line, sig)
    return {'macd': line, 'signal': signal}


def _atr(highs, lows, closes, period=14):
    if len(closes) <= period:
        return np.array([])
    tr = np.maximum.reduce([
        highs[1:] - lows[1:],
        np.abs(highs[1:] - closes[:-1]),
        np.abs(lows[1:] - closes[:-1]),
    ])
    atr = [tr[:period].mean()]
    for i in range(period, len(tr)):
        atr.append((atr[-1] * (period - 1) + tr[i]) / period)
    return np.array(atr)


def _compute_indicators(symbol, since_jst, until_jst):
    """5分足ベースで各種インジケーター統計を算出"""
    start_ms = int(since_jst.timestamp() * 1000)
    end_ms = int(until_jst.timestamp() * 1000)
    # 前のウォームアップ用に3時間余分に取る
    warmup_ms = 3 * 3600 * 1000
    klines = _fetch_klines(BINANCE_MAP[symbol], start_ms - warmup_ms, end_ms, '5m')
    data = _klines_to_arrays(klines)
    if not data:
        return None
    # 期間中のみ抽出するインデックス
    period_mask = np.array([t.timestamp() * 1000 >= start_ms for t in data['time']])
    if not period_mask.any():
        return None
    o, h, l, c, v = data['open'], data['high'], data['low'], data['close'], data['volume']
    t_arr = data['time']

    # RSI
    rsi = _rsi(c, 14)
    rsi_offset = len(c) - len(rsi)
    # BB
    bb = _bb(c, 20, 2.0)
    # MACD
    mac = _macd(c, 12, 26, 9)
    # ATR
    atr = _atr(h, l, c, 14)
    atr_offset = len(c) - len(atr)
    # EMA9
    ema9 = _ema(c, 9)

    # 期間中マスクに対応する範囲で統計
    def _in_period_mask(offset, length):
        return period_mask[offset: offset + length]

    # RSI統計
    rsi_mask = _in_period_mask(rsi_offset, len(rsi))
    rsi_in = rsi[rsi_mask] if rsi_mask.any() else np.array([])
    rsi_touches_30 = 0
    rsi_touches_70 = 0
    if len(rsi) >= 2:
        # 30 を上から下へ抜けた数、70 を下から上へ抜けた数
        for i in range(1, len(rsi)):
            if not rsi_mask[i]:
                continue
            if rsi[i - 1] >= 30 and rsi[i] < 30:
                rsi_touches_30 += 1
            if rsi[i - 1] <= 70 and rsi[i] > 70:
                rsi_touches_70 += 1

    # BB タッチ数
    bb_lower_touches = 0
    bb_upper_touches = 0
    if bb:
        bb_start = bb['start_idx']
        for idx in range(bb_start, len(c)):
            if not period_mask[idx]:
                continue
            bi = idx - bb_start
            if l[idx] <= bb['lower'][bi]:
                bb_lower_touches += 1
            if h[idx] >= bb['upper'][bi]:
                bb_upper_touches += 1

    # MACD クロス
    macd_crosses = 0
    if mac:
        diff = mac['macd'] - mac['signal']
        for i in range(1, len(diff)):
            if not period_mask[i]:
                continue
            if np.sign(diff[i - 1]) != np.sign(diff[i]) and diff[i - 1] != 0:
                macd_crosses += 1

    # ATR/出来高スパイク比（期間中の max / 期間全体の mean）
    atr_mask = _in_period_mask(atr_offset, len(atr))
    atr_in = atr[atr_mask] if atr_mask.any() else np.array([0.0])
    atr_mean = atr.mean() if len(atr) else 0.0
    atr_spike_ratio = (atr_in.max() / atr_mean) if atr_mean > 0 else 0.0
    vol_in = v[period_mask]
    vol_mean = v.mean() if len(v) else 0.0
    vol_spike_ratio = (vol_in.max() / vol_mean) if vol_mean > 0 else 0.0

    # 期間内の価格統計
    closes_in = c[period_mask]
    highs_in = h[period_mask]
    lows_in = l[period_mask]
    opens_in = o[period_mask]
    start_price = opens_in[0]
    end_price = closes_in[-1]
    hi = highs_in.max()
    lo = lows_in.min()
    change_pct = (end_price - start_price) / start_price * 100
    range_pct = (hi - lo) / start_price * 100
    if abs(change_pct) < 0.5:
        trend = 'RANGE'
    elif change_pct > 0:
        trend = 'UP'
    else:
        trend = 'DOWN'
    # EMAトレンド判定（EMA9始値vs終値）
    ema_in = ema9[period_mask]
    ema_trend = 'UP' if ema_in[-1] > ema_in[0] else 'DOWN'

    return {
        'symbol': symbol,
        'start_price': start_price,
        'end_price': end_price,
        'high': hi,
        'low': lo,
        'change_pct': change_pct,
        'range_pct': range_pct,
        'trend': trend,
        'ema_trend': ema_trend,
        'rsi_min': float(rsi_in.min()) if len(rsi_in) else 0.0,
        'rsi_max': float(rsi_in.max()) if len(rsi_in) else 0.0,
        'rsi_touches_30': rsi_touches_30,
        'rsi_touches_70': rsi_touches_70,
        'bb_lower_touches': bb_lower_touches,
        'bb_upper_touches': bb_upper_touches,
        'atr_spike_ratio': atr_spike_ratio,
        'vol_spike_ratio': vol_spike_ratio,
        'macd_crosses': macd_crosses,
        # 描画用データ
        'time_in': [t for t, m in zip(t_arr, period_mask) if m],
        'open_in': opens_in, 'high_in': highs_in, 'low_in': lows_in, 'close_in': closes_in,
    }


def _fmt_price(sym, v):
    if sym == 'BTC':
        return f"${v:,.0f}"
    if sym in ('ETH', 'BNB'):
        return f"${v:,.1f}"
    return f"${v:,.3f}"


# ======== 各PNG生成 ========
def _make_png1_overview(since_jst, until_jst, indicators, stats, ls_map, out_path):
    """PNG1: 期間総括 + 銘柄別 相場インジケーター × 戦略別成績"""
    fig = plt.figure(figsize=(14, 16), dpi=110)
    fig.patch.set_facecolor('#fafafa')

    stat_map = {(r['strategy'], r['symbol']): r for r in stats}
    total_pnl = sum(r['pnl'] for r in stats)
    total_slots = len(SYMBOLS) * len(STRATEGIES)
    active = sum(1 for r in stats if r['closes'] > 0)
    total_fills = sum(r['fills'] for r in stats)
    total_closes = sum(r['closes'] for r in stats)
    pct = total_pnl / (INITIAL_BALANCE * total_slots) * 100
    hours = int((until_jst - since_jst).total_seconds() // 3600)

    # タイトル
    fig.text(0.04, 0.975, 'Hyperliquid Bot 期間分析レポート ① 相場概況 & 戦略別成績',
             fontsize=18, fontweight='bold', color='#1a237e')
    sign = '+' if total_pnl >= 0 else ''
    fig.text(0.04, 0.958,
             f"期間: {since_jst.strftime('%m/%d %H:%M')} - "
             f"{until_jst.strftime('%m/%d %H:%M')} JST ({hours}時間)   "
             f"Fill数: {total_fills} / 決済: {total_closes}   "
             f"合計PnL: {sign}${total_pnl:.2f} ({sign}{pct:.2f}%)   "
             f"稼働: {active}/{total_slots}",
             fontsize=10, color='#555')

    # 期間総括文章
    ax_ov = fig.add_axes([0.04, 0.905, 0.92, 0.04])
    ax_ov.axis('off')
    overview_lines = []
    overview_lines.append(
        f"本期間 {hours}時間で bot 全体は {total_fills} 件の約定・{total_closes} 件の決済が発生し、"
        f"合計損益は ${total_pnl:+.2f}（{pct:+.2f}%）となりました。"
    )
    ups = [s['symbol'] for s in indicators.values() if s and s['trend'] == 'UP']
    downs = [s['symbol'] for s in indicators.values() if s and s['trend'] == 'DOWN']
    ranges = [s['symbol'] for s in indicators.values() if s and s['trend'] == 'RANGE']
    parts = []
    if ups:
        parts.append(f"上昇={', '.join(ups)}")
    if downs:
        parts.append(f"下落={', '.join(downs)}")
    if ranges:
        parts.append(f"レンジ={', '.join(ranges)}")
    overview_lines.append("相場の方向性は " + " ／ ".join(parts) + " でした。")
    ax_ov.text(0.0, 0.5, "\n".join(overview_lines), fontsize=10.5, color='#222', va='center',
               bbox=dict(boxstyle='round,pad=0.6', facecolor='#e8eaf6',
                         edgecolor='#9fa8da'))

    # 用語解説ボックス（ATR/出来高スパイク基準値）
    ax_leg = fig.add_axes([0.04, 0.855, 0.92, 0.035])
    ax_leg.axis('off')
    legend_text = (
        "▼ 用語解説（毎日確認用）\n"
        "・ATRスパイク比 = 期間内のATR最大値 ÷ 期間平均。"
        "×1.0=平常、×1.5=やや拡大、×2.0以上=急激なボラ拡大（breakout戦略の発火閾値）、×3.0以上=異常値。\n"
        "・出来高スパイク比 = 期間内の出来高最大値 ÷ 期間平均。"
        "×1.0=平常、×1.5以上=大口流入で注目（breakout戦略の発火閾値）、×3.0以上=ニュース級の急騰急落を疑う。\n"
        "・breakout戦略はATR×2.0以上 AND 出来高×1.5以上 AND 直近高安ブレイクの3条件同時成立で発火します。"
    )
    ax_leg.text(0.0, 0.5, legend_text, fontsize=9.3, color='#222', va='center',
                bbox=dict(boxstyle='round,pad=0.6', facecolor='#fff8e1',
                          edgecolor='#ffa000'))

    # セクション見出し
    fig.text(0.04, 0.820,
             '■ 銘柄別 相場インジケーター ＋ 戦略別成績（5分足ベース）',
             fontsize=13, fontweight='bold', color='#283593')

    # カード 3行×2列
    card_h_fig = 0.250  # 各カードの図内高さ
    for idx, sym in enumerate(SYMBOLS):
        col = idx % 2
        row = idx // 2
        x0 = 0.04 + col * 0.485
        y1 = 0.805 - row * (card_h_fig + 0.008)
        y0 = y1 - card_h_fig
        ax = fig.add_axes([x0, y0, 0.47, card_h_fig])
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis('off')
        ind = indicators.get(sym)
        # 背景枠
        ax.add_patch(plt.Rectangle((0, 0), 1, 1, facecolor='white',
                                    edgecolor='#c5cae9', linewidth=1.2))
        if not ind:
            ax.text(0.5, 0.5, f"{sym}/USDC: データ取得失敗",
                    ha='center', va='center', fontsize=11, color='#999')
            continue

        # 銘柄ヘッダ（上10%）
        ax.add_patch(plt.Rectangle((0, 0.905), 1, 0.095, facecolor='#283593'))
        ax.text(0.02, 0.952, f"{sym}/USDC  {TREND_ARROW[ind['trend']]}",
                fontsize=13, fontweight='bold', color='white', va='center')
        ax.text(0.25, 0.952,
                f"{_fmt_price(sym, ind['start_price'])} → {_fmt_price(sym, ind['end_price'])}"
                f"   変化 {ind['change_pct']:+.2f}%   値幅 {ind['range_pct']:.2f}%",
                fontsize=9.5, color='white', va='center')

        # 左半分: 相場インジケーター（0.02〜0.48）
        ax.text(0.02, 0.870, '▼ 相場インジケーター',
                fontsize=9, fontweight='bold', color='#283593', va='top')
        entries = [
            ('期間高値 / 安値', f"{_fmt_price(sym, ind['high'])} / {_fmt_price(sym, ind['low'])}"),
            ('EMA9トレンド', f"{TREND_ARROW[ind['ema_trend']]} {TREND_JP[ind['ema_trend']]}"),
            ('RSI レンジ', f"{ind['rsi_min']:.1f} 〜 {ind['rsi_max']:.1f}"),
            ('RSI 30到達 / 70到達', f"{ind['rsi_touches_30']}回 / {ind['rsi_touches_70']}回"),
            ('BB下限/上限タッチ', f"{ind['bb_lower_touches']}回 / {ind['bb_upper_touches']}回"),
            ('ATRスパイク比', f"×{ind['atr_spike_ratio']:.2f}  (閾値 ×2.0)"),
            ('出来高スパイク比', f"×{ind['vol_spike_ratio']:.2f}  (閾値 ×1.5)"),
            ('MACDクロス回数', f"{ind['macd_crosses']}回"),
        ]
        # 8項目を縦並び（左カラム）
        for i, (label, val) in enumerate(entries):
            yy = 0.820 - i * 0.090
            ax.text(0.03, yy, label, fontsize=8.2, color='#555', va='top')
            ax.text(0.03, yy - 0.04, val, fontsize=9.5, color='#1a237e',
                    fontweight='bold', va='top')

        # 右半分: 戦略別成績（0.50〜0.98）Long/Short 内訳付き
        ax.text(0.50, 0.870,
                '▼ 戦略別成績 ＋ Long/Short 内訳（PnL% = $100基準）',
                fontsize=8.8, fontweight='bold', color='#283593', va='top')
        col_x = {'戦略': 0.505, 'Fill': 0.660, '決済': 0.720,
                 '勝率': 0.785, 'PnL$': 0.865, 'PnL%': 0.950}
        tbl_top = 0.840
        hdr_h = 0.045
        # 各戦略ブロック: total 1行 + Long 1行 + Short 1行
        total_row_h = 0.050
        ls_row_h = 0.042
        block_h = total_row_h + ls_row_h * 2
        # ヘッダ
        ax.add_patch(plt.Rectangle((0.50, tbl_top - hdr_h), 0.48, hdr_h,
                                    facecolor='#283593', zorder=2))
        hdr_y = tbl_top - hdr_h / 2
        for hname, hx in col_x.items():
            ax.text(hx, hdr_y, hname, fontsize=7.8, color='white',
                    va='center',
                    ha='left' if hname == '戦略' else 'center',
                    fontweight='bold', zorder=3)

        def _draw_row(row_top, row_h, bg, cells, label_color, value_color,
                      fw_pnl_dollar, pnl_pct_color, font_size, indent=False):
            row_bot = row_top - row_h
            ax.add_patch(plt.Rectangle((0.50, row_bot), 0.48, row_h,
                                        facecolor=bg, edgecolor='#ddd',
                                        linewidth=0.4, zorder=2))
            row_mid = (row_top + row_bot) / 2
            for cname, val in cells.items():
                if cname == '戦略':
                    col_txt = label_color
                    fw = 'bold'
                    hx = col_x[cname] + (0.015 if indent else 0.0)
                    ax.text(hx, row_mid, val, fontsize=font_size,
                            color=col_txt, va='center', ha='left',
                            fontweight=fw, zorder=3)
                    continue
                if cname == 'PnL%':
                    col_txt = pnl_pct_color
                    fw = 'bold'
                elif cname == 'PnL$':
                    col_txt = value_color
                    fw = fw_pnl_dollar
                else:
                    col_txt = value_color
                    fw = 'normal'
                ax.text(col_x[cname], row_mid, val, fontsize=font_size,
                        color=col_txt, va='center', ha='center',
                        fontweight=fw, zorder=3)

        # データ: 3戦略 × (total, Long, Short)
        cursor_top = tbl_top - hdr_h
        for si, st in enumerate(STRATEGIES):
            r = stat_map.get((st, sym))
            ls = ls_map.get((st, sym))

            # ----- total 行 -----
            if r and r['pnl'] > 0:
                bg = '#e8f5e9'
            elif r and r['pnl'] < 0:
                bg = '#ffebee'
            else:
                bg = '#f5f5f5'
            if r:
                wr_s = f"{r['win_rate']:.0f}%" if r['closes'] > 0 else '-'
                pnl_s = f"{r['pnl']:+.2f}" if r['fills'] > 0 else '-'
                pct_s = f"{r['pnl_pct']:+.2f}%" if r['fills'] > 0 else '-'
                cells = {'戦略': st, 'Fill': str(r['fills']),
                         '決済': str(r['closes']), '勝率': wr_s,
                         'PnL$': pnl_s, 'PnL%': pct_s}
                pct_color = ('#2e7d32' if r['pnl'] > 0 else
                             ('#c62828' if r['pnl'] < 0 else '#555'))
            else:
                cells = {'戦略': st, 'Fill': '0', '決済': '0',
                         '勝率': '-', 'PnL$': '-', 'PnL%': '-'}
                pct_color = '#555'
            _draw_row(cursor_top, total_row_h, bg, cells,
                      label_color='#222', value_color='#222',
                      fw_pnl_dollar='bold', pnl_pct_color=pct_color,
                      font_size=8.3, indent=False)
            cursor_top -= total_row_h

            # ----- Long 行 -----
            if ls and ls['long_entries'] > 0:
                l_wr = f"{ls['long_win_rate']:.0f}%" if ls['long_closes'] > 0 else '-'
                l_pnl_s = f"{ls['long_pnl']:+.2f}" if ls['long_closes'] > 0 else '-'
                l_pct_s = f"{ls['long_pnl_pct']:+.2f}%" if ls['long_closes'] > 0 else '-'
                l_cells = {'戦略': '└ Long', 'Fill': str(ls['long_entries']),
                           '決済': str(ls['long_closes']), '勝率': l_wr,
                           'PnL$': l_pnl_s, 'PnL%': l_pct_s}
                l_bg = ('#e3f2fd' if ls['long_pnl'] > 0 else
                        ('#fce4ec' if ls['long_pnl'] < 0 else '#fafafa'))
                l_pct_color = ('#2e7d32' if ls['long_pnl'] > 0 else
                               ('#c62828' if ls['long_pnl'] < 0 else '#888'))
            else:
                l_cells = {'戦略': '└ Long', 'Fill': '0', '決済': '0',
                           '勝率': '-', 'PnL$': '-', 'PnL%': '-'}
                l_bg = '#fafafa'
                l_pct_color = '#888'
            _draw_row(cursor_top, ls_row_h, l_bg, l_cells,
                      label_color='#1976d2', value_color='#333',
                      fw_pnl_dollar='normal', pnl_pct_color=l_pct_color,
                      font_size=7.5, indent=True)
            cursor_top -= ls_row_h

            # ----- Short 行 -----
            if ls and ls['short_entries'] > 0:
                s_wr = f"{ls['short_win_rate']:.0f}%" if ls['short_closes'] > 0 else '-'
                s_pnl_s = f"{ls['short_pnl']:+.2f}" if ls['short_closes'] > 0 else '-'
                s_pct_s = f"{ls['short_pnl_pct']:+.2f}%" if ls['short_closes'] > 0 else '-'
                s_cells = {'戦略': '└ Short', 'Fill': str(ls['short_entries']),
                           '決済': str(ls['short_closes']), '勝率': s_wr,
                           'PnL$': s_pnl_s, 'PnL%': s_pct_s}
                s_bg = ('#e3f2fd' if ls['short_pnl'] > 0 else
                        ('#fce4ec' if ls['short_pnl'] < 0 else '#fafafa'))
                s_pct_color = ('#2e7d32' if ls['short_pnl'] > 0 else
                               ('#c62828' if ls['short_pnl'] < 0 else '#888'))
            else:
                s_cells = {'戦略': '└ Short', 'Fill': '0', '決済': '0',
                           '勝率': '-', 'PnL$': '-', 'PnL%': '-'}
                s_bg = '#fafafa'
                s_pct_color = '#888'
            _draw_row(cursor_top, ls_row_h, s_bg, s_cells,
                      label_color='#d81b60', value_color='#333',
                      fw_pnl_dollar='normal', pnl_pct_color=s_pct_color,
                      font_size=7.5, indent=True)
            cursor_top -= ls_row_h

        # 戦略別所見（カード下端）
        sym_rows = [stat_map.get((st, sym)) for st in STRATEGIES]
        sym_rows_valid = [r for r in sym_rows if r and r['fills'] > 0]
        if sym_rows_valid:
            best_r = max(sym_rows_valid, key=lambda r: r['pnl'])
            comment = (f"★ {best_r['strategy']} が最良（{best_r['pnl_pct']:+.2f}%"
                       f"  / ${best_r['pnl']:+.2f} / {best_r['closes']}決済）")
        else:
            comment = "★ この銘柄では全戦略エントリーなし"
        ax.text(0.50, 0.045, comment, fontsize=8.5, color='#e65100',
                va='center', fontweight='bold',
                bbox=dict(boxstyle='round,pad=0.3', facecolor='#fff8e1',
                          edgecolor='#ffa000'))

    fig.savefig(out_path, dpi=110, bbox_inches='tight', facecolor='#fafafa')
    plt.close(fig)


def _make_png2_charts(since_jst, until_jst, fills_by_symbol, out_path):
    """PNG2: 銘柄別マルチTFローソク（1h/5mを1枚に並べる、5mに約定▲▼）"""
    fig = plt.figure(figsize=(13, 22), dpi=110)
    fig.patch.set_facecolor('#fafafa')

    fig.text(0.05, 0.985, 'Hyperliquid Bot 期間分析レポート ② 銘柄別マルチTFチャート',
             fontsize=18, fontweight='bold', color='#1a237e')
    fig.text(0.05, 0.968,
             f"期間: {since_jst.strftime('%m/%d %H:%M')} - "
             f"{until_jst.strftime('%m/%d %H:%M')} JST   "
             f"上段=1時間足、下段=5分足（▲=buy / ▼=sell 約定）",
             fontsize=10, color='#555')

    start_ms = int(since_jst.timestamp() * 1000)
    end_ms = int(until_jst.timestamp() * 1000)

    # 6銘柄 × 2段 (1h, 5m) = 縦12サブプロット相当
    # レイアウト: 6行 × 2列(1h, 5m)
    row_h = 0.145
    for idx, sym in enumerate(SYMBOLS):
        y1 = 0.955 - (idx + 1) * row_h + 0.02
        y0 = y1 - row_h * 0.62
        # 1h
        ax1 = fig.add_axes([0.06, y0, 0.42, y1 - y0])
        # 5m
        ax2 = fig.add_axes([0.54, y0, 0.42, y1 - y0])

        # 1h取得
        k1 = _klines_to_arrays(_fetch_klines(BINANCE_MAP[sym], start_ms, end_ms, '1h'))
        if k1:
            for t, o, h, l, c in zip(k1['time'], k1['open'], k1['high'], k1['low'], k1['close']):
                color = '#26a69a' if c >= o else '#ef5350'
                ax1.plot([t, t], [l, h], color=color, linewidth=0.9)
                w = timedelta(minutes=20)
                ax1.add_patch(plt.Rectangle((t - w / 2, min(o, c)), w,
                                             abs(c - o) or (h - l) * 0.05,
                                             facecolor=color, edgecolor=color))
            ax1.xaxis.set_major_formatter(mdates.DateFormatter('%H時', tz=JST))
            ax1.xaxis.set_major_locator(mdates.HourLocator(interval=2))
        ax1.set_title(f"{sym}/USDC  1時間足", fontsize=10, loc='left', color='#283593')
        ax1.grid(alpha=0.3)
        ax1.tick_params(labelsize=7)

        # 5m取得
        k5 = _klines_to_arrays(_fetch_klines(BINANCE_MAP[sym], start_ms, end_ms, '5m'))
        if k5:
            for t, o, h, l, c in zip(k5['time'], k5['open'], k5['high'], k5['low'], k5['close']):
                color = '#26a69a' if c >= o else '#ef5350'
                ax2.plot([t, t], [l, h], color=color, linewidth=0.6)
                w = timedelta(minutes=1.5)
                ax2.add_patch(plt.Rectangle((t - w / 2, min(o, c)), w,
                                             abs(c - o) or (h - l) * 0.1,
                                             facecolor=color, edgecolor=color))
            # 約定マーカー
            fills = fills_by_symbol.get(sym, [])
            for t, st, side, price in fills:
                if side == 'buy':
                    ax2.plot(t, price, marker='^', markersize=6,
                             color='#1976d2', alpha=0.7, markeredgewidth=0)
                else:
                    ax2.plot(t, price, marker='v', markersize=6,
                             color='#d81b60', alpha=0.7, markeredgewidth=0)
            ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H時', tz=JST))
            ax2.xaxis.set_major_locator(mdates.HourLocator(interval=2))
        ax2.set_title(
            f"{sym}/USDC  5分足（約定 {len(fills_by_symbol.get(sym, []))}件）",
            fontsize=10, loc='left', color='#283593')
        ax2.grid(alpha=0.3)
        ax2.tick_params(labelsize=7)

    fig.text(0.05, 0.01,
             '※ 5分足チャート上の ▲(青)=buy / ▼(赤)=sell が約定点です。'
             'MM系はfill数が多いため密集表示になります。',
             fontsize=9, color='#666')

    fig.savefig(out_path, dpi=110, bbox_inches='tight', facecolor='#fafafa')
    plt.close(fig)


def _build_silent_diagnosis(strategy, symbol, ind, stat):
    """取引0件の組合せ診断"""
    if strategy == 'macd_vwap':
        if not ind:
            return "データ取得失敗のため診断不能。"
        if ind['macd_crosses'] == 0:
            return f"期間中のMACDクロスが {ind['macd_crosses']}回で、エントリートリガー自体が発生せず。"
        return (
            f"MACDクロスは {ind['macd_crosses']}回発生したものの、"
            f"30分EMAトレンド（{TREND_JP[ind['ema_trend']]}方向）と反対側クロスがフィルター除外され、"
            f"またポジション保有中は新規エントリーがブロックされるため実発火に至らず。"
        )
    if strategy in ('full_mm', 'simple_mm'):
        if not ind:
            return "データ取得失敗のため診断不能。"
        if ind['range_pct'] < 0.5:
            return (
                f"期間レンジ幅が {ind['range_pct']:.2f}% と極めて狭く、"
                f"MMが提示した指値が板に約定しづらい状況だった可能性。"
            )
        return (
            f"期間レンジ幅 {ind['range_pct']:.2f}% / トレンド {TREND_JP[ind['trend']]}。"
            f"MMは提示中と思われるが、この銘柄では対向flowが薄く約定が取れていない。"
            f"ボラティリティ補正（0.6-3.0x）が縮小側で維持された可能性もある。"
        )
    return "診断ルール未定義。"


def _wrap_text(text, width=30):
    """日本語対応の単純折返し（全角1文字=幅2、半角=1の近似）"""
    import unicodedata
    out = []
    cur = ''
    cur_w = 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ('F', 'W', 'A') else 1
        if cur_w + w > width and cur:
            out.append(cur)
            cur = ''
            cur_w = 0
        cur += ch
        cur_w += w
    if cur:
        out.append(cur)
    return '\n'.join(out)


def _make_png3_strategy(since_jst, until_jst, indicators, stats, out_path):
    """PNG3: 戦略分析・取引0件診断・まとめ（レイアウト計算改善版）"""
    stat_map = {(r['strategy'], r['symbol']): r for r in stats}

    # --- 事前計算: 必要な高さを算出 ---
    entered = [r for r in stats if r['fills'] > 0]
    entered.sort(key=lambda r: r['closes'], reverse=True)
    silent_list = []
    for st in STRATEGIES:
        for sym in SYMBOLS:
            r = stat_map.get((st, sym))
            if not r or r['closes'] == 0:
                silent_list.append((st, sym, r))
    n_cols = 3
    n_card_rows = (len(silent_list) + n_cols - 1) // n_cols

    # 各セクションのインチ高さ
    h_title = 0.7
    h_gap = 0.25
    h_sec_hdr = 0.4
    # テーブル高さ: 行数×0.3インチ + ヘッダ0.4
    h_table = 0.4 + max(len(entered), 1) * 0.32
    h_card = 1.1  # 1枚あたり
    h_cards = n_card_rows * h_card + (n_card_rows - 1) * 0.12
    h_summary = 2.6
    total_h = (h_title + h_gap + h_sec_hdr + h_table + h_gap
               + h_sec_hdr + h_cards + h_gap + h_sec_hdr + h_summary + 0.4)

    fig = plt.figure(figsize=(13, total_h), dpi=110)
    fig.patch.set_facecolor('#fafafa')

    # y座標はインチ単位で上から積む
    def y_frac(inch_from_top):
        return 1.0 - inch_from_top / total_h

    # タイトル
    fig.text(0.05, y_frac(0.35),
             'Hyperliquid Bot 期間分析レポート ③ 戦略分析・診断・まとめ',
             fontsize=18, fontweight='bold', color='#1a237e')
    fig.text(0.05, y_frac(0.60),
             f"期間: {since_jst.strftime('%m/%d %H:%M')} - "
             f"{until_jst.strftime('%m/%d %H:%M')} JST",
             fontsize=10, color='#555')

    cursor = h_title + h_gap  # 累積 y (top-down)

    # ===== 1. エントリーあり戦略テーブル =====
    fig.text(0.05, y_frac(cursor + 0.2),
             '■ 1. エントリーした戦略（決済数順）',
             fontsize=13, fontweight='bold', color='#283593')
    cursor += h_sec_hdr
    ax_e = fig.add_axes([0.05, y_frac(cursor + h_table), 0.90,
                          h_table / total_h])
    ax_e.axis('off')
    headers = ['戦略', '銘柄', 'Fill数', '決済数', '勝/負', '勝率', 'PnL(USD)', 'PnL%']
    rows = []
    colors = []
    for r in entered:
        wl = f"{r['wins']}/{r['losses']}" if r['closes'] > 0 else '-'
        wr = f"{r['win_rate']:.0f}%" if r['closes'] > 0 else '-'
        rows.append([r['strategy'], f"{r['symbol']}/USDC",
                     str(r['fills']), str(r['closes']), wl, wr,
                     f"{r['pnl']:+.4f}", f"{r['pnl_pct']:+.2f}%"])
        c = '#e8f5e9' if r['pnl'] > 0 else ('#ffebee' if r['pnl'] < 0 else '#f5f5f5')
        colors.append([c] * len(headers))
    if not rows:
        ax_e.text(0.5, 0.5, 'エントリーした戦略はありません',
                  ha='center', va='center', fontsize=11, color='#999')
    else:
        tbl = ax_e.table(cellText=rows, colLabels=headers, cellColours=colors,
                         loc='center', cellLoc='center')
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(9.5)
        tbl.scale(1.0, 1.2)
        for j in range(len(headers)):
            tbl[(0, j)].set_facecolor('#283593')
            tbl[(0, j)].set_text_props(color='white', fontweight='bold')
    cursor += h_table + h_gap

    # ===== 2. 取引0件診断カード =====
    fig.text(0.05, y_frac(cursor + 0.2),
             '■ 2. 取引0件だった組合せ × 理由診断',
             fontsize=13, fontweight='bold', color='#283593')
    cursor += h_sec_hdr

    card_w_frac = 0.30
    card_gap_x_frac = 0.01
    for i, (st, sym, r) in enumerate(silent_list):
        col = i % n_cols
        row = i // n_cols
        x0 = 0.05 + col * (card_w_frac + card_gap_x_frac)
        y_top_inch = cursor + row * (h_card + 0.12)
        ax = fig.add_axes([x0, y_frac(y_top_inch + h_card),
                            card_w_frac, h_card / total_h])
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis('off')
        ax.add_patch(plt.Rectangle((0, 0), 1, 1, facecolor='white',
                                    edgecolor='#ff9800', linewidth=1.5))
        ax.add_patch(plt.Rectangle((0, 0), 0.04, 1, facecolor='#ff9800'))
        fills_n = r['fills'] if r else 0
        ax.text(0.06, 0.92, f"{st} × {sym}/USDC",
                fontsize=10.5, fontweight='bold', color='#e65100', va='top')
        ax.text(0.06, 0.75, f"Fill {fills_n}件 / 決済 0件",
                fontsize=8.5, color='#555', va='top')
        diag = _build_silent_diagnosis(st, sym, indicators.get(sym), r)
        wrapped = _wrap_text(diag, width=34)
        ax.text(0.06, 0.58, wrapped, fontsize=7.6, color='#333', va='top')

    cursor += h_cards + h_gap

    # ===== 3. まとめ =====
    fig.text(0.05, y_frac(cursor + 0.2),
             '■ 3. まとめ・主要な学び',
             fontsize=13, fontweight='bold', color='#283593')
    cursor += h_sec_hdr
    ax_s = fig.add_axes([0.05, y_frac(cursor + h_summary),
                          0.90, h_summary / total_h])
    ax_s.axis('off')

    lines = []
    for sym in SYMBOLS:
        ind = indicators.get(sym)
        if not ind:
            continue
        lines.append(
            f"・{sym}/USDC: {TREND_ARROW[ind['trend']]} {TREND_JP[ind['trend']]} "
            f"／ 値幅 {ind['range_pct']:.2f}% ／ ATRスパイク×{ind['atr_spike_ratio']:.2f} "
            f"／ 出来高スパイク×{ind['vol_spike_ratio']:.2f}"
        )
    lines.append("")
    lines.append("▼ 主要な学び")
    top = max(stats, key=lambda r: r['pnl']) if stats else None
    worst = min(stats, key=lambda r: r['pnl']) if stats else None
    if top and top['pnl'] > 0:
        lines.append(
            f"・最高は {top['strategy']} × {top['symbol']}/USDC の +${top['pnl']:.2f}"
            f"（{top['closes']}決済、勝率{top['win_rate']:.0f}%）"
        )
    if worst and worst['pnl'] < 0:
        lines.append(
            f"・逆風は {worst['strategy']} × {worst['symbol']}/USDC の ${worst['pnl']:+.2f}"
            f"（相場方向との相性が悪かった可能性）"
        )
    lines.append(
        "・全体ではMM系（full_mm/simple_mm）がレンジ・小幅変動銘柄で利益を積み上げ、"
        "macd_vwap系はMACDクロス頻度とEMAフィルターの同方向一致が条件のため、"
        "トレンド転換局面でない今期間は発火限定的でした。"
    )
    lines.append(
        "・機会逸失（floor_triggered等の安全装置発動）の記録は本期間のDBにはありません。"
        "取引0件の組合せは純粋に条件未達による不発です。"
    )
    ax_s.text(0.02, 0.95, "\n".join(lines), fontsize=9.8, color='#222', va='top',
              bbox=dict(boxstyle='round,pad=0.7', facecolor='#fff8e1',
                        edgecolor='#ffa000'))

    fig.savefig(out_path, dpi=110, bbox_inches='tight', facecolor='#fafafa')
    plt.close(fig)


# ======== メインエントリ ========
def generate(since_jst, until_jst, out_path):
    """メイン: 3枚PNG生成。out_pathはベースパス、実際は3ファイル生成される。

    戻り値: summary dict（runner用）と追加PNGパスのリスト
    """
    start_iso = since_jst.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    end_iso = until_jst.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

    conn = sqlite3.connect(DB_PATH)
    try:
        stats = _query_stats(conn, start_iso, end_iso)
        ls_map = _query_ls_by_symbol(conn, start_iso, end_iso)
        fills_by_symbol = {sym: _query_fills(conn, start_iso, end_iso, sym)
                           for sym in SYMBOLS}
    finally:
        conn.close()

    # インジケーター計算（銘柄ごと）
    indicators = {sym: _compute_indicators(sym, since_jst, until_jst)
                  for sym in SYMBOLS}

    # 出力パス: report_YYYYMMDD_HHMM_1.png / _2.png / _3.png
    base = Path(out_path)
    p1 = base.with_name(base.stem + '_1_overview.png')
    p2 = base.with_name(base.stem + '_2_charts.png')
    p3 = base.with_name(base.stem + '_3_strategy.png')

    _make_png1_overview(since_jst, until_jst, indicators, stats, ls_map, str(p1))
    _make_png2_charts(since_jst, until_jst, fills_by_symbol, str(p2))
    _make_png3_strategy(since_jst, until_jst, indicators, stats, str(p3))

    # サマリー（runner の embed 用）
    total_pnl = sum(r['pnl'] for r in stats)
    total_slots = len(SYMBOLS) * len(STRATEGIES)
    total_balance = INITIAL_BALANCE * total_slots + total_pnl
    active = sum(1 for r in stats if r['closes'] > 0)
    pct = total_pnl / (INITIAL_BALANCE * total_slots) * 100
    top3 = sorted(stats, key=lambda r: r['pnl'], reverse=True)[:3]
    period_str = (f"{since_jst.strftime('%m/%d %H:%M')} - "
                  f"{until_jst.strftime('%m/%d %H:%M')} JST")

    return {
        'total_pnl': total_pnl,
        'total_balance': total_balance,
        'total_pct': pct,
        'active_strategies': active,
        'total_strategies': total_slots,
        'top3': top3,
        'period_label': period_str,
        'png_paths': [str(p1), str(p2), str(p3)],
    }
