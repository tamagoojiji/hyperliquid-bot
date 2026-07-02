"""過去キャンドル取得 — Hyperliquid REST candlesSnapshot"""

import time

from hyperliquid.info import Info
from hyperliquid.utils import constants

from src.data.candle_builder import Candle


_INTERVAL_MS = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}

# HL APIの1リクエスト上限（実測 ~5000本）
_HL_MAX_CANDLES_PER_REQ = 5000


def _to_candle(c: dict) -> Candle:
    return Candle(
        timestamp=float(c["t"]) / 1000.0,
        open=float(c["o"]),
        high=float(c["h"]),
        low=float(c["l"]),
        close=float(c["c"]),
        volume=float(c.get("v", 0.0)),
    )


def fetch_candles(symbol: str, interval: str, limit: int = _HL_MAX_CANDLES_PER_REQ) -> list[Candle]:
    """HL REST APIから過去キャンドルを取得。最新側に揃えて返す。

    上限が大きい場合は遡って複数リクエストし結合する。
    """
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    interval_ms = _INTERVAL_MS[interval]
    end_ms = int(time.time() * 1000)

    remaining = limit
    collected: list[dict] = []
    seen_ts: set[int] = set()

    while remaining > 0:
        chunk = min(remaining, _HL_MAX_CANDLES_PER_REQ)
        start_ms = end_ms - chunk * interval_ms
        raw = info.candles_snapshot(symbol, interval, start_ms, end_ms)
        if not raw:
            break
        # ts昇順想定。重複排除しつつ前方に積む。
        new_batch = []
        for c in raw:
            ts = int(c["t"])
            if ts in seen_ts:
                continue
            seen_ts.add(ts)
            new_batch.append(c)
        if not new_batch:
            break
        collected = new_batch + collected
        # 最古足のtsより前を次のend_msにする
        oldest_ts = int(new_batch[0]["t"])
        end_ms = oldest_ts
        remaining -= len(new_batch)
        if len(raw) < chunk:
            break  # APIが返せる過去がもうない

    collected.sort(key=lambda c: int(c["t"]))
    return [_to_candle(c) for c in collected]


def fetch_funding_history(
    symbol: str,
    start_ms: int,
    end_ms: int | None = None,
) -> list[tuple[float, float]]:
    """HLのfunding履歴を取得。[(ts_sec, rate_1h), ...] を昇順で返す。

    APIは1リクエスト最大500件（約20日分）のためページネーションする。
    """
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    end_ms = end_ms or int(time.time() * 1000)
    out: list[tuple[float, float]] = []
    cursor = start_ms
    while cursor < end_ms:
        batch = info.funding_history(symbol, cursor, end_ms)
        if not batch:
            break
        for f in batch:
            out.append((f["time"] / 1000.0, float(f["fundingRate"])))
        last_t = int(batch[-1]["time"])
        if last_t <= cursor:
            break
        cursor = last_t + 1
        if len(batch) < 500:
            break
        time.sleep(0.2)  # rate limit配慮
    out.sort(key=lambda x: x[0])
    return out


def aggregate_to_30m(c5m: list[Candle]) -> list[Candle]:
    """5分足 → 30分足に集約（6本まとめる）"""
    out: list[Candle] = []
    bucket: list[Candle] = []
    for c in c5m:
        bucket_start = (int(c.timestamp) // 1800) * 1800
        if bucket and (int(bucket[0].timestamp) // 1800) * 1800 != bucket_start:
            out.append(_merge_bucket(bucket))
            bucket = []
        bucket.append(c)
    if bucket and len(bucket) == 6:
        out.append(_merge_bucket(bucket))
    return out


def _merge_bucket(bucket: list[Candle]) -> Candle:
    return Candle(
        timestamp=(int(bucket[0].timestamp) // 1800) * 1800,
        open=bucket[0].open,
        high=max(c.high for c in bucket),
        low=min(c.low for c in bucket),
        close=bucket[-1].close,
        volume=sum(c.volume for c in bucket),
    )
