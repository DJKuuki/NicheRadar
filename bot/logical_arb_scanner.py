"""Logical Dependency & Monotonicity Arbitrage Scanner for Polymarket.

Detects mathematically guaranteed subset/superset and mutually-exclusive mispricings
across Polymarket events, and verifies executable depth on the CLOB order book:

1. DATE_MONOTONICITY:
   For cumulative deadline markets on the same event with T1 < T2:
   Event(by T1) is a strict subset of Event(by T2) => P(YES @ T1) <= P(YES @ T2).
   If Ask(NO @ T1) + Ask(YES @ T2) < 1.00, buying NO(T1) + YES(T2) guarantees
   a minimum payout of $1.00 (and $2.00 if the event occurs between T1 and T2).

2. STRIKE_MONOTONICITY:
   For upward threshold markets ("above/reach/over K") on the same asset & expiry with K1 < K2:
   Event(>= K2) is a strict subset of Event(>= K1) => P(YES @ K2) <= P(YES @ K1).
   If Ask(NO @ K2) + Ask(YES @ K1) < 1.00, buying NO(K2) + YES(K1) guarantees
   a minimum payout of $1.00 (and $2.00 if the outcome lands in [K1, K2)).

3. MUTEX_NO_BUNDLE:
   For any negRisk (mutually exclusive) event with N outcomes (even if non-exhaustive):
   At most 1 outcome can resolve YES, so at least (N - 1) NO tokens must resolve to $1.00.
   If sum(Ask(NO_i)) < N - 1, buying 1 NO share of every outcome locks in a guaranteed profit.

4. EXHAUSTIVE_YES_BUNDLE:
   For verified exhaustive mutually exclusive partitions (complete numeric range brackets
   with both lower and upper tails, or sets containing an explicit catch-all/Draw outcome):
   Exactly 1 outcome resolves YES. If sum(Ask(YES_i)) < 1.00, buying all YES locks in profit.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"

MONTH_MAP = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


@dataclass(frozen=True)
class LogicalArbLeg:
    market_id: str
    question: str
    token_id: str
    side: str  # "BUY_YES" or "BUY_NO"
    ask_price: float
    available_shares: float


@dataclass
class LogicalArbOpportunity:
    arb_type: str  # DATE_MONOTONICITY, STRIKE_MONOTONICITY, MUTEX_NO_BUNDLE, EXHAUSTIVE_YES_BUNDLE
    event_id: str
    event_slug: str
    title: str
    legs: list[LogicalArbLeg]
    total_cost_per_unit: float
    guaranteed_payout: float
    max_payout: float
    guaranteed_net_edge: float
    executable_units: float
    executable_cost_usdc: float
    guaranteed_profit_usdc: float
    is_clob_verified: bool = False
    notes: str = ""
    detected_at_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class MidpointInversionAnomaly:
    """Tracks relative-value logical inversions where Mid(subset) > Mid(superset).

    Even when the taker ask sum exceeds $1.00 due to wide bid-ask spreads, a midpoint
    inversion signals that passive maker orders can harvest structural mispricing.
    """
    anomaly_type: str  # DATE_MID_INVERSION, STRIKE_MID_INVERSION
    event_id: str
    event_slug: str
    subset_market_id: str
    subset_question: str
    subset_mid: float
    subset_bid: float
    subset_ask: float
    superset_market_id: str
    superset_question: str
    superset_mid: float
    superset_bid: float
    superset_ask: float
    inversion_spread: float  # subset_mid - superset_mid (> 0 means inverted)
    taker_arb_cost: float    # (1 - subset_bid) + superset_ask


class LogicalArbScanner:
    def __init__(self, timeout_sec: int = 15):
        self.timeout_sec = timeout_sec
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NicheRadar-LogicalArb/1.0",
            "Accept": "application/json",
        })

    def fetch_active_events(self, max_pages: int = 10, page_size: int = 100) -> list[dict[str, Any]]:
        """Fetch active multi-market events from Polymarket Gamma API."""
        events: list[dict[str, Any]] = []
        for page in range(max_pages):
            params = {
                "active": "true",
                "closed": "false",
                "limit": page_size,
                "offset": page * page_size,
                "order": "volume24hr",
                "ascending": "false",
            }
            resp = self.session.get(GAMMA_EVENTS_URL, params=params, timeout=self.timeout_sec)
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            events.extend(batch)
            if len(batch) < page_size:
                break
        return events

    def fetch_token_top_ask(self, token_id: str) -> tuple[float, float] | None:
        """Fetch executable lowest ask price and available size from CLOB book."""
        try:
            resp = self.session.get(
                CLOB_BOOK_URL,
                params={"token_id": token_id},
                timeout=self.timeout_sec,
            )
            resp.raise_for_status()
            book = resp.json()
            asks = [
                (float(x["price"]), float(x["size"]))
                for x in book.get("asks", [])
                if float(x["size"]) > 0 and 0.0 < float(x["price"]) < 1.0
            ]
            if not asks:
                return None
            asks.sort(key=lambda pair: pair[0])
            best_price = asks[0][0]
            total_size_at_best = sum(sz for px, sz in asks if abs(px - best_price) <= 1e-6)
            return best_price, total_size_at_best
        except Exception as exc:
            logger.debug("CLOB book fetch failed for %s: %s", token_id, exc)
            return None

    @staticmethod
    def _extract_token_ids(market: dict[str, Any]) -> tuple[str | None, str | None]:
        """Return (yes_token_id, no_token_id) for a binary market."""
        try:
            tokens = market.get("clobTokenIds", [])
            outcomes = market.get("outcomes", ["Yes", "No"])
            tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
            outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
            if not isinstance(tokens, list) or not isinstance(outcomes, list) or len(tokens) < 2:
                return None, None
            lower_outcomes = [str(o).strip().lower() for o in outcomes]
            if "yes" in lower_outcomes and "no" in lower_outcomes:
                yes_idx = lower_outcomes.index("yes")
                no_idx = lower_outcomes.index("no")
                return str(tokens[yes_idx]), str(tokens[no_idx])
            return str(tokens[0]), str(tokens[1])
        except Exception:
            return None, None

    @staticmethod
    def _safe_float(val: Any) -> float | None:
        try:
            f = float(val)
            return f if math.isfinite(f) else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def parse_deadline_question(cls, question: str) -> tuple[str, datetime] | None:
        """Parse cumulative deadline questions like 'Will X happen by October 31, 2026?'.

        Returns (normalized_stem, deadline_utc) if the question represents a cumulative
        'by / before / on or before' deadline event; otherwise None.
        """
        q = question.strip()
        # Match "by/before <Month> <Day>[, <Year>]" or "by/before <Month> <Year>"
        pattern_mdy = re.compile(
            r"^(?P<prefix>.+?)\s+\b(?:by|before|on or before)\s+"
            r"(?P<month>january|february|march|april|may|june|july|august|september|sept|october|november|december|"
            r"jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)"
            r"(?:\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?)?"
            r"(?:,?\s*(?P<year>20\d{2}))?"
            r"\s*\??$",
            re.IGNORECASE,
        )
        m = pattern_mdy.match(q)
        if not m:
            return None

        stem = re.sub(r"\s+", " ", m.group("prefix").strip().lower())
        month_num = MONTH_MAP.get(m.group("month").lower())
        if not month_num:
            return None
        year_num = int(m.group("year")) if m.group("year") else 2026
        if m.group("day"):
            day_num = int(m.group("day"))
        else:
            # End of month fallback when day is omitted (e.g., "by November 2026")
            if month_num in (1, 3, 5, 7, 8, 10, 12):
                day_num = 31
            elif month_num in (4, 6, 9, 11):
                day_num = 30
            else:
                day_num = 28
        try:
            dt = datetime(year_num, month_num, day_num, 23, 59, 59, tzinfo=timezone.utc)
            return stem, dt
        except ValueError:
            return None

    @classmethod
    def parse_strike_question(cls, question: str) -> tuple[str, str, float] | None:
        """Parse price/metric threshold questions into (normalized_stem, direction, strike).

        direction is 'UP' for 'above/over/reach/hit/at least/exceed' (K1 < K2 => P(K2) <= P(K1)),
        or 'DOWN' for 'below/under/dip to/drop to/at most' (K1 < K2 => P(K1) <= P(K2)).
        Rejects range questions like 'between X and Y' or 'X-Y'.
        """
        q = question.strip()
        if re.search(r"\bbetween\b|\bover\s*/\s*under\b|\d+\s*[-–]\s*\d+", q, re.IGNORECASE):
            return None

        # Match directional comparator + number + optional k/m/b suffix (must end at word boundary)
        strike_re = re.compile(
            r"\b(?P<dir>above|over|reach|reaches|hit|hits|exceed|exceeds|at least|more than|"
            r"below|under|dip to|dips to|drop to|drops to|less than|at most)\s+"
            r"\$?(?P<num>\d+(?:,\d{3})*(?:\.\d+)?)(?P<unit>[kmb]\b|\s+(?:thousand|million|billion)\b|%)?",
            re.IGNORECASE,
        )
        m = strike_re.search(q)
        if not m:
            return None

        dir_word = m.group("dir").lower()
        direction = (
            "UP"
            if dir_word in {"above", "over", "reach", "reaches", "hit", "hits", "exceed", "exceeds", "at least", "more than"}
            else "DOWN"
        )
        raw_num = float(m.group("num").replace(",", ""))
        unit = (m.group("unit") or "").lower()
        mult = 1.0
        if unit in {"k", "thousand"}:
            mult = 1e3
        elif unit in {"m", "million"}:
            mult = 1e6
        elif unit in {"b", "billion"}:
            mult = 1e9
        strike = raw_num * mult

        # Build canonical stem by replacing the matched comparator+number with a placeholder
        stem = (q[: m.start()] + f"<{direction}_STRIKE>" + q[m.end() :]).strip().lower()
        stem = re.sub(r"\s+", " ", stem)
        return stem, direction, strike

    @staticmethod
    def is_exhaustive_partition(markets: list[dict[str, Any]]) -> bool:
        """Determine whether a set of mutually exclusive markets forms a mathematically complete partition."""
        if len(markets) < 2:
            return False

        questions = [str(m.get("question", "")).lower() for m in markets]
        group_titles = [str(m.get("groupItemTitle", "")).lower() for m in markets]
        combined_texts = [f"{q} {g}" for q, g in zip(questions, group_titles)]

        # 1. Explicit catch-all outcome ("other", "none", "any other", "someone else")
        if any(
            re.search(r"\b(other|others|any other|none of|no one|nobody|field)\b", txt)
            for txt in combined_texts
        ):
            return True

        # 2. 3-way sports match with explicit Draw / Tie outcome
        if len(markets) == 3 and any(re.search(r"\b(draw|tie)\b", txt) for txt in combined_texts):
            return True

        # 3. Complete numerical range brackets: must have both a lower tail (or 0-start) and upper tail (+)
        has_lower_tail = any(
            re.search(r"(\b0\s*[-–]\s*\d+|\bunder\s+\d+|\bbelow\s+\d+|\bless than\s+\d+|\bor less\b|\bor below\b|<\s*\d+)", txt)
            for txt in combined_texts
        )
        has_upper_tail = any(
            re.search(r"(\d+\s*\+|\d+\s*or more|\bover\s+\d+|\babove\s+\d+|\bmore than\s+\d+|\bor higher\b|>\s*\d+)", txt)
            for txt in combined_texts
        )
        if has_lower_tail and has_upper_tail and len(markets) >= 3:
            return True

        return False

    def scan_event_for_monotonicity(
        self,
        event: dict[str, Any],
        min_edge: float = 0.005,
        verify_clob: bool = True,
    ) -> tuple[list[LogicalArbOpportunity], list[MidpointInversionAnomaly]]:
        """Inspect a single event for Date or Strike monotonicity arbitrage & midpoint inversions."""
        opportunities: list[LogicalArbOpportunity] = []
        inversions: list[MidpointInversionAnomaly] = []

        if event.get("negRisk"):
            return opportunities, inversions

        active_markets = [
            m for m in event.get("markets", [])
            if m.get("active") and not m.get("closed") and m.get("acceptingOrders") is not False
        ]
        if len(active_markets) < 2:
            return opportunities, inversions

        event_id = str(event.get("id", ""))
        event_slug = str(event.get("slug", ""))
        title = str(event.get("title", ""))

        # 1. Group by Date Monotonicity stems
        date_groups: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
        # 2. Group by Strike Monotonicity stems
        strike_groups: dict[tuple[str, str], list[tuple[float, dict[str, Any]]]] = {}

        for m in active_markets:
            q = str(m.get("question", ""))
            parsed_date = self.parse_deadline_question(q)
            if parsed_date:
                stem, dt = parsed_date
                date_groups.setdefault(stem, []).append((dt, m))

            parsed_strike = self.parse_strike_question(q)
            if parsed_strike:
                stem, direction, strike = parsed_strike
                strike_groups.setdefault((stem, direction), []).append((strike, m))

        # Helper to evaluate ordered pairs where subset_mkt => superset_mkt
        def check_subset_pair(
            subset_mkt: dict[str, Any],
            superset_mkt: dict[str, Any],
            arb_type: str,
            anomaly_type: str,
            note: str,
        ) -> None:
            sub_bid = self._safe_float(subset_mkt.get("bestBid"))
            sub_ask = self._safe_float(subset_mkt.get("bestAsk"))
            sup_bid = self._safe_float(superset_mkt.get("bestBid"))
            sup_ask = self._safe_float(superset_mkt.get("bestAsk"))
            if None in (sub_bid, sub_ask, sup_bid, sup_ask):
                return
            if not (0 < sub_bid <= sub_ask < 1 and 0 < sup_bid <= sup_ask < 1):
                return

            sub_mid = 0.5 * (sub_bid + sub_ask)
            sup_mid = 0.5 * (sup_bid + sup_ask)
            # In a binary book, Ask(NO of subset) = 1.0 - Bid(YES of subset)
            screen_no_ask = round(1.0 - sub_bid, 6)
            screen_cost = screen_no_ask + sup_ask

            if sub_mid > sup_mid + 0.005:
                inversions.append(
                    MidpointInversionAnomaly(
                        anomaly_type=anomaly_type,
                        event_id=event_id,
                        event_slug=event_slug,
                        subset_market_id=str(subset_mkt.get("id")),
                        subset_question=str(subset_mkt.get("question")),
                        subset_mid=round(sub_mid, 4),
                        subset_bid=sub_bid,
                        subset_ask=sub_ask,
                        superset_market_id=str(superset_mkt.get("id")),
                        superset_question=str(superset_mkt.get("question")),
                        superset_mid=round(sup_mid, 4),
                        superset_bid=sup_bid,
                        superset_ask=sup_ask,
                        inversion_spread=round(sub_mid - sup_mid, 4),
                        taker_arb_cost=round(screen_cost, 4),
                    )
                )

            if screen_cost < 1.0 - min_edge:
                _, sub_no_tok = self._extract_token_ids(subset_mkt)
                sup_yes_tok, _ = self._extract_token_ids(superset_mkt)
                if not sub_no_tok or not sup_yes_tok:
                    return

                no_px, no_sz = screen_no_ask, 100.0
                yes_px, yes_sz = sup_ask, 100.0
                verified = False
                if verify_clob:
                    no_top = self.fetch_token_top_ask(sub_no_tok)
                    yes_top = self.fetch_token_top_ask(sup_yes_tok)
                    if not no_top or not yes_top:
                        return
                    no_px, no_sz = no_top
                    yes_px, yes_sz = yes_top
                    verified = True

                total_cost = round(no_px + yes_px, 6)
                if total_cost >= 1.0 - min_edge:
                    return

                exec_units = min(no_sz, yes_sz)
                net_edge = (1.0 - total_cost) / total_cost
                opportunities.append(
                    LogicalArbOpportunity(
                        arb_type=arb_type,
                        event_id=event_id,
                        event_slug=event_slug,
                        title=title,
                        legs=[
                            LogicalArbLeg(
                                market_id=str(subset_mkt.get("id")),
                                question=str(subset_mkt.get("question")),
                                token_id=sub_no_tok,
                                side="BUY_NO",
                                ask_price=no_px,
                                available_shares=no_sz,
                            ),
                            LogicalArbLeg(
                                market_id=str(superset_mkt.get("id")),
                                question=str(superset_mkt.get("question")),
                                token_id=sup_yes_tok,
                                side="BUY_YES",
                                ask_price=yes_px,
                                available_shares=yes_sz,
                            ),
                        ],
                        total_cost_per_unit=total_cost,
                        guaranteed_payout=1.0,
                        max_payout=2.0,
                        guaranteed_net_edge=round(net_edge, 4),
                        executable_units=round(exec_units, 2),
                        executable_cost_usdc=round(exec_units * total_cost, 2),
                        guaranteed_profit_usdc=round(exec_units * (1.0 - total_cost), 2),
                        is_clob_verified=verified,
                        notes=note,
                    )
                )

        # Evaluate Date groups: T1 < T2 => mkt(T1) is subset of mkt(T2)
        for stem, items in date_groups.items():
            if len(items) < 2:
                continue
            items.sort(key=lambda x: x[0])
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    t1, m1 = items[i]
                    t2, m2 = items[j]
                    if t1 < t2:
                        check_subset_pair(
                            subset_mkt=m1,
                            superset_mkt=m2,
                            arb_type="DATE_MONOTONICITY",
                            anomaly_type="DATE_MID_INVERSION",
                            note=f"Deadline {t1.date()} implies deadline {t2.date()}",
                        )

        # Evaluate Strike groups
        for (stem, direction), items in strike_groups.items():
            if len(items) < 2:
                continue
            items.sort(key=lambda x: x[0])
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    k1, m1 = items[i]
                    k2, m2 = items[j]
                    if k1 == k2:
                        continue
                    if direction == "UP":
                        # Reaching K2 (> K1) implies reaching K1
                        check_subset_pair(
                            subset_mkt=m2,
                            superset_mkt=m1,
                            arb_type="STRIKE_MONOTONICITY",
                            anomaly_type="STRIKE_MID_INVERSION",
                            note=f"Strike >={k2:g} implies strike >={k1:g}",
                        )
                    else:
                        # Dipping below K1 (< K2) implies dipping below K2
                        check_subset_pair(
                            subset_mkt=m1,
                            superset_mkt=m2,
                            arb_type="STRIKE_MONOTONICITY",
                            anomaly_type="STRIKE_MID_INVERSION",
                            note=f"Strike <={k1:g} implies strike <={k2:g}",
                        )

        return opportunities, inversions

    def scan_event_for_mutex_bundles(
        self,
        event: dict[str, Any],
        min_edge: float = 0.005,
        verify_clob: bool = True,
    ) -> list[LogicalArbOpportunity]:
        """Inspect a negRisk mutually exclusive event for bundle arbitrage."""
        opportunities: list[LogicalArbOpportunity] = []
        if not event.get("negRisk"):
            return opportunities

        all_markets = event.get("markets", [])
        active_markets = [
            m for m in all_markets
            if m.get("active") and not m.get("closed") and m.get("acceptingOrders") is not False
        ]
        if len(active_markets) < 3:
            return opportunities

        event_id = str(event.get("id", ""))
        event_slug = str(event.get("slug", ""))
        title = str(event.get("title", ""))
        n = len(active_markets)

        asks: list[float] = []
        bids: list[float] = []
        for m in active_markets:
            a = self._safe_float(m.get("bestAsk"))
            b = self._safe_float(m.get("bestBid"))
            if a is None or b is None or not (0 <= b <= a <= 1):
                return opportunities
            asks.append(a)
            bids.append(b)

        sum_yes_ask = sum(asks)
        sum_yes_bid = sum(bids)

        # 1. Check MUTEX_NO_BUNDLE: sum(YES bid) > 1.00 <=> sum(NO ask) < N - 1
        # Guaranteed payout is at least (N - 1) even if outcomes are NOT exhaustive!
        if sum_yes_bid > 1.0 + min_edge:
            legs: list[LogicalArbLeg] = []
            verified = False
            valid = True
            for m, b in zip(active_markets, bids):
                _, no_tok = self._extract_token_ids(m)
                if not no_tok:
                    valid = False
                    break
                if verify_clob:
                    top = self.fetch_token_top_ask(no_tok)
                    if not top:
                        valid = False
                        break
                    px, sz = top
                    verified = True
                else:
                    px, sz = round(1.0 - b, 6), 100.0
                legs.append(
                    LogicalArbLeg(
                        market_id=str(m.get("id")),
                        question=str(m.get("question")),
                        token_id=no_tok,
                        side="BUY_NO",
                        ask_price=px,
                        available_shares=sz,
                    )
                )

            if valid and legs:
                total_no_cost = round(sum(l.ask_price for l in legs), 6)
                guaranteed_payout = float(n - 1)
                if total_no_cost < guaranteed_payout - min_edge:
                    exec_units = min(l.available_shares for l in legs)
                    net_edge = (guaranteed_payout - total_no_cost) / total_no_cost
                    opportunities.append(
                        LogicalArbOpportunity(
                            arb_type="MUTEX_NO_BUNDLE",
                            event_id=event_id,
                            event_slug=event_slug,
                            title=title,
                            legs=legs,
                            total_cost_per_unit=total_no_cost,
                            guaranteed_payout=guaranteed_payout,
                            max_payout=float(n),
                            guaranteed_net_edge=round(net_edge, 4),
                            executable_units=round(exec_units, 2),
                            executable_cost_usdc=round(exec_units * total_no_cost, 2),
                            guaranteed_profit_usdc=round(exec_units * (guaranteed_payout - total_no_cost), 2),
                            is_clob_verified=verified,
                            notes=f"{n} mutually exclusive outcomes; >= {n-1} NO shares guaranteed to pay $1",
                        )
                    )

        # 2. Check EXHAUSTIVE_YES_BUNDLE: only if all markets in the event are active and exhaustive
        if (
            len(active_markets) == len(all_markets)
            and sum_yes_ask < 1.0 - min_edge
            and self.is_exhaustive_partition(active_markets)
        ):
            legs = []
            verified = False
            valid = True
            for m, a in zip(active_markets, asks):
                yes_tok, _ = self._extract_token_ids(m)
                if not yes_tok:
                    valid = False
                    break
                if verify_clob:
                    top = self.fetch_token_top_ask(yes_tok)
                    if not top:
                        valid = False
                        break
                    px, sz = top
                    verified = True
                else:
                    px, sz = a, 100.0
                legs.append(
                    LogicalArbLeg(
                        market_id=str(m.get("id")),
                        question=str(m.get("question")),
                        token_id=yes_tok,
                        side="BUY_YES",
                        ask_price=px,
                        available_shares=sz,
                    )
                )

            if valid and legs:
                total_yes_cost = round(sum(l.ask_price for l in legs), 6)
                if total_yes_cost < 1.0 - min_edge:
                    exec_units = min(l.available_shares for l in legs)
                    net_edge = (1.0 - total_yes_cost) / total_yes_cost
                    opportunities.append(
                        LogicalArbOpportunity(
                            arb_type="EXHAUSTIVE_YES_BUNDLE",
                            event_id=event_id,
                            event_slug=event_slug,
                            title=title,
                            legs=legs,
                            total_cost_per_unit=total_yes_cost,
                            guaranteed_payout=1.0,
                            max_payout=1.0,
                            guaranteed_net_edge=round(net_edge, 4),
                            executable_units=round(exec_units, 2),
                            executable_cost_usdc=round(exec_units * total_yes_cost, 2),
                            guaranteed_profit_usdc=round(exec_units * (1.0 - total_yes_cost), 2),
                            is_clob_verified=verified,
                            notes=f"Exhaustive {n}-outcome partition with sum(YES ask)={total_yes_cost:.4f} < 1.00",
                        )
                    )

        return opportunities

    def scan_all(
        self,
        max_pages: int = 10,
        min_edge: float = 0.005,
        verify_clob: bool = True,
    ) -> tuple[list[LogicalArbOpportunity], list[MidpointInversionAnomaly]]:
        """Full scan across active Polymarket events for logical arbitrage and inversions."""
        events = self.fetch_active_events(max_pages=max_pages)
        all_opps: list[LogicalArbOpportunity] = []
        all_inversions: list[MidpointInversionAnomaly] = []

        for ev in events:
            opps, invs = self.scan_event_for_monotonicity(ev, min_edge=min_edge, verify_clob=verify_clob)
            all_opps.extend(opps)
            all_inversions.extend(invs)

            mutex_opps = self.scan_event_for_mutex_bundles(ev, min_edge=min_edge, verify_clob=verify_clob)
            all_opps.extend(mutex_opps)

        all_opps.sort(key=lambda o: o.guaranteed_net_edge, reverse=True)
        all_inversions.sort(key=lambda inv: inv.inversion_spread, reverse=True)
        return all_opps, all_inversions
