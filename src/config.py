"""設定管理 — 環境変数 + 戦略パラメータ"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class HLConfig:
    """Hyperliquid接続設定"""
    wallet_address_a: str = ""       # BTC用ウォレットアドレス
    api_private_key_a: str = ""      # BTC用API Wallet秘密鍵
    wallet_address_b: str = ""       # SOL用ウォレットアドレス
    api_private_key_b: str = ""      # SOL用API Wallet秘密鍵
    mainnet: bool = True

    @classmethod
    def from_env(cls) -> "HLConfig":
        return cls(
            wallet_address_a=os.getenv("HL_WALLET_ADDRESS_A", ""),
            api_private_key_a=os.getenv("HL_API_PRIVATE_KEY_A", ""),
            wallet_address_b=os.getenv("HL_WALLET_ADDRESS_B", ""),
            api_private_key_b=os.getenv("HL_API_PRIVATE_KEY_B", ""),
        )


@dataclass
class RSI30Config:
    """RSI 30-30 戦略パラメータ"""
    # タイムフレーム（全指標を30分足で計算）
    entry_tf_seconds: int = 1800      # 30分足

    # RSI Channel
    rsi_period: int = 14
    rsi_ob_level: float = 70.0
    rsi_os_level: float = 30.0
    rsi_ema_smooth: int = 1

    # ボリンジャーバンド
    bb_period: int = 20
    bb_multiplier: float = 2.0

    # EMA（全て30分足）
    ema_period: int = 9               # 短期EMA（反転確認用）
    trend_ema_period: int = 200       # トレンドEMA（方向フィルター用）

    # ATR（損切り・利確）
    atr_period: int = 14
    atr_sl_multiplier: float = 1.5    # 損切り = ATR × 1.5
    rr_ratio: float = 2.0             # リスクリワード比

    # リスク管理
    order_size_usd: float = 10.0
    max_position_usd: float = 30.0
    max_loss_usd: float = 20.0
    max_concurrent_positions: int = 1


@dataclass
class SimpleMMConfig:
    """シンプルMM 戦略パラメータ"""
    spread_bps: float = 15.0          # BTC用デフォルト
    order_size_usd: float = 10.0
    max_position_usd: float = 30.0
    max_loss_usd: float = 20.0
    update_interval_ms: int = 1000
    num_levels: int = 1
    fee_bps: float = 1.5
    min_spread_fee_multiplier: float = 3.0  # 最小スプレッド = fee × 3
    max_hold_seconds: int = 300       # IOCクローズまでの最大保有時間
    price_divergence_bps: float = 50.0  # BinanceとHL乖離閾値

    # 安全装置
    initial_balance: float = 100.0        # 初期資金（USD）
    position_stop_loss_pct: float = 5.0   # 含み損が資金の何%で損切り
    daily_loss_limit_pct: float = 10.0    # 1日の累計実現損上限（資金の%）
    atr_spike_multiplier: float = 3.0     # ATR急騰判定（24h平均の何倍）
    atr_recovery_multiplier: float = 2.0  # ATR復活条件（24h平均の何倍以下）


# ペア別MMパラメータオーバーライド
MM_OVERRIDES: dict[str, dict] = {
    "SOL": {
        "spread_bps": 25.0,
        "order_size_usd": 8.0,
        "max_position_usd": 25.0,
        "max_loss_usd": 15.0,
    },
}


@dataclass
class BotConfig:
    """Bot全体の設定"""
    strategy: str = "rsi30"           # rsi30 / simple_mm / full_mm
    symbol: str = "BTC"
    mode: str = "dry"                 # dry / live
    hl: HLConfig = field(default_factory=HLConfig.from_env)
    discord_webhook_url: str = ""
    rsi30: RSI30Config = field(default_factory=RSI30Config)
    simple_mm: SimpleMMConfig = field(default_factory=SimpleMMConfig)

    @classmethod
    def from_env(cls, strategy: str = "rsi30", symbol: str = "BTC",
                 mode: str = "dry") -> "BotConfig":
        config = cls(
            strategy=strategy,
            symbol=symbol,
            mode=mode,
            hl=HLConfig.from_env(),
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", ""),
        )
        # ペア別オーバーライド
        if symbol in MM_OVERRIDES and strategy == "simple_mm":
            for key, val in MM_OVERRIDES[symbol].items():
                if hasattr(config.simple_mm, key):
                    setattr(config.simple_mm, key, val)
        return config

    @property
    def api_private_key(self) -> str:
        """現在のシンボルに対応するAPI Wallet秘密鍵"""
        if self.symbol == "SOL":
            return self.hl.api_private_key_b
        return self.hl.api_private_key_a

    @property
    def account_address(self) -> str:
        """現在のシンボルに対応するウォレットアドレス"""
        if self.symbol == "SOL":
            return self.hl.wallet_address_b
        return self.hl.wallet_address_a
