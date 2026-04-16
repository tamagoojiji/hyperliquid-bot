"""フルスペックMM戦略 — Temari型マーケットメイキング"""

import math
import time
from collections import deque
from dataclasses import dataclass

from src.config import SimpleMMConfig
from src.strategies.base import BaseStrategy, Signal, SignalType
from src.utils.logger import get_logger

_SECONDS_PER_DAY = 86400

logger = get_logger("full_mm")

# ボラティリティ計算の基準値
_BASE_VOL_BPS = 5.0
_VOL_MULT_MIN = 0.6
_VOL_MULT_MAX = 3.0

# リングバッファサイズ
_OFFSET_BUFFER_SIZE = 500
_OFFSET_WINDOW_SEC = 60.0
_VOL_WINDOW_SEC = 30.0


@dataclass
class _PriceSample:
    offset: float
    ts: float


@dataclass
class _TradeSample:
    log_return: float
    ts: float


@dataclass
class _FillRecord:
    price: float
    side: str  # "buy" or "sell"
    timestamp: float
    checked: bool = False


# ------------------------------------------------------------------
# FillToxicityTracker
# ------------------------------------------------------------------

class FillToxicityTracker:
    """Fill後の逆選択を検出"""

    def __init__(self, window: int = 20, check_after_sec: float = 5.0):
        self._window = window
        self._check_after_sec = check_after_sec
        self._fills: deque[_FillRecord] = deque(maxlen=window * 2)
        self._toxic_count: int = 0
        self._total_count: int = 0
        self._results: deque[bool] = deque(maxlen=window)

    def record_fill(self, price: float, side: str, timestamp: float):
        """約定を記録"""
        self._fills.append(_FillRecord(price=price, side=side, timestamp=timestamp))

    def check_toxicity(self, current_price: float, current_time: float):
        """未チェックのfillについて5秒後の価格変動を評価"""
        for fill in self._fills:
            if fill.checked:
                continue
            elapsed = current_time - fill.timestamp
            if elapsed < self._check_after_sec:
                continue

            fill.checked = True
            # returnBps: fillした側から見た損益
            if fill.side == "buy":
                return_bps = (current_price - fill.price) / fill.price * 10000
            else:
                return_bps = (fill.price - current_price) / fill.price * 10000

            is_toxic = return_bps < -1.0

            # スライディングウィンドウ管理
            if len(self._results) >= self._window:
                old = self._results[0]
                if old:
                    self._toxic_count -= 1
                self._total_count -= 1
            self._results.append(is_toxic)
            self._total_count += 1
            if is_toxic:
                self._toxic_count += 1

    @property
    def toxic_ratio(self) -> float:
        if self._total_count == 0:
            return 0.0
        return self._toxic_count / self._total_count

    @property
    def multiplier(self) -> float:
        """toxic比率に基づくスプレッド倍率
        0% → 0.90, 20% → 1.00, 50%+ → 1.80
        線形補間: [0, 0.2] → [0.9, 1.0], [0.2, 0.5] → [1.0, 1.8]
        """
        ratio = self.toxic_ratio
        if ratio <= 0.2:
            return 0.90 + (ratio / 0.2) * 0.10
        else:
            clamped = min(ratio, 0.5)
            return 1.0 + ((clamped - 0.2) / 0.3) * 0.80


# ------------------------------------------------------------------
# SmcAnalyzer
# ------------------------------------------------------------------

