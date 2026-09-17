"""Shadow Order Placement & Fill Simulation Engine.

Simulates virtual maker/taker execution against live Polymarket orderbooks
and Data API trades, preventing optimistic cherry-picking.
"""

from __future__ import annotations

import json
import time
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from bot.shadow_storage import ShadowOrder, ShadowStorage
from bot.tweet_poisson_model import BracketEvaluation
from bot.tweet_market_scanner import ScannedTweetEvent
from bot.xtracker_client import TrackingProgress


@dataclass
class EngineConfig:
    min_edge: float = 0.04
    min_entry_price: float = 0.10       # Safety corridor: strict rejection of deep OTM (< 0.10)
    max_entry_price: float = 0.65       # Safety corridor: avoid poor payoff asymmetry (> 0.65)
    max_bracket_risk_usdc: float = 50.0
    min_bracket_risk_usdc: float = 5.0
    max_event_risk_usdc: float = 60.0   # Max total risk allocated to any single event across all brackets
    max_otm_risk_usdc: float = 15.0     # Backwards compatibility clamp
    otm_threshold: float = 0.05         # Backwards compatibility threshold
    max_stale_order_hours: float = 12.0 # Max duration an unfilled resting limit order is allowed to stay
    default_fill_mode: str = "MAKER_STRICT"  # MAKER_STRICT, MAKER_TOUCH, TAKER
    request_timeout: int = 10
    target_risk_multipliers: dict[str, float] = None

    def __post_init__(self):
        if self.target_risk_multipliers is None:
            self.target_risk_multipliers = {
                "cz_binance": 1.25,
                "elonmusk": 1.0,
                "WhiteHouse": 0.8,
                "realDonaldTrump": 0.6,
            }


@dataclass
class TradeCandidate:
    event_id: str
    event_slug: str
    market_id: str
    condition_id: str
    clob_token_id: str
    bracket_name: str
    side: str  # "BUY_YES" or "BUY_NO"
    model_prob: float
    market_prob: float
    net_edge: float
    kelly_fraction: float
    target_price: float


def detect_handle_from_slug(slug: str) -> str:
    """Infers target handle from event slug."""
    slug_lower = slug.lower()
    if "cz" in slug_lower or "binance" in slug_lower:
        return "cz_binance"
    if "trump" in slug_lower or "donald" in slug_lower:
        return "realDonaldTrump"
    if "white-house" in slug_lower or "whitehouse" in slug_lower:
        return "WhiteHouse"
    return "elonmusk"


def candidates_from_scan_results(
    scan_results: list[tuple[ScannedTweetEvent, Optional[TrackingProgress], list[BracketEvaluation]]],
    min_edge: float = 0.04,
    min_entry_price: float = 0.10,
    max_entry_price: float = 0.65,
    top_k_per_event: int = 1,
) -> list[TradeCandidate]:
    """Converts scanner output tuples into structured TradeCandidate items,
    filtered by the entry price safety corridor (0.10 <= px <= 0.65) and
    restricted to top_k_per_event (mutual exclusivity).
    """
    candidates_by_event: dict[str, list[TradeCandidate]] = {}

    for ev, _, evals in scan_results:
        market_by_slug: dict[str, dict[str, Any]] = {}
        market_by_q: dict[str, dict[str, Any]] = {}
        for m in ev.markets:
            if m.get("slug"):
                market_by_slug[m["slug"]] = m
            if m.get("question"):
                market_by_q[m["question"]] = m

        if ev.event_id not in candidates_by_event:
            candidates_by_event[ev.event_id] = []

        for b_eval in evals:
            edge = b_eval.edge_maker or b_eval.edge_buy or 0.0
            if edge < min_edge or b_eval.recommendation == "HOLD":
                continue

            # Find market dict
            m = market_by_slug.get(b_eval.bracket.market_slug or "") or market_by_q.get(b_eval.bracket.name)
            if not m:
                continue

            market_id = str(m.get("id") or "")
            condition_id = str(m.get("conditionId") or "")

            raw_tokens = m.get("clobTokenIds")
            clob_token_id = ""
            if isinstance(raw_tokens, str):
                try:
                    tok_list = json.loads(raw_tokens)
                    if tok_list:
                        clob_token_id = str(tok_list[0])
                except Exception:
                    pass
            elif isinstance(raw_tokens, list) and raw_tokens:
                clob_token_id = str(raw_tokens[0])

            target_px = (
                b_eval.target_maker_price
                or b_eval.market_bid
                or b_eval.market_ask
                or round(b_eval.fair_prob - edge, 3)
            )

            # Safety corridor filter: reject deep OTM (< 0.10) and low-payoff deep ITM (> 0.65)
            if not (min_entry_price <= target_px <= max_entry_price):
                continue

            mkt_prob = b_eval.market_ask or b_eval.market_bid or target_px

            candidates_by_event[ev.event_id].append(
                TradeCandidate(
                    event_id=ev.event_id,
                    event_slug=ev.slug,
                    market_id=market_id,
                    condition_id=condition_id,
                    clob_token_id=clob_token_id,
                    bracket_name=b_eval.bracket.name,
                    side="BUY_YES",
                    model_prob=round(b_eval.fair_prob, 4),
                    market_prob=round(mkt_prob, 4),
                    net_edge=round(edge, 4),
                    kelly_fraction=round(b_eval.kelly_fraction or 0.05, 4),
                    target_price=round(target_px, 3),
                )
            )

    # For each event, select top_k_per_event (sorted by net_edge desc, model_prob desc)
    selected_candidates: list[TradeCandidate] = []
    for ev_id, cands in candidates_by_event.items():
        cands.sort(key=lambda c: (c.net_edge, c.model_prob), reverse=True)
        selected_candidates.extend(cands[:top_k_per_event])

    return selected_candidates


