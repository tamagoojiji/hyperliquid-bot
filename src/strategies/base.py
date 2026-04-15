"""戦略の基底クラス"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class SignalType(Enum):
    NONE = "none"
    BUY = "buy"
    SELL = "sell"


@dataclass
class Signal:
    type: SignalType
    price: float = 0.0
    size_usd: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    reason: str = ""


class BaseStrategy(ABC):
    """全戦略の基底クラス"""

    def __init__(self, symbol: str, mode: str):
        self.symbol = symbol
        self.mode = mode  # "dry" or "live"

    @property
    @abstractmethod
    def name(self) -> str:
        """戦略名"""
        ...

    @abstractmethod
    def on_candle(self, candle) -> Signal:
        """新しいキャンドルが確定した時に呼ばれる"""
        ...

    @abstractmethod
    def on_trade(self, price: float, size: float, timestamp: float):
        """トレードデータの更新"""
        ...

    @abstractmethod
    def ready(self) -> bool:
        """十分なデータが溜まって判定可能か"""
        ...