class SmcAnalyzer:
    """SMC構造分析（BOS/CHoCH検出）"""

    def __init__(self, swing_lookback: int = 5):
        self._lookback = swing_lookback
        self._swing_highs: list[float] = []
        self._swing_lows: list[float] = []
        self._structure: str = "neutral"  # "bos" / "choch" / "neutral"

    def update(self, candles_15m: list, candles_4h: list):
        """キャンドルデータからスイングポイントとBOS/CHoCHを検出

        candle format: {"high": float, "low": float, "close": float, ...}
        """
        candles = candles_4h if len(candles_4h) >= self._lookback * 2 + 1 else candles_15m
        if len(candles) < self._lookback * 2 + 1:
            self._structure = "neutral"
            return

        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        lb = self._lookback

        # スイングポイント検出（前後lb本より高い/低い）
        swing_highs: list[tuple[int, float]] = []
        swing_lows: list[tuple[int, float]] = []

        for i in range(lb, len(candles) - lb):
            if all(highs[i] >= highs[i - j] for j in range(1, lb + 1)) and \
               all(highs[i] >= highs[i + j] for j in range(1, lb + 1)):
                swing_highs.append((i, highs[i]))

            if all(lows[i] <= lows[i - j] for j in range(1, lb + 1)) and \
               all(lows[i] <= lows[i + j] for j in range(1, lb + 1)):
                swing_lows.append((i, lows[i]))

        self._swing_highs = [h for _, h in swing_highs]
        self._swing_lows = [l for _, l in swing_lows]

        if len(swing_highs) < 2 or len(swing_lows) < 2:
            self._structure = "neutral"
            return

        # 直近2つのスイングで判定
        prev_high, last_high = swing_highs[-2][1], swing_highs[-1][1]
        prev_low, last_low = swing_lows[-2][1], swing_lows[-1][1]

        # 上昇トレンド中のBOS: Higher High + Higher Low
        # 下降トレンド中のBOS: Lower High + Lower Low
        is_hh_hl = last_high > prev_high and last_low > prev_low
        is_ll_lh = last_high < prev_high and last_low < prev_low

        if is_hh_hl or is_ll_lh:
            self._structure = "bos"
            return

        # CHoCH: トレンド反転 (Higher High + Lower Low or vice versa)
        is_reversal = (last_high > prev_high and last_low < prev_low) or \
                      (last_high < prev_high and last_low > prev_low)

        if is_reversal:
            self._structure = "choch"
        else:
            self._structure = "neutral"

    @property
    def structure(self) -> str:
        return self._structure

    @property
    def multiplier(self) -> float:
        if self._structure == "choch":
            return 1.5
        return 1.0


# ------------------------------------------------------------------
# FullMMStrategy
# ------------------------------------------------------------------