class ShadowEngine:
    def __init__(
        self,
        storage: ShadowStorage,
        config: Optional[EngineConfig] = None,
        book_fetcher: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
        trades_fetcher: Optional[Callable[[str], Optional[list[dict[str, Any]]]]] = None,
    ) -> None:
        self.storage = storage
        self.config = config or EngineConfig()
        self._custom_book_fetcher = book_fetcher
        self._custom_trades_fetcher = trades_fetcher

    def evaluate_and_place_orders(
        self,
        candidates: list[TradeCandidate],
    ) -> list[ShadowOrder]:
        """Scans positive edge candidates and places virtual orders subject to risk limits."""
        placed_orders: list[ShadowOrder] = []
        account = self.storage.get_account_summary()
        available_cash = account.cash_balance

        # Track cumulative committed risk per event in this cycle
        event_committed_risk: dict[str, float] = {}

        for cand in candidates:
            if cand.net_edge < self.config.min_edge:
                continue

            if cand.side not in ("BUY_YES", "BUY_NO"):
                continue

            # Safety corridor filter check
            limit_price = round(cand.target_price, 3)
            if not (self.config.min_entry_price <= limit_price <= self.config.max_entry_price):
                continue

            # Check if order already open or filled for this market
            if self.storage.has_active_order_for_bracket(cand.market_id, cand.side):
                continue

            # Mutual Exclusivity: skip if this event already has ANY active order or was chosen in this cycle
            if cand.event_id in event_committed_risk:
                continue
            if self.storage.has_active_order_for_event(cand.event_id):
                continue

            # Dynamic target risk scaling
            handle = detect_handle_from_slug(cand.event_slug)
            mult = self.config.target_risk_multipliers.get(handle, 1.0)

            # Sizing based on Quarter-Kelly with target multiplier
            risk_usdc = available_cash * max(0.01, cand.kelly_fraction) * mult
            risk_usdc = min(risk_usdc, self.config.max_bracket_risk_usdc * mult)
            risk_usdc = min(risk_usdc, self.config.max_event_risk_usdc)

            if risk_usdc < self.config.min_bracket_risk_usdc:
                continue

            shares = round(risk_usdc / limit_price, 2)
            actual_cost = round(shares * limit_price, 4)
            if actual_cost > available_cash:
                continue

            order = ShadowOrder(
                order_id=f"sh_{uuid.uuid4().hex[:12]}",
                event_id=cand.event_id,
                event_slug=cand.event_slug,
                market_id=cand.market_id,
                condition_id=cand.condition_id,
                token_id=cand.clob_token_id,
                bracket_name=cand.bracket_name,
                side=cand.side,
                limit_price=limit_price,
                size_shares=shares,
                cost_usdc=actual_cost,
                placed_at_utc=datetime.now(timezone.utc).isoformat(),
                status="OPEN",
                fill_mode=self.config.default_fill_mode,
                metadata_json=json.dumps(
                    {
                        "model_prob": cand.model_prob,
                        "market_prob": cand.market_prob,
                        "net_edge": cand.net_edge,
                        "kelly_fraction": cand.kelly_fraction,
                    }
                ),
            )

            if self.storage.create_order(order):
                placed_orders.append(order)
                available_cash -= actual_cost
                event_committed_risk[cand.event_id] = actual_cost

        return placed_orders

    def cancel_stale_orders(self, max_age_hours: Optional[float] = None) -> list[ShadowOrder]:
        """Cancels open resting limit orders that exceed max_age_hours without fills."""
        limit_hours = max_age_hours if max_age_hours is not None else self.config.max_stale_order_hours
        stale_orders = self.storage.get_stale_open_orders(max_age_hours=limit_hours)
        cancelled: list[ShadowOrder] = []
        for o in stale_orders:
            if self.storage.cancel_order(o.order_id):
                o.status = "CANCELED"
                cancelled.append(o)
        return cancelled

    def check_and_update_fills(self) -> list[ShadowOrder]:
        """Checks real-time market tape & orderbooks to simulate realistic order fills."""
        open_orders = self.storage.get_open_orders()
        filled_orders: list[ShadowOrder] = []

        for order in open_orders:
            if not order.token_id:
                continue

            filled = False
            fill_price = order.limit_price

            # 1. Check orderbook
            book = self._fetch_book(order.token_id)
            if book:
                asks = book.get("asks", [])
                if asks:
                    best_ask = min(float(a["price"]) for a in asks if "price" in a)
                    # If best ask is at or below our limit, an incoming seller matches or crossed our bid
                    if best_ask <= order.limit_price:
                        filled = True
                        if order.fill_mode == "TAKER":
                            fill_price = best_ask
                        else:
                            fill_price = order.limit_price

            # 2. If not filled via book, check real Data API trades tape
            if not filled and order.condition_id and order.fill_mode in ("MAKER_STRICT", "MAKER_TOUCH"):
                trades = self._fetch_trades(order.condition_id)
                if trades:
                    # Look for trades matching our token and price criteria
                    for t in trades:
                        t_price = float(t.get("price") or 0.0)
                        t_size = float(t.get("size") or 0.0)
                        t_asset = str(t.get("asset") or "")

                        if t_asset and t_asset != order.token_id:
                            continue

                        # Check if trade occurred at or below our limit
                        if order.fill_mode == "MAKER_STRICT":
                            # Strict trade through: market traded strictly below our limit, or traded at limit with sufficient volume
                            if t_price < order.limit_price or (t_price <= order.limit_price and t_size >= order.size_shares):
                                filled = True
                                fill_price = order.limit_price
                                break
                        elif order.fill_mode == "MAKER_TOUCH":
                            # Touch: any print at or below our limit
                            if t_price <= order.limit_price:
                                filled = True
                                fill_price = order.limit_price
                                break

            if filled:
                if self.storage.mark_order_filled(order.order_id, fill_price=fill_price):
                    order.status = "FILLED"
                    order.fill_price = fill_price
                    filled_orders.append(order)

        return filled_orders

    def _fetch_book(self, token_id: str) -> Optional[dict[str, Any]]:
        if self._custom_book_fetcher:
            return self._custom_book_fetcher(token_id)

        url = f"https://clob.polymarket.com/book?token_id={token_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        try:
            with urllib.request.urlopen(req, timeout=self.config.request_timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _fetch_trades(self, condition_id: str) -> Optional[list[dict[str, Any]]]:
        if self._custom_trades_fetcher:
            return self._custom_trades_fetcher(condition_id)

        url = f"https://data-api.polymarket.com/trades?market={condition_id}&limit=20"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        try:
            with urllib.request.urlopen(req, timeout=self.config.request_timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data if isinstance(data, list) else None
        except Exception:
            return None
