"""Binance Futures WebSocket — 参照価格（BBO）取得"""

import asyncio
from src.exchange.ws_manager import WSManager
from src.utils.logger import get_logger

log = get_logger("binance_feed")

# Binance Futures WebSocket（公開、APIキー不要）
BINANCE_WS_BASE = "wss://fstream.binance.com/ws"

# HLシンボル → Binanceシンボルのマッピング
SYMBOL_MAP = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
    "SOL": "solusdt",
}


class BinanceFeed:
    """Binance Futuresの最良気配値をリアルタイム取得"""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self._binance_symbol = SYMBOL_MAP.get(symbol, f"{symbol.lower()}usdt")
        self._bid: float = 0.0
        self._ask: float = 0.0
        self._mid: float = 0.0
        self._ws: WSManager | None = None

    @property
    def bid(self) -> float:
        return self._bid

    @property
    def ask(self) -> float:
        return self._ask

    @property
    def mid(self) -> float:
        return self._mid

    @property
    def ready(self) -> bool:
        return self._mid > 0

    @property
    def connected(self) -> bool:
        return self._ws.connected if self._ws else False

    async def start(self):
        """WebSocket接続を開始"""
        url = f"{BINANCE_WS_BASE}/{self._binance_symbol}@bookTicker"
        self._ws = WSManager(
            url=url,
            name=f"binance-{self.symbol}",
            on_message=self._handle_message,
            ping_interval=30.0,
        )
        asyncio.create_task(self._ws.run())
        log.info(f"Binance feed started for {self._binance_symbol}")

    async def _handle_message(self, data: dict):
        """bookTickerメッセージを処理"""
        # Binance bookTicker format: {"u":id,"s":"BTCUSDT","b":"65000.00","B":"1.5","a":"65001.00","A":"0.8"}
        if "b" in data and "a" in data:
            self._bid = float(data["b"])
            self._ask = float(data["a"])
            self._mid = (self._bid + self._ask) / 2

    async def close(self):
        if self._ws:
            await self._ws.close()
