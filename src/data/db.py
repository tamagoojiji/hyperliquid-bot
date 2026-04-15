import aiosqlite
import asyncio
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = Path("/app/data/bot.db")


class Database:
    def __init__(self):
        self._db: aiosqlite.Connection | None = None
        self._queue: asyncio.Queue = asyncio.Queue()

    async def connect(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(DB_PATH))
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._create_tables()

    async def _create_tables(self):
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                strategy TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                order_type TEXT NOT NULL,
                status TEXT NOT NULL,
                hl_order_id TEXT
            );

            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                order_id INTEGER,
                strategy TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                fee REAL NOT NULL DEFAULT 0,
                realized_pnl REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                strategy TEXT NOT NULL,
                symbol TEXT NOT NULL,
                size REAL NOT NULL,
                entry_price REAL NOT NULL,
                unrealized_pnl REAL NOT NULL DEFAULT 0,
                margin_used REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS state_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                strategy TEXT NOT NULL,
                data TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS heartbeat (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                strategy TEXT NOT NULL,
                ws_connected INTEGER NOT NULL,
                last_quote_age_ms INTEGER NOT NULL,
                error_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS shadow_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                strategy TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                signal_price REAL NOT NULL,
                would_fill_price REAL NOT NULL,
                size REAL NOT NULL,
                estimated_pnl REAL NOT NULL DEFAULT 0,
                fill_model TEXT NOT NULL
            );
        """)

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    async def insert_order(
        self,
        strategy: str,
        symbol: str,
        side: str,
        price: float,
        size: float,
        order_type: str,
        status: str,
        hl_order_id: str | None = None,
    ) -> int:
        ts = self._now()
        cursor = await self._db.execute(
            "INSERT INTO orders (timestamp, strategy, symbol, side, price, size, order_type, status, hl_order_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, strategy, symbol, side, price, size, order_type, status, hl_order_id),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def insert_fill(
        self,
        order_id: int | None,
        strategy: str,
        symbol: str,
        side: str,
        price: float,
        size: float,
        fee: float,
        realized_pnl: float,
    ) -> int:
        ts = self._now()
        cursor = await self._db.execute(
            "INSERT INTO fills (timestamp, order_id, strategy, symbol, side, price, size, fee, realized_pnl) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, order_id, strategy, symbol, side, price, size, fee, realized_pnl),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def insert_position(
        self,
        strategy: str,
        symbol: str,
        size: float,
        entry_price: float,
        unrealized_pnl: float,
        margin_used: float,
    ) -> int:
        ts = self._now()
        cursor = await self._db.execute(
            "INSERT INTO positions (timestamp, strategy, symbol, size, entry_price, unrealized_pnl, margin_used) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, strategy, symbol, size, entry_price, unrealized_pnl, margin_used),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def insert_state_snapshot(self, strategy: str, data: dict) -> int:
        ts = self._now()
        cursor = await self._db.execute(
            "INSERT INTO state_snapshots (timestamp, strategy, data) VALUES (?, ?, ?)",
            (ts, strategy, json.dumps(data)),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def insert_heartbeat(
        self,
        strategy: str,
        ws_connected: bool,
        last_quote_age_ms: int,
        error_count: int,
    ) -> int:
        ts = self._now()
        cursor = await self._db.execute(
            "INSERT INTO heartbeat (timestamp, strategy, ws_connected, last_quote_age_ms, error_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, strategy, int(ws_connected), last_quote_age_ms, error_count),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def insert_shadow_fill(
        self,
        strategy: str,
        symbol: str,
        side: str,
        signal_price: float,
        would_fill_price: float,
        size: float,
        estimated_pnl: float,
        fill_model: str,
    ) -> int:
        ts = self._now()
        cursor = await self._db.execute(
            "INSERT INTO shadow_fills (timestamp, strategy, symbol, side, signal_price, would_fill_price, size, estimated_pnl, fill_model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, strategy, symbol, side, signal_price, would_fill_price, size, estimated_pnl, fill_model),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def get_daily_summary(self, strategy: str) -> dict:
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()

        cursor = await self._db.execute(
            "SELECT price, size, side, fee, realized_pnl FROM fills "
            "WHERE strategy = ? AND timestamp >= ?",
            (strategy, today_start),
        )
        rows = await cursor.fetchall()

        if not rows:
            return {
                "strategy": strategy,
                "trade_count": 0,
                "win_count": 0,
                "loss_count": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "total_fees": 0.0,
                "net_pnl": 0.0,
                "total_volume": 0.0,
                "max_drawdown": 0.0,
            }

        trade_count = len(rows)
        win_count = sum(1 for r in rows if r[4] > 0)
        loss_count = sum(1 for r in rows if r[4] < 0)
        total_pnl = sum(r[4] for r in rows)
        total_fees = sum(r[3] for r in rows)
        total_volume = sum(r[1] * r[0] for r in rows)

        # max drawdown calculation (running cumulative PnL)
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for r in rows:
            cumulative += r[4] - r[3]
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        return {
            "strategy": strategy,
            "trade_count": trade_count,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": (win_count / trade_count * 100) if trade_count > 0 else 0.0,
            "total_pnl": round(total_pnl, 4),
            "total_fees": round(total_fees, 4),
            "net_pnl": round(total_pnl - total_fees, 4),
            "total_volume": round(total_volume, 2),
            "max_drawdown": round(max_dd, 4),
        }

    async def get_latest_state(self, strategy: str) -> dict | None:
        cursor = await self._db.execute(
            "SELECT data FROM state_snapshots WHERE strategy = ? ORDER BY id DESC LIMIT 1",
            (strategy,),
        )
        row = await cursor.fetchone()
        if row:
            return json.loads(row[0])
        return None

    async def close(self):
        if self._db:
            await self._db.close()
