"""SQLite persistence layer for Shadow Paper Trading.

Manages virtual accounts, resting orders, fills, snapshots, and settlements
with ACID transactions and connection safety.
"""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Optional


@dataclass
class ShadowOrder:
    order_id: str
    event_id: str
    event_slug: str
    market_id: str
    condition_id: str
    token_id: str
    bracket_name: str
    side: str  # "BUY_YES" or "BUY_NO"
    limit_price: float
    size_shares: float
    cost_usdc: float
    placed_at_utc: str
    status: str = "OPEN"  # OPEN, FILLED, CANCELED, SETTLED
    fill_mode: str = "MAKER_STRICT"  # MAKER_STRICT, MAKER_TOUCH, TAKER
    filled_at_utc: Optional[str] = None
    fill_price: Optional[float] = None
    settled_at_utc: Optional[str] = None
    settled_price: Optional[float] = None  # 1.0 or 0.0
    realized_pnl: Optional[float] = None
    metadata_json: str = "{}"


@dataclass
class AccountSummary:
    initial_bankroll: float
    cash_balance: float
    locked_collateral: float
    realized_pnl: float
    unrealized_pnl: float
    total_equity: float
    open_orders_count: int
    filled_positions_count: int
    settled_orders_count: int


class ShadowStorage:
    def __init__(self, db_path: str | Path = "data/shadow_trading.sqlite") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS shadow_account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    initial_bankroll REAL NOT NULL,
                    cash_balance REAL NOT NULL,
                    locked_collateral REAL NOT NULL DEFAULT 0.0,
                    realized_pnl REAL NOT NULL DEFAULT 0.0,
                    updated_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS shadow_orders (
                    order_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    event_slug TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    condition_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    bracket_name TEXT NOT NULL,
                    side TEXT NOT NULL,
                    limit_price REAL NOT NULL,
                    size_shares REAL NOT NULL,
                    cost_usdc REAL NOT NULL,
                    placed_at_utc TEXT NOT NULL,
                    status TEXT NOT NULL,
                    fill_mode TEXT NOT NULL,
                    filled_at_utc TEXT,
                    fill_price REAL,
                    settled_at_utc TEXT,
                    settled_price REAL,
                    realized_pnl REAL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_orders_status ON shadow_orders(status);
                CREATE INDEX IF NOT EXISTS idx_orders_market_side ON shadow_orders(market_id, side);
                CREATE INDEX IF NOT EXISTS idx_orders_event ON shadow_orders(event_id);

                CREATE TABLE IF NOT EXISTS shadow_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_utc TEXT NOT NULL,
                    total_equity REAL NOT NULL,
                    cash_balance REAL NOT NULL,
                    locked_collateral REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL
                );
                """
            )
            # Initialize default account if not exists
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT OR IGNORE INTO shadow_account (id, initial_bankroll, cash_balance, locked_collateral, realized_pnl, updated_at_utc)
                VALUES (1, 1000.0, 1000.0, 0.0, 0.0, ?)
                """,
                (now,),
            )
            conn.commit()

    def get_account_summary(self) -> AccountSummary:
        with closing(self._connect()) as conn:
            acc = conn.execute("SELECT * FROM shadow_account WHERE id = 1").fetchone()
            if not acc:
                return AccountSummary(1000.0, 1000.0, 0.0, 0.0, 0.0, 1000.0, 0, 0, 0)

            open_count = conn.execute(
                "SELECT COUNT(*) FROM shadow_orders WHERE status = 'OPEN'"
            ).fetchone()[0]
            filled_count = conn.execute(
                "SELECT COUNT(*) FROM shadow_orders WHERE status = 'FILLED'"
            ).fetchone()[0]
            settled_count = conn.execute(
                "SELECT COUNT(*) FROM shadow_orders WHERE status = 'SETTLED'"
            ).fetchone()[0]

            # Compute approximate unrealized PnL from filled orders
            filled_rows = conn.execute(
                "SELECT cost_usdc FROM shadow_orders WHERE status = 'FILLED'"
            ).fetchall()
            unrealized_pnl = 0.0  # By default marked at cost unless market mark updated

            cash = float(acc["cash_balance"])
            locked = float(acc["locked_collateral"])
            realized = float(acc["realized_pnl"])
            initial = float(acc["initial_bankroll"])
            equity = cash + locked + unrealized_pnl

            return AccountSummary(
                initial_bankroll=initial,
                cash_balance=round(cash, 4),
                locked_collateral=round(locked, 4),
                realized_pnl=round(realized, 4),
                unrealized_pnl=round(unrealized_pnl, 4),
                total_equity=round(equity, 4),
                open_orders_count=open_count,
                filled_positions_count=filled_count,
                settled_orders_count=settled_count,
            )

    def has_active_order_for_bracket(self, market_id: str, side: str) -> bool:
        """Returns True if there is already an OPEN or FILLED order on this market & side."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM shadow_orders
                WHERE market_id = ? AND side = ? AND status IN ('OPEN', 'FILLED')
                LIMIT 1
                """,
                (market_id, side),
            ).fetchone()
            return row is not None

    def get_active_risk_for_event(self, event_id: str) -> float:
        """Returns total cost_usdc of all OPEN and FILLED orders for an event."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT SUM(cost_usdc) FROM shadow_orders
                WHERE event_id = ? AND status IN ('OPEN', 'FILLED')
                """,
                (event_id,),
            ).fetchone()
            return float(row[0]) if row and row[0] is not None else 0.0

    def create_order(self, order: ShadowOrder) -> bool:
        """Inserts a new virtual order and locks the required cash collateral.
        
        Returns False if available cash is insufficient or duplicate active order exists.
        """
        with closing(self._connect()) as conn:
            acc = conn.execute("SELECT cash_balance, locked_collateral FROM shadow_account WHERE id = 1").fetchone()
            if not acc:
                return False

            cash = float(acc["cash_balance"])
            if cash < order.cost_usdc:
                return False  # Insufficient cash

            # Check duplicate active
            dup = conn.execute(
                "SELECT 1 FROM shadow_orders WHERE market_id = ? AND side = ? AND status IN ('OPEN', 'FILLED')",
                (order.market_id, order.side),
            ).fetchone()
            if dup:
                return False

            now = datetime.now(timezone.utc).isoformat()
            new_cash = cash - order.cost_usdc
            new_locked = float(acc["locked_collateral"]) + order.cost_usdc

            conn.execute(
                """
                UPDATE shadow_account
                SET cash_balance = ?, locked_collateral = ?, updated_at_utc = ?
                WHERE id = 1
                """,
                (new_cash, new_locked, now),
            )
            conn.execute(
                """
                INSERT INTO shadow_orders (
                    order_id, event_id, event_slug, market_id, condition_id, token_id,
                    bracket_name, side, limit_price, size_shares, cost_usdc,
                    placed_at_utc, status, fill_mode, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.order_id,
                    order.event_id,
                    order.event_slug,
                    order.market_id,
                    order.condition_id,
                    order.token_id,
                    order.bracket_name,
                    order.side,
                    order.limit_price,
                    order.size_shares,
                    order.cost_usdc,
                    order.placed_at_utc,
                    order.status,
                    order.fill_mode,
                    order.metadata_json,
                ),
            )
            conn.commit()
            return True

    def get_open_orders(self) -> list[ShadowOrder]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM shadow_orders WHERE status = 'OPEN'").fetchall()
            return [self._row_to_order(r) for r in rows]

    def get_filled_orders(self) -> list[ShadowOrder]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM shadow_orders WHERE status = 'FILLED'").fetchall()
            return [self._row_to_order(r) for r in rows]

    def get_all_orders(self, limit: int = 100) -> list[ShadowOrder]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM shadow_orders ORDER BY placed_at_utc DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._row_to_order(r) for r in rows]

    def mark_order_filled(
        self,
        order_id: str,
        fill_price: float,
        filled_at_utc: Optional[str] = None,
    ) -> bool:
        """Transitions order from OPEN to FILLED."""
        now = filled_at_utc or datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            cur = conn.execute(
                """
                UPDATE shadow_orders
                SET status = 'FILLED', fill_price = ?, filled_at_utc = ?
                WHERE order_id = ? AND status = 'OPEN'
                """,
                (fill_price, now, order_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def cancel_order(self, order_id: str) -> bool:
        """Cancels an OPEN order and refunds the locked collateral."""
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            order = conn.execute(
                "SELECT cost_usdc FROM shadow_orders WHERE order_id = ? AND status = 'OPEN'",
                (order_id,),
            ).fetchone()
            if not order:
                return False

            cost = float(order["cost_usdc"])
            acc = conn.execute("SELECT cash_balance, locked_collateral FROM shadow_account WHERE id = 1").fetchone()
            if acc:
                new_cash = float(acc["cash_balance"]) + cost
                new_locked = max(0.0, float(acc["locked_collateral"]) - cost)
                conn.execute(
                    "UPDATE shadow_account SET cash_balance = ?, locked_collateral = ?, updated_at_utc = ? WHERE id = 1",
                    (new_cash, new_locked, now),
                )

            conn.execute(
                "UPDATE shadow_orders SET status = 'CANCELED' WHERE order_id = ?",
                (order_id,),
            )
            conn.commit()
            return True

    def settle_order(
        self,
        order_id: str,
        terminal_price: float,  # 1.0 if our side won, 0.0 if lost
        settled_at_utc: Optional[str] = None,
    ) -> Optional[float]:
        """Settles an order (FILLED or OPEN).
        
        If FILLED:
          Realized PnL = (terminal_price - fill_price) * size_shares
          Payout returned to cash = terminal_price * size_shares
          Locked collateral reduced by cost_usdc
        If OPEN:
          Order was never filled. Simply cancel and refund collateral (Realized PnL = 0.0).
        """
        now = settled_at_utc or datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            order = conn.execute(
                "SELECT status, fill_price, limit_price, size_shares, cost_usdc FROM shadow_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if not order or order["status"] in ("SETTLED", "CANCELED"):
                return None

            status = order["status"]
            cost = float(order["cost_usdc"])
            size = float(order["size_shares"])

            acc = conn.execute(
                "SELECT cash_balance, locked_collateral, realized_pnl FROM shadow_account WHERE id = 1"
            ).fetchone()
            if not acc:
                return None

            cash = float(acc["cash_balance"])
            locked = float(acc["locked_collateral"])
            tot_pnl = float(acc["realized_pnl"])

            if status == "OPEN":
                # Unfilled before market resolved: refund collateral
                new_cash = cash + cost
                new_locked = max(0.0, locked - cost)
                realized_pnl = 0.0

                conn.execute(
                    "UPDATE shadow_account SET cash_balance = ?, locked_collateral = ?, updated_at_utc = ? WHERE id = 1",
                    (new_cash, new_locked, now),
                )
                conn.execute(
                    """
                    UPDATE shadow_orders
                    SET status = 'CANCELED', settled_at_utc = ?, settled_price = ?, realized_pnl = 0.0
                    WHERE order_id = ?
                    """,
                    (now, terminal_price, order_id),
                )
                conn.commit()
                return 0.0

            # Status is FILLED
            fill_p = float(order["fill_price"] or order["limit_price"])
            payout = terminal_price * size
            realized_pnl = round(payout - cost, 4)

            new_cash = cash + payout
            new_locked = max(0.0, locked - cost)
            new_pnl = round(tot_pnl + realized_pnl, 4)

            conn.execute(
                """
                UPDATE shadow_account
                SET cash_balance = ?, locked_collateral = ?, realized_pnl = ?, updated_at_utc = ?
                WHERE id = 1
                """,
                (new_cash, new_locked, new_pnl, now),
            )
            conn.execute(
                """
                UPDATE shadow_orders
                SET status = 'SETTLED', settled_at_utc = ?, settled_price = ?, realized_pnl = ?
                WHERE order_id = ?
                """,
                (now, terminal_price, realized_pnl, order_id),
            )
            conn.commit()
            return realized_pnl

    def _row_to_order(self, row: sqlite3.Row) -> ShadowOrder:
        return ShadowOrder(
            order_id=row["order_id"],
            event_id=row["event_id"],
            event_slug=row["event_slug"],
            market_id=row["market_id"],
            condition_id=row["condition_id"],
            token_id=row["token_id"],
            bracket_name=row["bracket_name"],
            side=row["side"],
            limit_price=float(row["limit_price"]),
            size_shares=float(row["size_shares"]),
            cost_usdc=float(row["cost_usdc"]),
            placed_at_utc=row["placed_at_utc"],
            status=row["status"],
            fill_mode=row["fill_mode"],
            filled_at_utc=row["filled_at_utc"],
            fill_price=float(row["fill_price"]) if row["fill_price"] is not None else None,
            settled_at_utc=row["settled_at_utc"],
            settled_price=float(row["settled_price"]) if row["settled_price"] is not None else None,
            realized_pnl=float(row["realized_pnl"]) if row["realized_pnl"] is not None else None,
            metadata_json=row["metadata_json"] or "{}",
        )