class FullMMStrategy(BaseStrategy):
    """Temari型フルスペックマーケットメイキング戦略"""

    def __init__(self, symbol: str, mode: str, config: SimpleMMConfig | None = None):
        super().__init__(symbol, mode)
        self.config = config or SimpleMMConfig()

        # 価格データ（SimpleMMと同一）
        self._binance_mid: float = 0.0
        self._hl_mid: float = 0.0
        self._hl_mark: float = 0.0
        self._fair_price: float = 0.0
        self._price_valid: bool = False
        self._quote_paused: bool = False
        self._pause_reason: str = ""

        # offset リングバッファ
        self._offset_buf: deque[_PriceSample] = deque(maxlen=_OFFSET_BUFFER_SIZE)

        # ボラティリティ用トレード履歴
        self._trade_buf: deque[_TradeSample] = deque(maxlen=2000)
        self._last_trade_price: float = 0.0
        self._vol_bps: float = _BASE_VOL_BPS

        # ポジション追跡
        self.position_usd: float = 0.0
        self.unrealized_pnl: float = 0.0
        self.position_entry_ts: float = 0.0

        # 安全装置: 状態管理
        self._stopped: bool = False
        self._stopped_reason: str = ""  # "daily_loss" / "volatility_spike"
        self._stopped_at: float = 0.0
        self._stopped_day: int = -1
        self._daily_realized_loss: float = 0.0
        self._daily_reset_day: int = -1
        self._atr_24h_samples: deque[float] = deque(maxlen=1440)
        self._atr_24h_avg: float = 0.0
        self._current_atr: float = 0.0

        # --- Full MM 追加コンポーネント ---
        self._fill_toxicity = FillToxicityTracker()
        self._smc = SmcAnalyzer()

        # マルチレベル設定
        self.num_levels: int = 3
        self.level_spacing_bps: float = 6.0
        self.skew_factor: float = 0.5
        self.max_skew_bps: float = 4.0

    @property
    def name(self) -> str:
        return "full_mm"

    # ------------------------------------------------------------------
    # 価格注入（SimpleMMと同一ロジック）
    # ------------------------------------------------------------------

    def update_prices(self, binance_mid: float, hl_mid: float, hl_mark: float):
        """外部から価格を注入"""
        now = time.time()
        self._binance_mid = binance_mid
        self._hl_mid = hl_mid
        self._hl_mark = hl_mark

        offset = hl_mid - binance_mid
        self._offset_buf.append(_PriceSample(offset=offset, ts=now))

        cutoff = now - _OFFSET_WINDOW_SEC
        window_offsets = [s.offset for s in self._offset_buf if s.ts >= cutoff]

        if not window_offsets:
            self._price_valid = False
            return

        median_offset = self._median(window_offsets)
        self._fair_price = binance_mid + median_offset
        self._price_valid = True

        divergence_bps = abs(self._fair_price - hl_mark) / hl_mark * 10000
        if divergence_bps > self.config.price_divergence_bps:
            self._quote_paused = True
            self._pause_reason = (
                f"price_divergence={divergence_bps:.1f}bps > "
                f"{self.config.price_divergence_bps}bps"
            )
            logger.warning(f"[{self.symbol}] Quote paused: {self._pause_reason}")
        else:
            self._quote_paused = False
            self._pause_reason = ""

    # ------------------------------------------------------------------
    # トレードデータ → ボラティリティ + 逆選択チェック
    # ------------------------------------------------------------------

    def on_trade(self, price: float, size: float, timestamp: float):
        """ボラティリティ計算 + 逆選択の5秒後チェック"""
        if self._last_trade_price > 0 and price > 0:
            log_ret = math.log(price / self._last_trade_price)
            self._trade_buf.append(_TradeSample(log_return=log_ret, ts=timestamp))
        self._last_trade_price = price

        self._update_volatility(timestamp)
        self._fill_toxicity.check_toxicity(price, timestamp)

    def _update_volatility(self, now: float):
        cutoff = now - _VOL_WINDOW_SEC
        returns = [s.log_return for s in self._trade_buf if s.ts >= cutoff]

        if len(returns) < 5:
            self._vol_bps = _BASE_VOL_BPS
            return

        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        self._vol_bps = math.sqrt(variance) * 10000

    def _vol_multiplier(self) -> float:
        if self._vol_bps <= 0:
            return 1.0
        raw = self._vol_bps / _BASE_VOL_BPS
        return max(_VOL_MULT_MIN, min(raw, _VOL_MULT_MAX))

    # ------------------------------------------------------------------
    # 約定記録 → 逆選択トラッカー
    # ------------------------------------------------------------------

    def record_fill(self, side: str, price: float, size: float, timestamp: float):
        """約定記録 → 逆選択トラッカーに登録"""
        self._fill_toxicity.record_fill(price=price, side=side, timestamp=timestamp)
        logger.info(
            f"[{self.symbol}] Fill recorded: {side} {size:.4f} @ {price:.2f}"
        )

    # ------------------------------------------------------------------
    # SMC キャンドル更新
    # ------------------------------------------------------------------

    def update_candles(self, candles_15m: list, candles_4h: list):
        """SMC分析用キャンドル更新"""
        self._smc.update(candles_15m, candles_4h)

    # ------------------------------------------------------------------
    # 安全装置
    # ------------------------------------------------------------------

    def should_stop_loss(self) -> bool:
        """含み損が資金のposition_stop_loss_pct%に達したか"""
        threshold = self.config.initial_balance * self.config.position_stop_loss_pct / 100
        return self.unrealized_pnl < -threshold

    def is_daily_limit_reached(self) -> bool:
        """1日の累計実現損が上限に達したか"""
        threshold = self.config.initial_balance * self.config.daily_loss_limit_pct / 100
        return self._daily_realized_loss >= threshold

    def is_volatility_spike(self) -> bool:
        """ATRが24時間平均のatr_spike_multiplier倍以上か"""
        if self._atr_24h_avg <= 0 or self._current_atr <= 0:
            return False
        return self._current_atr >= self._atr_24h_avg * self.config.atr_spike_multiplier

    def update_atr(self, atr_value: float):
        """外部からATR値を注入（1分ごとに呼ばれる想定）"""
        self._current_atr = atr_value
        self._atr_24h_samples.append(atr_value)
        if len(self._atr_24h_samples) > 0:
            self._atr_24h_avg = sum(self._atr_24h_samples) / len(self._atr_24h_samples)

    def record_realized_loss(self, amount: float):
        """実現損を記録（外部から呼ばれる）。amountは正の値で損失額"""
        self._daily_realized_loss += amount
        logger.info(
            f"[{self.symbol}] Realized loss recorded: ${amount:.2f}, "
            f"daily total: ${self._daily_realized_loss:.2f}"
        )

    def _stop(self, reason: str):
        """安全装置による停止"""
        now = time.time()
        self._stopped = True
        self._stopped_reason = reason
        self._stopped_at = now
        self._stopped_day = int(now // _SECONDS_PER_DAY)
        logger.warning(
            f"[{self.symbol}] Safety stop: {reason} | "
            f"daily_loss=${self._daily_realized_loss:.2f}, "
            f"atr={self._current_atr:.4f}, atr_24h_avg={self._atr_24h_avg:.4f}"
        )

    def _check_daily_reset(self):
        """UTC 0:00を跨いだら日次損失をリセット"""
        today = int(time.time() // _SECONDS_PER_DAY)
        if today != self._daily_reset_day:
            if self._daily_realized_loss > 0:
                logger.info(
                    f"[{self.symbol}] Daily loss reset: "
                    f"${self._daily_realized_loss:.2f} -> $0.00"
                )
            self._daily_realized_loss = 0.0
            self._daily_reset_day = today

    def _check_recovery(self):
        """停止状態からの復活チェック（日次損失 or ボラ急騰）"""
        now = time.time()
        today = int(now // _SECONDS_PER_DAY)

        # 条件①: UTC翌日になっている
        if today <= self._stopped_day:
            return

        # 条件②: ATRが24h平均の recovery_multiplier 倍以下（相場が落ち着いた）
        if self._atr_24h_avg > 0 and self._current_atr > 0:
            if self._current_atr > self._atr_24h_avg * self.config.atr_recovery_multiplier:
                return

        # 両条件クリア → 復活
        logger.info(
            f"[{self.symbol}] Recovery from '{self._stopped_reason}': "
            f"atr={self._current_atr:.4f}, atr_24h_avg={self._atr_24h_avg:.4f}"
        )
        self._stopped = False
        self._stopped_reason = ""
        self._stopped_at = 0.0
        self._check_daily_reset()

    # ------------------------------------------------------------------
    # マルチレベルクオート算出
    # ------------------------------------------------------------------

    def get_quotes(self) -> dict:
        """マルチレベルのbid/askを計算

        Returns:
            dict: {
                "should_quote": bool,
                "levels": [
                    {"bid_price": float, "bid_size": float,
                     "ask_price": float, "ask_size": float},
                    ...
                ]
            }
        """
        no_quote = {"should_quote": False, "levels": []}

        # 復活チェック（停止中の場合）
        if self._stopped:
            self._check_recovery()
            if self._stopped:
                return no_quote

        # 日次リセット
        self._check_daily_reset()

        # 日次損失上限
        if self.is_daily_limit_reached():
            self._stop("daily_loss")
            return no_quote

        # ボラ急騰
        if self.is_volatility_spike():
            self._stop("volatility_spike")
            return no_quote

        if not self._price_valid or self._quote_paused:
            return no_quote

        if self.unrealized_pnl < -self.config.max_loss_usd:
            logger.warning(
                f"[{self.symbol}] Max loss hit: {self.unrealized_pnl:.2f} < "
                f"-{self.config.max_loss_usd}"
            )
            return no_quote

        fair = self._fair_price

        # --- Combined multiplier ---
        vol_mult = self._vol_multiplier()
        smc_mult = self._smc.multiplier
        toxic_mult = self._fill_toxicity.multiplier
        combined_mult = vol_mult * smc_mult * toxic_mult

        # --- スプレッド計算 ---
        fee_bps = self.config.fee_bps
        raw_spread_bps = max(
            self.config.spread_bps * combined_mult,
            fee_bps * 3,
        )
        half_spread_bps = raw_spread_bps / 2

        # --- Progressive sqrt skew ---
        skew_bps = self._progressive_skew()

        # --- 片側停止フラグ ---
        bid_stopped = self.position_usd >= self.config.max_position_usd
        ask_stopped = self.position_usd <= -self.config.max_position_usd

        # --- マルチレベル注文生成 ---
        levels = []
        for lvl in range(self.num_levels):
            level_offset_bps = lvl * self.level_spacing_bps

            bid_half = max(half_spread_bps + skew_bps + level_offset_bps, fee_bps)
            ask_half = max(half_spread_bps - skew_bps + level_offset_bps, fee_bps)

            bid_price = fair * (1 - bid_half / 10000)
            ask_price = fair * (1 + ask_half / 10000)

            # L0: 80%サイズ、L1+: 120%サイズ
            if lvl == 0:
                size_mult = 0.80
            else:
                size_mult = 1.20
            # サイズをコイン建てに変換（USD / 価格）
            base_size = (self.config.order_size_usd * size_mult / fair) if fair > 0 else 0.0

            bid_size = base_size if not bid_stopped else 0.0
            ask_size = base_size if not ask_stopped else 0.0

            levels.append({
                "bid_price": round(bid_price, 8),
                "bid_size": bid_size,
                "ask_price": round(ask_price, 8),
                "ask_size": ask_size,
            })

        should_quote = any(
            l["bid_size"] > 0 or l["ask_size"] > 0 for l in levels
        )

        return {"should_quote": should_quote, "levels": levels}

    def _progressive_skew(self) -> float:
        """Progressive sqrt skew

        inventoryUnits = positionUsd / orderSizeUsd
        rawSkew = inventoryUnits * skewFactor
        progressiveSkew = sign(raw) * maxSkewBps * sqrt(min(|raw|/maxSkew, 4))
        """
        if self.config.order_size_usd == 0:
            return 0.0

        inventory_units = self.position_usd / self.config.order_size_usd
        raw_skew = inventory_units * self.skew_factor
        sign = 1.0 if raw_skew >= 0 else -1.0
        clamped = min(abs(raw_skew) / self.max_skew_bps, 4.0)
        return sign * self.max_skew_bps * math.sqrt(clamped)

    # ------------------------------------------------------------------
    # 最大保有時間チェック
    # ------------------------------------------------------------------

    def should_force_close(self) -> bool:
        if self.position_usd == 0 or self.position_entry_ts == 0:
            return False
        elapsed = time.time() - self.position_entry_ts
        return elapsed > self.config.max_hold_seconds

    # ------------------------------------------------------------------
    # BaseStrategy インターフェース
    # ------------------------------------------------------------------

    def on_candle(self, candle) -> Signal:
        return Signal(type=SignalType.NONE)

    def ready(self) -> bool:
        return self._price_valid and not self._quote_paused

    def get_state(self) -> dict:
        return {
            "symbol": self.symbol,
            "mode": self.mode,
            "strategy": self.name,
            "fair_price": round(self._fair_price, 4) if self._fair_price else 0,
            "binance_mid": round(self._binance_mid, 4) if self._binance_mid else 0,
            "hl_mid": round(self._hl_mid, 4) if self._hl_mid else 0,
            "hl_mark": round(self._hl_mark, 4) if self._hl_mark else 0,
            "price_valid": self._price_valid,
            "quote_paused": self._quote_paused,
            "pause_reason": self._pause_reason,
            "position_usd": round(self.position_usd, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "vol_bps": round(self._vol_bps, 2),
            "vol_multiplier": round(self._vol_multiplier(), 3),
            "smc_structure": self._smc.structure,
            "smc_multiplier": self._smc.multiplier,
            "toxic_ratio": round(self._fill_toxicity.toxic_ratio, 3),
            "toxic_multiplier": round(self._fill_toxicity.multiplier, 3),
            "combined_multiplier": round(
                self._vol_multiplier() * self._smc.multiplier
                * self._fill_toxicity.multiplier, 3
            ),
            "progressive_skew_bps": round(self._progressive_skew(), 3),
            "num_levels": self.num_levels,
            "offset_samples": len(self._offset_buf),
            "trade_samples": len(self._trade_buf),
            "should_force_close": self.should_force_close(),
            "should_stop_loss": self.should_stop_loss(),
            "stopped": self._stopped,
            "stopped_reason": self._stopped_reason,
            "daily_realized_loss": round(self._daily_realized_loss, 4),
            "current_atr": round(self._current_atr, 6),
            "atr_24h_avg": round(self._atr_24h_avg, 6),
            "atr_samples": len(self._atr_24h_samples),
        }

    # ------------------------------------------------------------------
    # ユーティリティ
    # ------------------------------------------------------------------

    @staticmethod
    def _median(values: list[float]) -> float:
        s = sorted(values)
        n = len(s)
        if n == 0:
            return 0.0
        mid = n // 2
        if n % 2 == 0:
            return (s[mid - 1] + s[mid]) / 2
        return s[mid]
