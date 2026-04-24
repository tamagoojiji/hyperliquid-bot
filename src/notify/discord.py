import aiohttp
import asyncio
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))


class DiscordNotifier:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self._queue: asyncio.Queue = asyncio.Queue()
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None

    async def start(self):
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._sender_loop())

    async def _sender_loop(self):
        while True:
            msg = await self._queue.get()
            try:
                await self._send(msg)
            except Exception as e:
                print(f"Discord notification error: {e}")
            await asyncio.sleep(0.5)

    async def _send(self, content: str):
        if not self._session or not self.webhook_url:
            return
        for attempt in range(3):
            async with self._session.post(self.webhook_url, json={"content": content}) as resp:
                if resp.status == 204:
                    return
                if resp.status == 429:
                    retry_after = 2.0 * (attempt + 1)
                    await asyncio.sleep(retry_after)
                    continue
                print(f"Discord webhook failed: {resp.status}")
                return

    def _ts(self) -> str:
        return datetime.now(JST).strftime("%H:%M:%S")

    async def notify_startup(self, strategy: str, mode: str, symbol: str, balance: float):
        mode_label = "ドライラン" if mode == "dry" else "本番"
        msg = f"\U0001f7e2 `{strategy}` `{symbol}` {mode_label}開始"
        await self._queue.put(msg)

    async def notify_shutdown(self, reason: str):
        await self._queue.put(f"\U0001f534 停止: {reason}")

    async def notify_entry(self, strategy: str, symbol: str, side: str, price: float, size: float,
                           state: dict | None = None, balance: float = 100.0):
        arrow = "\U0001f4c8" if side.upper() == "BUY" else "\U0001f4c9"
        side_label = "ロング" if side.upper() == "BUY" else "ショート"
        sl = state.get("stop_loss", 0) if state else 0
        tp = state.get("take_profit", 0) if state else 0
        leverage = size / balance if balance > 0 else 0
        msg = (
            f"{arrow} **{strategy} {symbol} {side_label}** [{self._ts()}]\n"
            f"価格: `${price:,.2f}` | 金額: `${size:,.2f}` | レバ: `{leverage:.1f}x`"
        )
        if sl > 0 or tp > 0:
            msg += f"\n損切: `${sl:,.2f}` | 利確: `${tp:,.2f}`"
        await self._queue.put(msg)

    async def notify_exit(self, strategy: str, symbol: str, side: str, price: float,
                          size: float, pnl: float, hold_time: str,
                          total_pnl: float = 0.0, initial_balance: float = 100.0):
        if pnl >= 0:
            emoji = "\U0001f4b0"
            result = "利確"
        else:
            emoji = "\U0001f4c9"
            result = "損切"
        pnl_sign = "+" if pnl >= 0 else ""
        total_sign = "+" if total_pnl >= 0 else ""
        total_pct = total_pnl / initial_balance * 100 if initial_balance > 0 else 0
        pct_sign = "+" if total_pct >= 0 else ""
        msg = (
            f"{emoji} **{result} {strategy} {symbol}** [{self._ts()}]\n"
            f"決済価格: `${price:,.2f}` | 損益: `{pnl_sign}${pnl:,.4f}`\n"
            f"累計: `{total_sign}${total_pnl:,.2f}` (`{pct_sign}{total_pct:.1f}%`) | 保有: {hold_time}"
        )
        await self._queue.put(msg)

    async def notify_stop_loss(self, loss_amount: float, total_pnl: float):
        msg = (
            f"\U0001f6a8 **最大損失到達** 損失: `${loss_amount:,.2f}` | 累計: `${total_pnl:,.2f}`\n"
            f"新規エントリー停止"
        )
        await self._queue.put(msg)

    async def notify_error(self, error_msg: str, retry_info: str):
        msg = f"\u26a0\ufe0f `{error_msg}` ({retry_info})"
        await self._queue.put(msg)

    async def notify_daily_summary(self, summary: dict):
        net = summary.get("net_pnl", 0)
        gross = summary.get("total_pnl", 0)
        fees = summary.get("total_fees", 0)
        funding = summary.get("total_funding", 0)
        emoji = "\U0001f4b0" if net >= 0 else "\U0001f4c9"
        pnl_sign = "+" if net >= 0 else ""
        msg = (
            f"\U0001f4ca **日次レポート** `{summary.get('strategy', '?')}`\n"
            f"取引: `{summary.get('trade_count', 0)}回` "
            f"(勝{summary.get('win_count', 0)} 負{summary.get('loss_count', 0)}) "
            f"勝率`{summary.get('win_rate', 0):.0f}%`\n"
            f"粗損益: `${gross:,.4f}` | 手数料: `-${fees:,.4f}` | funding: `${-funding:,.4f}`\n"
            f"{emoji} 純損益: `{pnl_sign}${net:,.4f}`"
        )
        await self._queue.put(msg)

    async def notify_health(self, strategy: str, ws_connected: bool, position_info: str):
        ws_icon = "\U0001f7e2" if ws_connected else "\U0001f534"
        msg = f"\U0001f3e5 `{strategy}` 接続{ws_icon} ポジ: {position_info}"
        await self._queue.put(msg)

    async def notify_mm_quote(self, strategy: str, symbol: str, quotes: dict,
                               state: dict | None = None):
        pass  # MM quoteの定期通知は不要

    async def close(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()
