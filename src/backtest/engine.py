"""バックテストエンジン — 過去キャンドルで戦略を回しトレードを記録"""

from dataclasses import dataclass, field

from src.strategies.base import BaseStrategy, Signal, SignalType
from src.data.candle_builder import Candle


@dataclass
class Trade:
    entry_ts: float
    exit_ts: float
    side: str            # "buy" / "sell"
    entry_price: float
    exit_price: float
    size_usd: float
    reason: str          # "take_profit" / "stop_loss" / "forced"
    fee_usd: float
    pnl_usd: float       # 手数料控除後

    @property
    def hold_seconds(self) -> float:
        return self.exit_ts - self.entry_ts


def _walk_intrabar(candle: Candle, strategy: BaseStrategy) -> "Trade | None":
    """キャンドル内の値動きを on_trade で流して、戦略内部のSL/TPを発火させる。

    値動き順序:
      陽線（close > open）: open → low → high → close
      陰線（close < open）: open → high → low → close
    """
    # ポジションあるかは戦略の内部状態に依存するが、無位置なら on_trade で何も起きない。
    if candle.close >= candle.open:
        sequence = [candle.open, candle.low, candle.high, candle.close]
    else:
        sequence = [candle.open, candle.high, candle.low, candle.close]

    # 1ティックずつ流して、SL/TPが発火したら ExitEvent を取り出す
    for price in sequence:
        strategy.on_trade(price, 0.0, candle.timestamp)
        exit_evt = strategy.consume_exit_event()
        if exit_evt is not None:
            return exit_evt  # type: ignore (Trade風だがExitEvent)
    return None


def run_backtest(
    strategy: BaseStrategy,
    entry_candles: list[Candle],
    filter_candles: list[Candle] | None = None,
    *,
    maker_bps: float = 1.5,
    taker_bps: float = 4.5,
    initial_balance: float = 100.0,
) -> dict:
    """バックテスト実行。

    Args:
        strategy: 評価対象戦略
        entry_candles: エントリー判定足
        filter_candles: フィルター足（5分足戦略のみ。30分足リスト）
        maker_bps / taker_bps: 手数料
        initial_balance: 初期資金（PnL累計用）

    Returns:
        {
          "trades": [Trade...],
          "equity_curve": [(ts, balance)...],
          "first_ts": float,
          "last_ts": float,
        }
    """
    trades: list[Trade] = []
    equity_curve: list[tuple[float, float]] = []
    balance = initial_balance

    # フィルター足をtsで引けるようにインデックス化
    filter_idx = 0
    filter_sorted = filter_candles or []

    # 足のtimestampは始値時刻。確定済みの足だけを渡すため、クローズ時刻で比較する
    def _interval(candles: list[Candle]) -> float:
        return candles[1].timestamp - candles[0].timestamp if len(candles) >= 2 else 0.0

    entry_int = _interval(entry_candles)
    filter_int = _interval(filter_sorted)

    # オープンポジション管理
    open_entry_ts: float | None = None
    open_entry_price: float = 0.0
    open_side: str = ""
    open_size_usd: float = 0.0
    open_is_maker: bool = False

    for candle in entry_candles:
        # 確定済みフィルター足を先に流す（フィルター足クローズ ≤ エントリー足クローズ）
        while (filter_idx < len(filter_sorted)
               and filter_sorted[filter_idx].timestamp + filter_int
               <= candle.timestamp + entry_int):
            fc = filter_sorted[filter_idx]
            if hasattr(strategy, "on_filter_candle"):
                strategy.on_filter_candle(fc)
            filter_idx += 1

        # まずキャンドル内の値動きで既存ポジションのSL/TP判定
        if open_entry_ts is not None:
            exit_evt = _walk_intrabar(candle, strategy)
            if exit_evt is not None:
                exit_price = exit_evt.exit_price
                exit_fee_bps = maker_bps if exit_evt.is_maker else taker_bps
                entry_fee_bps = maker_bps if open_is_maker else taker_bps
                entry_fee = open_size_usd * (entry_fee_bps / 10_000.0)
                exit_fee = open_size_usd * (exit_fee_bps / 10_000.0)
                fee_total = entry_fee + exit_fee
                if open_side == "buy":
                    gross = open_size_usd * (exit_price / open_entry_price - 1.0)
                else:
                    gross = open_size_usd * (1.0 - exit_price / open_entry_price)
                pnl = gross - fee_total
                balance += pnl
                trades.append(Trade(
                    entry_ts=open_entry_ts,
                    exit_ts=candle.timestamp,
                    side=open_side,
                    entry_price=open_entry_price,
                    exit_price=exit_price,
                    size_usd=open_size_usd,
                    reason=exit_evt.reason,
                    fee_usd=fee_total,
                    pnl_usd=pnl,
                ))
                equity_curve.append((candle.timestamp, balance))
                open_entry_ts = None
                open_side = ""
                open_entry_price = 0.0
                open_size_usd = 0.0
                open_is_maker = False

        # キャンドル確定 → エントリーシグナル判定
        signal: Signal = strategy.on_candle(candle)

        # 新規エントリー（既存ポジが無いとき）
        if signal.type in (SignalType.BUY, SignalType.SELL) and open_entry_ts is None:
            open_entry_ts = candle.timestamp
            open_entry_price = signal.price or candle.close
            open_side = "buy" if signal.type == SignalType.BUY else "sell"
            open_size_usd = signal.size_usd or 10.0
            open_is_maker = signal.is_maker

    # 期間終了時に未決済 → 強制クローズ（成行扱い）
    if open_entry_ts is not None and entry_candles:
        last = entry_candles[-1]
        exit_price = last.close
        entry_fee_bps = maker_bps if open_is_maker else taker_bps
        entry_fee = open_size_usd * (entry_fee_bps / 10_000.0)
        exit_fee = open_size_usd * (taker_bps / 10_000.0)
        fee_total = entry_fee + exit_fee
        if open_side == "buy":
            gross = open_size_usd * (exit_price / open_entry_price - 1.0)
        else:
            gross = open_size_usd * (1.0 - exit_price / open_entry_price)
        pnl = gross - fee_total
        balance += pnl
        trades.append(Trade(
            entry_ts=open_entry_ts,
            exit_ts=last.timestamp,
            side=open_side,
            entry_price=open_entry_price,
            exit_price=exit_price,
            size_usd=open_size_usd,
            reason="forced_eob",
            fee_usd=fee_total,
            pnl_usd=pnl,
        ))
        equity_curve.append((last.timestamp, balance))

    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "first_ts": entry_candles[0].timestamp if entry_candles else 0.0,
        "last_ts": entry_candles[-1].timestamp if entry_candles else 0.0,
        "initial_balance": initial_balance,
        "final_balance": balance,
    }
