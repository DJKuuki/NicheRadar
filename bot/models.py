from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Market:
    market_id: str
    title: str
    description: str
    rules: str
    category: str
    closes_at: datetime
    volume: float
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    outcomes: list[str] = field(default_factory=list)
    token_ids: list[str] = field(default_factory=list)
    outcome_token_ids: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def mid_probability(self) -> float:
        return round((self.yes_bid + self.yes_ask) / 2, 4)

    @property
    def spread(self) -> float:
        return round(self.yes_ask - self.yes_bid, 4)

    @property
    def no_mid_probability(self) -> float:
        return round((self.no_bid + self.no_ask) / 2, 4)

    @property
    def no_spread(self) -> float:
        return round(self.no_ask - self.no_bid, 4)

    def bid_for_side(self, side: str) -> float:
        return self.yes_bid if side == "BUY_YES" else self.no_bid

    def ask_for_side(self, side: str) -> float:
        return self.yes_ask if side == "BUY_YES" else self.no_ask

    def mid_for_side(self, side: str) -> float:
        return self.mid_probability if side == "BUY_YES" else self.no_mid_probability

    def spread_for_side(self, side: str) -> float:
        return self.spread if side == "BUY_YES" else self.no_spread


@dataclass
class ParsedMarket:
    market: Market
    event_type: str
    subject: str
    platform: str
    action: str
    days_to_expiry: float


@dataclass
class Evidence:
    score: float
    confidence: float
    reasons: list[str]
    mode: str = "unknown"
    source_url: str | None = None
    source_type: str | None = None
    recent_entries_30d: int | None = None
    keyword_hits_30d: int | None = None
    latest_entry_age_days: float | None = None
    preheat_score: float | None = None
    cadence_score: float | None = None
    partner_score: float | None = None
    source_reliability: float | None = None


@dataclass
class Signal:
    market_id: str
    side: str
    p_model: float
    p_mid: float
    edge: float
    net_edge: float
    max_entry_price: float
    confidence: float
    reasons: list[str]


@dataclass
class TradeIdea:
    market_id: str
    title: str
    action: str
    side: str
    target_price: float
    net_edge: float
    reasons: list[str]
