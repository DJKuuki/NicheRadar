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
    max_bracket_risk_usdc: float = 50.0
    min_bracket_risk_usdc: float = 5.0
    max_event_risk_usdc: float = 60.0  # Max total risk allocated to any single event across all brackets
    max_otm_risk_usdc: float = 15.0    # Clamp for deep OTM brackets (limit_price < otm_threshold)
    otm_threshold: float = 0.05
    default_fill_mode: str = "MAKER_STRICT"  # MAKER_STRICT, MAKER_TOUCH, TAKER
    request_timeout: int = 10


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


def candidates_from_scan_results(
    scan_results: list[tuple[ScannedTweetEvent, Optional[TrackingProgress], list[BracketEvaluation]]],
    min_edge: float = 0.04,
) -> list[TradeCandidate]:
    """Converts scanner output tuples into structured TradeCandidate items."""
    candidates: list[TradeCandidate] = []

    for ev, _, evals in scan_results:
        # Build market lookup by slug and question
        market_by_slug: dict[str, dict[str, Any]] = {}
        market_by_q: dict[str, dict[str, Any]] = {}
        for m in ev.markets:
            if m.get("slug"):
                market_by_slug[m["slug"]] = m
            if m.get("question"):
                market_by_q[m["question"]] = m

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
            mkt_prob = b_eval.market_ask or b_eval.market_bid or target_px

            candidates.append(
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

    return candidates


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

            # Check if order already open or filled for this market
            if self.storage.has_active_order_for_bracket(cand.market_id, cand.side):
                continue

            # Check Event-Level portfolio risk cap
            if cand.event_id not in event_committed_risk:
                event_committed_risk[cand.event_id] = self.storage.get_active_risk_for_event(cand.event_id)

            current_event_risk = event_committed_risk[cand.event_id]
            if current_event_risk >= self.config.max_event_risk_usdc:
                continue

            budget_left = self.config.max_event_risk_usdc - current_event_risk

            # Sizing based on Quarter-Kelly
            risk_usdc = available_cash * max(0.01, cand.kelly_fraction)
            risk_usdc = min(risk_usdc, self.config.max_bracket_risk_usdc)

            # Deep OTM risk clamp: prevent huge dollar bets on tiny penny probabilities
            limit_price = round(cand.target_price, 3)
            if limit_price <= 0.01 or limit_price >= 0.99:
                continue

            if limit_price < self.config.otm_threshold:
                risk_usdc = min(risk_usdc, self.config.max_otm_risk_usdc)

            # Cap by event budget
            risk_usdc = min(risk_usdc, budget_left)
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
                event_committed_risk[cand.event_id] = current_event_risk + actual_cost

        return placed_orders

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
