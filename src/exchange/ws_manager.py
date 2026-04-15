"""WebSocket接続管理 — ping/再接続/指数バックオフ"""

import asyncio
import json
import time
from typing import Callable, Awaitable

import websockets
from websockets.asyncio.client import ClientConnection

from src.utils.logger import get_logger

log = get_logger("ws_manager")


class WSManager:
    """WebSocket接続の管理（ping送信、再接続、指数バックオフ）"""

    def __init__(
        self,
        url: str,
        name: str,
        on_message: Callable[[dict], Awaitable[None]],
        subscribe_msgs: list[dict] | None = None,
        ping_interval: float = 30.0,
        max_backoff: float = 30.0,
    ):
        self.url = url
        self.name = name
        self._on_message = on_message
        self._subscribe_msgs = subscribe_msgs or []
        self._ping_interval = ping_interval
        self._max_backoff = max_backoff
        self._ws: ClientConnection | None = None
        self._connected = False
        self._backoff = 1.0
        self._last_msg_time: float = 0.0

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_msg_age_ms(self) -> int:
        if self._last_msg_time == 0:
            return -1
        return int((time.time() - self._last_msg_time) * 1000)

    async def run(self):
        """接続ループ — 切断時に自動再接続"""
        while True:
            try:
                await self._connect_and_listen()
            except (
                websockets.ConnectionClosed,
                websockets.InvalidURI,
                ConnectionRefusedError,
                OSError,
            ) as e:
                self._connected = False
                log.warning(
                    f"[{self.name}] WS disconnected: {e}. "
                    f"Reconnecting in {self._backoff:.1f}s"
                )
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, self._max_backoff)
            except Exception as e:
                self._connected = False
                log.error(f"[{self.name}] Unexpected WS error: {e}")
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, self._max_backoff)

    async def _connect_and_listen(self):
        async with websockets.connect(self.url) as ws:
            self._ws = ws
            self._connected = True
            self._backoff = 1.0
            log.info(f"[{self.name}] Connected to {self.url}")

            # サブスクリプション送信
            for msg in self._subscribe_msgs:
                await ws.send(json.dumps(msg))
                log.info(f"[{self.name}] Subscribed: {msg.get('type', msg)}")

            # ping送信タスクを起動
            ping_task = asyncio.create_task(self._ping_loop(ws))

            try:
                async for raw in ws:
                    self._last_msg_time = time.time()
                    try:
                        data = json.loads(raw)
                        await self._on_message(data)
                    except json.JSONDecodeError:
                        log.warning(f"[{self.name}] Invalid JSON: {raw[:100]}")
            finally:
                ping_task.cancel()
                self._connected = False

    async def _ping_loop(self, ws: ClientConnection):
        """定期pingでタイムアウト防止"""
        try:
            while True:
                await asyncio.sleep(self._ping_interval)
                try:
                    pong = await ws.ping()
                    await asyncio.wait_for(pong, timeout=10)
                except Exception:
                    log.warning(f"[{self.name}] Ping failed, closing")
                    await ws.close()
                    return
        except asyncio.CancelledError:
            pass

    async def close(self):
        if self._ws:
            await self._ws.close()
            self._connected = False
