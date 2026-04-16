"""シンプルMM戦略 — Binance + Hyperliquid デュアルアンカー"""

import math
import time
from collections import deque
from dataclasses import dataclass, field

from src.config import SimpleMMConfig
from src.strategies.base import BaseStrategy, Signal, SignalType
from src.utils.logger import get_logger

_SECONDS_PER_DAY = 86400

logger = get_logger("simple_mm")

# ボラティリティ計算の基準値
_BASE_VOL_BPS = 5.0
_VOL_MULT_MIN = 0.6
_VOL_MULT_MAX = 3.0

# 在庫スキュー
_MAX_SKEW_BPS = 4.0
_MAX_SKEW_RATIO = 4.0  # sqrt clamp用

# リングバッファサイズ
_OFFSET_BUFFER_SIZE = 500
_OFFSET_WINDOW_SEC = 60.0
_VOL_WINDOW_SEC = 30.0


@dataclass
class _PriceSample:
    offset: float  # hl_mid - binance_mid
    ts: float


@dataclass
class _TradeSample:
    log_return: float
    ts: float


class SimpleMMStrategy(BaseStrategy):
    """Binance+HL BBOデュアルアンカーのシンプルマーケットメイキング戦略"""

    def __init__(self, symbol: str, mode: str, config: SimpleMMConfig | None = None):
        super().__init__(symbol, mode)
        self.config = config or SimpleMMConfig()

        # 価格データ
        self._binance_mid: float = 0.0
        self._hl_mid: float = 0.0
        self._hl_mark: float = 0.0
        self._fair_price: float = 0.0
        self._price_valid: bool = False
        self._quote_paused: bool = False
        self._pause_reason: str = ""

        # offset リングバッファ（hl_mid - binance_mid の時系列）
        self._offset_buf: deque[_PriceSample] = deque(maxlen=_OFFSET_BUFFER_SIZE)

        # ボラティリティ用トレード履歴
        self._trade_buf: deque[_TradeSample] = deque(maxlen=2000)
        self._last_trade_price: float = 0.0
        self._vol_bps: float = _BASE_VOL_BPS

        # ポジション追跡（外部から設定される想定）
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
        self._atr_24h_samples: deque[float] = deque(maxlen=1440)  # 1分×24時間
        self._atr_24h_avg: float = 0.0
        self._current_atr: float = 0.0

    @property
    def name(self) -> str:
        return "simple_mm"

    # ------------------------------------------------------------------
    # 価格注入
    # ------------------------------------------------------------------

    def update_prices(self, binance_mid: float, hl_mid: float, hl_mark: float):
        """外部から価格を注入（main.pyから呼ばれる）"""
        now = time.time()
        self._binance_mid = binance_mid
        self._hl_mid = hl_mid
        self._hl_mark = hl_mark

        # offset をバッファに追加
        offset = hl_mid - binance_mid
        self._offset_buf.append(_PriceSample(offset=offset, ts=now))

        # 60秒ウィンドウ内のサンプルだけで中央値を計算
        cutoff = now - _OFFSET_WINDOW_SEC
        window_offsets = [s.offset for s in self._offset_buf if s.ts >= cutoff]

        if not window_offsets:
            self._price_valid = False
            return

        median_offset = self._median(window_offsets)
        self._fair_price = binance_mid + median_offset
        self._price_valid = True

        # 乖離チェック: |fair_price - hl_mark| > 閾値 → quote停止
        divergence_bps = abs(self._fair_price - hl_mark) / hl_mark * 10000
        if divergence_bps > self.config.price_divergence_bps:
            self._quote_paused = True
            self._pause_reason = (
                f"price_divergence={divergence_bps:.1f}bps > "
                f"{self.config.price_divergence_bps}bps"
            )
            logger.warning(
                f"[{self.symbol}] Quote paused: {self._pause_reason}",
            )
        else:
            self._quote_paused = False
            self._pause_reason = ""

    # ------------------------------------------------------------------
    # トレードデータ → ボラティリティ
    # ------------------------------------------------------------------

    def on_trade(self, price: float, size: float, timestamp: float):
        """ボラティリティ計算用のトレードデータ更新"""
        if self._last_trade_price > 0 and price > 0:
            log_ret = math.log(price / self._last_trade_price)
            self._trade_buf.append(_TradeSample(log_return=log_ret, ts=timestamp))
        self._last_trade_price = price

        # 30秒ウィンドウで標準偏差を更新
        self._update_volatility(timestamp)

    def _update_volatility(self, now: float):
        cutoff = now - _VOL_WINDOW_SEC
        returns = [s.log_return for s in self._trade_buf if s.ts >= cutoff]

        if len(returns) < 5:
            self._vol_bps = _BASE_VOL_BPS
            return

        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        std = math.sqrt(variance)
        self._vol_bps = std * 10000  # 対数リターンをbpsに変換

    def _vol_multiplier(self) -> float:
        """ボラティリティに基づくスプレッド倍率 (0.6x〜3.0x)"""
        if self._vol_bps <= 0:
            return 1.0
        raw = self._vol_bps / _BASE_VOL_BPS
        return max(_VOL_MULT_MIN, min(raw, _VOL_MULT_MAX))

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
        # 日次損失もリセット（翌日になっているため）
        self._check_daily_reset()

    # ------------------------------------------------------------------
    # クオート算出
    # ------------------------------------------------------------------

    def get_quotes(self) -> dict:
        """現在のbid/askを計算して返す

        Returns:
            dict: {
                "bid_price": float,
                "bid_size": float,
                "ask_price": float,
                "ask_size": float,
                "should_quote": bool,
            }
        """
        no_quote = {
            "bid_price": 0.0, "bid_size": 0.0,
            "ask_price": 0.0, "ask_size": 0.0,
            "should_quote": False,
        }

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

        # 準備ができていない / 乖離で停止中
        if not self._price_valid:
            return no_quote
        if self._quote_paused:
            return no_quote

        # 最大損失チェック
        if self.unrealized_pnl < -self.config.max_loss_usd:
            logger.warning(
                f"[{self.symbol}] Max loss hit: {self.unrealized_pnl:.2f} < "
                f"-{self.config.max_loss_usd}"
            )
            return no_quote

        fair = self._fair_price

        # --- ボラティリティ倍率 ---
        vol_mult = self._vol_multiplier()

        # --- 在庫スキュー ---
        skew_bps = self._inventory_skew()

        # --- スプレッド計算 ---
        min_spread_bps = self.config.fee_bps * self.config.min_spread_fee_multiplier
        raw_spread_bps = self.config.spread_bps * vol_mult
        half_spread_bps = max(raw_spread_bps, min_spread_bps) / 2

        # bid/ask 各サイドの半スプレッド（skewで非対称にする）
        bid_half_bps = max(half_spread_bps + skew_bps, self.config.fee_bps)
        ask_half_bps = max(half_spread_bps - skew_bps, self.config.fee_bps)

        bid_price = fair * (1 - bid_half_bps / 10000)
        ask_price = fair * (1 + ask_half_bps / 10000)

        # サイズをコイン建てに変換（USD / 価格）
        bid_size = self.config.order_size_usd / fair if fair > 0 else 0.0
        ask_size = self.config.order_size_usd / fair if fair > 0 else 0.0

        # --- 片側停止（最大ポジションに到達したサイドを止める）---
        if self.position_usd >= self.config.max_position_usd:
            bid_size = 0.0  # ロング上限 → 買い停止
        if self.position_usd <= -self.config.max_position_usd:
            ask_size = 0.0  # ショート上限 → 売り停止

        should_quote = bid_size > 0 or ask_size > 0

        return {
            "bid_price": round(bid_price, 8),
            "bid_size": bid_size,
            "ask_price": round(ask_price, 8),
            "ask_size": ask_size,
            "should_quote": should_quote,
        }

    def _inventory_skew(self) -> float:
        """在庫偏りに応じたスキュー（bps）

        ロングが大きい → skew正 → bidを広げaskを狭める → 売りやすく買いにくく
        """
        if self.config.order_size_usd == 0:
            return 0.0

        raw = self.position_usd / self.config.order_size_usd
        clamped = min(abs(raw) / _MAX_SKEW_RATIO, _MAX_SKEW_RATIO)
        sign = 1.0 if raw >= 0 else -1.0
        return sign * _MAX_SKEW_BPS * math.sqrt(clamped)

    # ------------------------------------------------------------------
    # 最大保有時間チェック
    # ------------------------------------------------------------------

    def should_force_close(self) -> bool:
        """ポジションの保有時間が max_hold_seconds を超過しているか"""
        if self.position_usd == 0 or self.position_entry_ts == 0:
            return False
        elapsed = time.time() - self.position_entry_ts
        return elapsed > self.config.max_hold_seconds

    # ------------------------------------------------------------------
    # BaseStrategy インターフェース
    # ------------------------------------------------------------------

    def on_candle(self, candle) -> Signal:
        """MMではキャンドルベースのシグナルは使わない"""
        return Signal(type=SignalType.NONE)

    def ready(self) -> bool:
        """フェア価格が算出可能か"""
        return self._price_valid and not self._quote_paused

    def get_state(self) -> dict:
        """デバッグ・ログ用の状態dict"""
        return {
            "symbol": self.symbol,
            "mode": self.mode,
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
            "inventory_skew_bps": round(self._inventory_skew(), 3),
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
        """リストの中央値を返す"""
        s = sorted(values)
        n = len(s)
        if n == 0:
            return 0.0
        mid = n // 2
        if n % 2 == 0:
            return (s[mid - 1] + s[mid]) / 2
        return s[mid]
