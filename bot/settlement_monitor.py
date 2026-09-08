"""Settlement Monitor for Tweet Markets.

Periodically queries Polymarket Gamma API and XTracker to detect resolved markets,
computes terminal payoffs, and records realized PnL in SQLite.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from bot.shadow_storage import ShadowOrder, ShadowStorage


@dataclass
class SettlementResult:
    order_id: str
    market_id: str
    bracket_name: str
    side: str
    shares: float
    fill_price: float
    terminal_price: float
    realized_pnl: float
    settled_at_utc: str


class SettlementMonitor:
    def __init__(
        self,
        storage: ShadowStorage,
        gamma_fetcher: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
        request_timeout: int = 10,
    ) -> None:
        self.storage = storage
        self._custom_gamma_fetcher = gamma_fetcher
        self.request_timeout = request_timeout

    def check_and_sync_settlements(self) -> list[SettlementResult]:
        """Finds all open or filled orders and checks if their markets have resolved."""
        active_orders = self.storage.get_filled_orders() + self.storage.get_open_orders()
        if not active_orders:
            return []

        # Unique markets
        market_ids = list(dict.fromkeys(o.market_id for o in active_orders))
        results: list[SettlementResult] = []

        for m_id in market_ids:
            market_data = self._fetch_market(m_id)
            if not market_data:
                continue

            resolution = self._check_market_resolution(market_data)
            if not resolution:
                continue

            yes_won = resolution["yes_won"]
            now_iso = datetime.now(timezone.utc).isoformat()

            orders_for_market = [o for o in active_orders if o.market_id == m_id]
            for o in orders_for_market:
                terminal_price = 1.0 if ((o.side == "BUY_YES" and yes_won) or (o.side == "BUY_NO" and not yes_won)) else 0.0
                pnl = self.storage.settle_order(o.order_id, terminal_price=terminal_price, settled_at_utc=now_iso)

                if pnl is not None and o.status == "FILLED":
                    results.append(
                        SettlementResult(
                            order_id=o.order_id,
                            market_id=o.market_id,
                            bracket_name=o.bracket_name,
                            side=o.side,
                            shares=o.size_shares,
                            fill_price=o.fill_price or o.limit_price,
                            terminal_price=terminal_price,
                            realized_pnl=pnl,
                            settled_at_utc=now_iso,
                        )
                    )

        return results

    def _check_market_resolution(self, market_data: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Determines if a market is officially resolved."""
        closed = bool(market_data.get("closed"))
        resolved = bool(market_data.get("resolved")) or (market_data.get("umaResolutionStatus") == "resolved")

        raw_prices = market_data.get("outcomePrices")
        if isinstance(raw_prices, str):
            try:
                prices = [float(p) for p in json.loads(raw_prices)]
            except Exception:
                prices = []
        elif isinstance(raw_prices, list):
            prices = [float(p) for p in raw_prices]
        else:
            prices = []

        if len(prices) < 2:
            return None

        # Check terminal prices
        yes_price = prices[0]
        no_price = prices[1]

        # Resolution criteria:
        # Either marked closed/resolved AND prices are at boundary (>= 0.95 vs <= 0.05),
        # or prices already collapsed to 1.0 / 0.0
        if (closed or resolved) or (yes_price >= 0.99 or no_price >= 0.99):
            if yes_price >= 0.90 and no_price <= 0.10:
                return {"yes_won": True, "yes_price": yes_price, "no_price": no_price}
            if no_price >= 0.90 and yes_price <= 0.10:
                return {"yes_won": False, "yes_price": yes_price, "no_price": no_price}

        return None

    def _fetch_market(self, market_id: str) -> Optional[dict[str, Any]]:
        if self._custom_gamma_fetcher:
            return self._custom_gamma_fetcher(market_id)

        url = f"https://gamma-api.polymarket.com/markets/{market_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data if isinstance(data, dict) else None
        except Exception:
            return None
