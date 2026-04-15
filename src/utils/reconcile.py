from src.utils.logger import get_logger

log = get_logger("reconcile")


async def reconcile_on_startup(exchange, db, position_tracker):
    """起動時に取引所の状態とローカルDBを照合する"""
    log.info("Starting reconciliation...")

    # 1. open ordersを取得、不明な注文はキャンセル
    open_orders = await exchange.get_open_orders()
    if open_orders:
        log.info(
            f"Found {len(open_orders)} open orders, cancelling all",
        )
        await exchange.cancel_all_orders()
    else:
        log.info("No open orders found")

    # 2. positionsを取得してローカルと照合
    positions = await exchange.get_positions()
    for pos in positions:
        position_tracker.sync_from_exchange(
            symbol=pos["symbol"],
            size=pos["size"],
            entry_price=pos["entry_price"],
            unrealized_pnl=pos["unrealized_pnl"],
        )
        log.info(
            f"Synced position: {pos['symbol']} size={pos['size']}",
        )

    if not positions:
        log.info("No open positions found")

    # 3. state snapshotを保存
    state = {
        "open_orders": [],
        "positions": [p.__dict__ for p in position_tracker.positions.values()],
    }
    await db.insert_state_snapshot("startup", state)

    log.info("Reconciliation complete")
