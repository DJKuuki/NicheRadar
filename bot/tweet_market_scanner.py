"""Tweet Market Scanner - Scans Polymarket Gamma API and CLOB for Tweet Markets,

correlates them with XTracker live feeds, and runs quantitative edge evaluation.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from bot.tweet_poisson_model import BracketEvaluation, BracketSpec, TweetProbabilityModel
from bot.xtracker_client import TrackingProgress, XTrackerClient

logger = logging.getLogger(__name__)

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"


@dataclass
class ScannedTweetEvent:
    event_id: str
    title: str
    slug: str
    start_date: datetime
    end_date: datetime
    active: bool
    markets: list[dict[str, Any]]
    brackets: list[BracketSpec]
    matched_tracking_id: str | None = None


class TweetMarketScanner:
    def __init__(
        self,
        xtracker_client: XTrackerClient | None = None,
        prob_model: TweetProbabilityModel | None = None,
        timeout_sec: int = 15,
    ):
        self.xtracker = xtracker_client or XTrackerClient()
        self.model = prob_model or TweetProbabilityModel()
        self.timeout_sec = timeout_sec
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NicheRadar-TweetScanner/1.0",
            "Accept": "application/json",
        })

    def fetch_active_tweet_events(self, handle: str = "elonmusk") -> list[ScannedTweetEvent]:
        """Fetch all active or recent tweet markets from Gamma API."""
        params = {
            "tag_id": "972",
            "active": "true",
            "closed": "false",
            "limit": 30,
        }
        resp = self.session.get(GAMMA_EVENTS_URL, params=params, timeout=self.timeout_sec)
        resp.raise_for_status()
        events_data = resp.json()
        if not isinstance(events_data, list):
            return []

        scanned = []
        for e in events_data:
            title = e.get("title", "")
            slug = e.get("slug", "")
            tags = [t.get("slug") for t in e.get("tags", []) if isinstance(t, dict)]

            # Filter for tweet markets
            is_tweet_market = (
                "tweets-markets" in tags
                or "tweet" in title.lower()
                or "tweets" in slug.lower()
            )
            if not is_tweet_market:
                continue

            markets = e.get("markets", [])
            brackets = []
            for m in markets:
                question = m.get("question", "")
                spec = self._parse_bracket_question(question, m)
                if spec:
                    brackets.append(spec)

            # Sort brackets by low
            brackets.sort(key=lambda b: b.low)

            start_dt = self._parse_iso(e.get("startDate")) or datetime.now(timezone.utc)
            end_dt = self._parse_iso(e.get("endDate")) or datetime.now(timezone.utc)

            scanned.append(
                ScannedTweetEvent(
                    event_id=str(e.get("id")),
                    title=title,
                    slug=slug,
                    start_date=start_dt,
                    end_date=end_dt,
                    active=bool(e.get("active", True)),
                    markets=markets,
                    brackets=brackets,
                )
            )
        return scanned

    def match_with_xtracker(
        self, event: ScannedTweetEvent, handle: str = "elonmusk"
    ) -> TrackingProgress | None:
        """Find the matching XTracker tracking session for this Polymarket event."""
        trackings = self.xtracker.list_trackings(handle)
        for t in trackings:
            # Check marketLink or title match
            if t.market_link and event.slug in t.market_link:
                return self.xtracker.get_tracking_progress(t.id, handle=handle)
            # Fuzzy date range match
            if abs((t.end_date - event.end_date).total_seconds()) < 7200:
                return self.xtracker.get_tracking_progress(t.id, handle=handle)
        return None

    def scan_and_evaluate(
        self, handle: str = "elonmusk"
    ) -> list[tuple[ScannedTweetEvent, TrackingProgress | None, list[BracketEvaluation]]]:
        """Full end-to-end pipeline: scan markets, correlate xtracker, and evaluate edges."""
        # 1. Update historical model parameters from XTracker
        user_info = self.xtracker.get_user_info(handle)
        user_id = user_info.get("id")
        if user_id:
            mean_d, var_d, _ = self.xtracker.calculate_historical_daily_stats(user_id)
            self.model.update_historical_parameters(mean_d, var_d)

        events = self.fetch_active_tweet_events(handle=handle)
        results = []

        for ev in events:
            progress = self.match_with_xtracker(ev, handle=handle)
            current_count = progress.current_count if progress else 0
            now = datetime.now(timezone.utc)
            remaining_hours = max(0.0, (ev.end_date - now).total_seconds() / 3600.0)

            # Build market quotes map
            quotes: dict[str, tuple[float | None, float | None]] = {}
            for m in ev.markets:
                q = m.get("question", "")
                prices = m.get("outcomePrices")
                yes_price = None
                if isinstance(prices, list) and len(prices) > 0:
                    try:
                        yes_price = float(prices[0])
                    except Exception:
                        pass
                elif isinstance(prices, str):
                    try:
                        import json
                        p_list = json.loads(prices)
                        if len(p_list) > 0:
                            yes_price = float(p_list[0])
                    except Exception:
                        pass

                # Approximate bid / ask spread if CLOB prices not directly nested
                ask = yes_price
                bid = max(0.01, round(yes_price - 0.02, 2)) if yes_price is not None else None
                quotes[q] = (ask, bid)

            evaluations = self.model.evaluate_all_brackets(
                ev.brackets,
                current_count=current_count,
                remaining_hours=remaining_hours,
                market_quotes=quotes,
            )
            results.append((ev, progress, evaluations))

        return results

    @staticmethod
    def _parse_bracket_question(q: str, m: dict[str, Any]) -> BracketSpec | None:
        """Parse bracket string like 'Will Elon Musk post 200-219 tweets...' or '500+ tweets...'."""
        slug = m.get("slug", "")
        # Match pattern like "200-219"
        match_range = re.search(r"(\d+)\s*[-–]\s*(\d+)\s*tweets?", q, re.IGNORECASE)
        if match_range:
            low = int(match_range.group(1))
            high = int(match_range.group(2))
            return BracketSpec(low=low, high=high, name=q, market_slug=slug)

        # Match pattern like "500+" or "500 or more"
        match_plus = re.search(r"(\d+)\s*\+\s*tweets?", q, re.IGNORECASE)
        if match_plus:
            low = int(match_plus.group(1))
            return BracketSpec(low=low, high=None, name=q, market_slug=slug)

        return None

    @staticmethod
    def _parse_iso(s: str | None) -> datetime | None:
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
