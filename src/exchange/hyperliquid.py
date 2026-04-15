"""Hyperliquid API接続 — 注文・照会・WebSocket"""

import asyncio
import json
import time
from typing import Any

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

from src.exchange.ws_manager import WSManager
from src.utils.logger import get_logger

log = get_logger("hyperliquid")

HL_WS_URL = "wss://api.hyperliquid.xyz/ws"


class HyperliquidClient:
    """Hyperliquid API クライアント"""

    def __init__(
        self,
        wallet_address: str,
        api_private_key: str,
        account_address: str | None = None,
        is_mainnet: bool = True,
    ):
        self._wallet_address = wallet_address
        self._api_key = api_private_key
        self._account_address = account_address or wallet_address
        self._is_mainnet = is_mainnet

        # SDK初期化
        base_url = constants.MAINNET_API_URL if is_mainnet else constants.TESTNET_API_URL
        self._info = Info(base_url, skip_ws=True)
        self._exchange: Exchange | None = None

        # WebSocket
        self._ws: WSManager | None = None
        self._on_trade_cb = None
        self._on_l2_cb = None

        # メタ情報キャッシュ
        self._meta: dict = {}
        self._sz_decimals: dict[str, int] = {}

    async def connect(self):
        """接続初期化 — meta情報取得 + Exchange SDK初期化"""
        # meta情報でシンボル・tick/lot sizeを取得
        self._meta = self._info.meta()
        for asset in self._meta.get("universe", []):
            self._sz_decimals[asset["name"]] = asset["szDecimals"]
        log.info(f"Loaded {len(self._sz_decimals)} symbols from meta")

        # Exchange SDK（注文用）— dry modeではNone
        if self._api_key:
            self._exchange = Exchange(
                self._info,
                self._api_key,
                account_address=self._account_address,
            )

    def get_sz_decimals(self, symbol: str) -> int:
        return self._sz_decimals.get(symbol, 2)

    def round_size(self, symbol: str, size: float) -> float:
        decimals = self.get_sz_decimals(symbol)
        return round(size, decimals)

    # ── 照会API ──

    def get_mid_price(self, symbol: str) -> float | None:
        """現在の中間価格を取得"""
        try:
            all_mids = self._info.all_mids()
            return float(all_mids.get(symbol, 0))
        except Exception as e:
            log.error(f"Failed to get mid price: {e}")
            return None

    def get_positions(self) -> list[dict]:
        """現在のポジション一覧"""
        try:
            state = self._info.user_state(self._account_address)
            positions = []
            for pos in state.get("assetPositions", []):
                p = pos.get("position", {})
                size = float(p.get("szi", "0"))
                if size != 0:
                    positions.append({
                        "symbol": p.get("coin", ""),
                        "size": size,
                        "entry_price": float(p.get("entryPx", "0")),
                        "unrealized_pnl": float(p.get("unrealizedPnl", "0")),
                        "margin_used": float(p.get("marginUsed", "0")),
                    })
            return positions
        except Exception as e:
            log.error(f"Failed to get positions: {e}")
            return []

    def get_open_orders(self) -> list[dict]:
        """未約定の注文一覧"""
        try:
            return self._info.open_orders(self._account_address)
        except Exception as e:
            log.error(f"Failed to get open orders: {e}")
            return []

    def get_account_balance(self) -> float:
        """アカウント残高（USDC）"""
        try:
            state = self._info.user_state(self._account_address)
            return float(state.get("marginSummary", {}).get("accountValue", "0"))
        except Exception as e:
            log.error(f"Failed to get balance: {e}")
            return 0.0

    def get_candles(self, symbol: str, interval: str, limit: int = 500) -> list[dict]:
        """ヒストリカルキャンドルデータ取得"""
        try:
            end_time = int(time.time() * 1000)
            start_time = end_time - (limit * _interval_to_ms(interval))
            candles = self._info.candles_snapshot(symbol, interval, start_time, end_time)
            return candles
        except Exception as e:
            log.error(f"Failed to get candles: {e}")
            return []

    # ── 注文API ──

    async def place_order(
        self,
        symbol: str,
        is_buy: bool,
        size: float,
        price: float | None = None,
        order_type: str = "limit",
        reduce_only: bool = False,
    ) -> dict | None:
        """注文を出す"""
        if not self._exchange:
            log.warning("Exchange not initialized (dry mode?)")
            return None

        sz = self.round_size(symbol, abs(size))
        if sz <= 0:
            return None

        try:
            if order_type == "limit" and price is not None:
                result = self._exchange.order(
                    symbol, is_buy, sz, price,
                    {"limit": {"tif": "Alo"}},  # Post-Only
                    reduce_only=reduce_only,
                )
            elif order_type == "ioc" and price is not None:
                result = self._exchange.order(
                    symbol, is_buy, sz, price,
                    {"limit": {"tif": "Ioc"}},
                    reduce_only=reduce_only,
                )
            else:
                # Market order (IOC with slippage)
                mid = self.get_mid_price(symbol)
                if mid is None:
                    return None
                slippage = 0.005  # 0.5%
                px = mid * (1 + slippage) if is_buy else mid * (1 - slippage)
                result = self._exchange.order(
                    symbol, is_buy, sz, px,
                    {"limit": {"tif": "Ioc"}},
                    reduce_only=reduce_only,
                )

            log.info(
                f"Order placed: {symbol} {'BUY' if is_buy else 'SELL'} "
                f"{sz} @ {price} ({order_type})",
            )
            return result
        except Exception as e:
            log.error(f"Order failed: {e}")
            return None

    async def cancel_all_orders(self, symbol: str | None = None) -> bool:
        """全注文キャンセル"""
        if not self._exchange:
            return False
        try:
            open_orders = self.get_open_orders()
            for order in open_orders:
                coin = order.get("coin", "")
                if symbol and coin != symbol:
                    continue
                oid = order.get("oid")
                if oid:
                    self._exchange.cancel(coin, oid)
            log.info(f"Cancelled all orders{f' for {symbol}' if symbol else ''}")
            return True
        except Exception as e:
            log.error(f"Cancel failed: {e}")
            return False

    # ── WebSocket ──

    async def start_ws(self, symbol: str, on_trade=None, on_l2=None):
        """WebSocket接続を開始（トレード・板情報）"""
        self._on_trade_cb = on_trade
        self._on_l2_cb = on_l2

        subscribe_msgs = []
        if on_trade:
            subscribe_msgs.append({
                "method": "subscribe",
                "subscription": {"type": "trades", "coin": symbol},
            })
        if on_l2:
            subscribe_msgs.append({
                "method": "subscribe",
                "subscription": {"type": "l2Book", "coin": symbol},
            })

        self._ws = WSManager(
            url=HL_WS_URL,
            name=f"hl-{symbol}",
            on_message=self._handle_ws_message,
            subscribe_msgs=subscribe_msgs,
            ping_interval=30.0,
        )
        asyncio.create_task(self._ws.run())

    async def _handle_ws_message(self, data: dict):
        """WSメッセージをコールバックに振り分け"""
        channel = data.get("channel")
        if channel == "trades" and self._on_trade_cb:
            for trade in data.get("data", []):
                await self._on_trade_cb(trade)
        elif channel == "l2Book" and self._on_l2_cb:
            await self._on_l2_cb(data.get("data", {}))

    @property
    def ws_connected(self) -> bool:
        return self._ws.connected if self._ws else False

    @property
    def ws_last_msg_age_ms(self) -> int:
        return self._ws.last_msg_age_ms if self._ws else -1

    async def close(self):
        if self._ws:
            await self._ws.close()


def _interval_to_ms(interval: str) -> int:
    """キャンドルインターバル文字列をミリ秒に変換"""
    units = {"1m": 60000, "5m": 300000, "15m": 900000, "30m": 1800000,
             "1h": 3600000, "4h": 14400000, "1d": 86400000}
    return units.get(interval, 300000)
