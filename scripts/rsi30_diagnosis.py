"""RSI 30-30 がなぜ発火しないかを診断"""
import sys
sys.path.insert(0, "/app")

from src.exchange.hyperliquid import HyperliquidClient
from src.indicators.rsi_channel import RSIChannel
from src.indicators.bollinger import BollingerBands
from src.indicators.ema import EMA
from src.data.candle_builder import Candle

# HL APIから最新の5分足キャンドルを取得
hl = HyperliquidClient(wallet_address="", api_private_key="", account_address="")
import asyncio
# skip async connect
# 同期的に接続
from hyperliquid.info import Info
from hyperliquid.utils import constants
info = Info(constants.MAINNET_API_URL, skip_ws=True)

import time
symbol = "BTC"
end_time = int(time.time() * 1000)
start_time = end_time - (200 * 300000)  # 200本 × 5分
candles_raw = info.candles_snapshot(symbol, "5m", start_time, end_time)

# インジケーターを初期化して最新200本を流す
rsi = RSIChannel(period=14, ob_level=70, os_level=30, ema_smooth=1)
bb = BollingerBands(period=20, multiplier=2.0)
ema = EMA(period=9)

for c in candles_raw:
    close = float(c["c"])
    rsi.update(close)
    bb.update(close)
    ema.update(close)

# 最新のインジケーター値を表示
last_close = float(candles_raw[-1]["c"])
print(f"=== RSI 30-30 診断 ({symbol}) ===")
print(f"最新価格: ${last_close:,.2f}")
print(f"RSI: {rsi.rsi_value:.1f}")
print(f"RSI OB価格(70): ${rsi.ob_price:,.2f}")
print(f"RSI OS価格(30): ${rsi.os_price:,.2f}")
print(f"BB上: ${bb.upper:,.2f}")
print(f"BB下: ${bb.lower:,.2f}")
print(f"BB中: ${bb.basis:,.2f}")
print(f"EMA9: ${ema.value:,.2f}")
print()

# 条件判定
dist_to_os = (last_close - rsi.os_price) / last_close * 100
dist_to_ob = (rsi.ob_price - last_close) / last_close * 100
print(f"=== 条件チェック ===")
print(f"① RSI OS(30)ラインまで: {dist_to_os:.2f}%（マイナス=到達済み）")
print(f"① RSI OB(70)ラインまで: {dist_to_ob:.2f}%（マイナス=到達済み）")
print(f"③ 終値 > EMA9: {last_close > ema.value} ({last_close:.2f} vs {ema.value:.2f})")
print(f"④ BB下バンド付近: 価格-BB下={last_close - bb.lower:.2f}")
print()

# 直近10本でRSI OS/OBに到達した足があるか
print(f"=== 直近20本のRSI推移 ===")
rsi2 = RSIChannel(period=14, ob_level=70, os_level=30, ema_smooth=1)
for c in candles_raw[:-20]:
    rsi2.update(float(c["c"]))

for c in candles_raw[-20:]:
    close = float(c["c"])
    rsi2.update(close)
    hit = ""
    if close <= rsi2.os_price:
        hit = " ★ OS到達!"
    elif close >= rsi2.ob_price:
        hit = " ★ OB到達!"
    print(f"  close=${close:,.2f} RSI={rsi2.rsi_value:.1f} OS=${rsi2.os_price:,.2f} OB=${rsi2.ob_price:,.2f}{hit}")

# 30分足EMA方向
candles_30m = info.candles_snapshot(symbol, "30m", end_time - (100 * 1800000), end_time)
filter_ema = EMA(period=9)
for c in candles_30m:
    filter_ema.update(float(c["c"]))

prev_ema = 0
filter_ema2 = EMA(period=9)
for c in candles_30m[:-1]:
    filter_ema2.update(float(c["c"]))
prev_ema = filter_ema2.value

print(f"\n=== 30分足フィルター ===")
print(f"30分EMA: ${filter_ema.value:,.2f} (前回: ${prev_ema:,.2f})")
print(f"方向: {'上向き(買いOK)' if filter_ema.value > prev_ema else '下向き(売りOK)'}")
