"""Tweet Market Scanner - Scans Polymarket Gamma API and CLOB for Tweet/Post Markets,

correlates them with XTracker live feeds, and runs quantitative edge evaluation
across multiple targets (e.g. Elon Musk, Donald Trump, CZ, White House).
"""

from __future__ import annotations

import json
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

DEFAULT_TARGET_HANDLES = ["elonmusk", "realDonaldTrump", "cz_binance", "WhiteHouse"]

HANDLE_KEYWORDS: dict[str, list[str]] = {
    "elonmusk": ["elon", "musk"],
    "realDonaldTrump": ["trump", "donald"],
    "cz_binance": ["cz", "binance"],
    "WhiteHouse": ["white house", "whitehouse", "white-house"],
    "tedcruz": ["ted cruz", "cruz"],
    "ZelenskyyUa": ["zelenskyy", "zelensky"],
    "NYCMayor": ["nyc mayor", "adams"],
    "khamenei_ir": ["khamenei"],
}


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
        self._default_model = prob_model or TweetProbabilityModel()
        self._models: dict[str, TweetProbabilityModel] = {}
        self.timeout_sec = timeout_sec
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NicheRadar-TweetScanner/1.0",
            "Accept": "application/json",
        })

    def get_model_for_handle(self, handle: str) -> TweetProbabilityModel:
        """Returns or instantiates a dedicated probability model for a specific target."""
        if handle not in self._models:
            self._models[handle] = TweetProbabilityModel(
                min_edge_threshold=self._default_model.min_edge,
                kelly_scale=self._default_model.kelly_scale,
            )
        return self._models[handle]

    def fetch_active_tweet_events(self, handle: str | None = None) -> list[ScannedTweetEvent]:
        """Fetch all active tweet or social post count markets from Gamma API."""
        params = {
            "tag_id": "972",
            "active": "true",
            "closed": "false",
            "limit": 50,
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

            # Filter for tweet / post count markets
            is_post_market = (
                "tweets-markets" in tags
                or "tweet" in title.lower()
                or "tweets" in slug.lower()
                or "posts" in title.lower()
                or "posts" in slug.lower()
            )
            if not is_post_market:
                continue

            # If handle is specified, filter events matching this handle
            if handle is not None:
                keywords = HANDLE_KEYWORDS.get(handle, [handle.lower()])
                title_lower = title.lower()
                slug_lower = slug.lower()
                if not any(k in title_lower or k in slug_lower for k in keywords):
                    continue

            markets = e.get("markets", [])
            brackets = []
            for m in markets:
                question = m.get("question", "")
                spec = self._parse_bracket_question(question, m)
                if spec:
                    brackets.append(spec)

            # Sort brackets by low threshold
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
        try:
            trackings = self.xtracker.list_trackings(handle)
        except Exception as exc:
            logger.warning("Failed to list trackings for %s: %s", handle, exc)
            return None

        for t in trackings:
            # Check marketLink or slug match
            if t.market_link and event.slug in t.market_link:
                try:
                    return self.xtracker.get_tracking_progress(t.id, handle=handle)
                except Exception as exc:
                    logger.warning("Failed to get tracking progress for %s (%s): %s", handle, t.id, exc)
                    return None

            # Fuzzy date range match if titles correspond
            if abs((t.end_date - event.end_date).total_seconds()) < 7200:
                try:
                    return self.xtracker.get_tracking_progress(t.id, handle=handle)
                except Exception as exc:
                    logger.warning("Failed to get tracking progress for %s (%s): %s", handle, t.id, exc)
                    return None

        return None

    def scan_and_evaluate(
        self, handles: list[str] | str | None = None
    ) -> list[tuple[ScannedTweetEvent, TrackingProgress | None, list[BracketEvaluation]]]:
        """Full end-to-end pipeline: scan markets for targets, correlate xtracker, and evaluate edges."""
        if handles is None:
            target_handles = DEFAULT_TARGET_HANDLES
        elif isinstance(handles, str):
            target_handles = [handles]
        else:
            target_handles = list(handles)

        all_results: list[tuple[ScannedTweetEvent, TrackingProgress | None, list[BracketEvaluation]]] = []

        for handle in target_handles:
            model = self.get_model_for_handle(handle)

            # 1. Update historical model parameters from XTracker
            try:
                user_info = self.xtracker.get_user_info(handle)
                user_id = user_info.get("id")
                if user_id:
                    mean_d, var_d, _ = self.xtracker.calculate_historical_daily_stats(user_id)
                    model.update_historical_parameters(mean_d, var_d)
            except Exception as exc:
                logger.warning("Failed to refresh historical stats for %s: %s (continuing with current parameters)", handle, exc)

            try:
                events = self.fetch_active_tweet_events(handle=handle)
            except Exception as exc:
                logger.error("Failed to fetch active events for %s from Gamma: %s", handle, exc)
                continue

            for ev in events:
                try:
                    progress = self.match_with_xtracker(ev, handle=handle)
                except Exception as exc:
                    logger.warning("Failed to match XTracker progress for %s: %s", ev.slug, exc)
                    progress = None

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
                            p_list = json.loads(prices)
                            if len(p_list) > 0:
                                yes_price = float(p_list[0])
                        except Exception:
                            pass

                    ask = yes_price
                    bid = max(0.01, round(yes_price - 0.02, 2)) if yes_price is not None else None
                    quotes[q] = (ask, bid)

                evaluations = model.evaluate_all_brackets(
                    ev.brackets,
                    current_count=current_count,
                    remaining_hours=remaining_hours,
                    market_quotes=quotes,
                )
                all_results.append((ev, progress, evaluations))

        return all_results

    @staticmethod
    def _parse_bracket_question(q: str, m: dict[str, Any]) -> BracketSpec | None:
        """Parse bracket string like 'Will Elon Musk post 200-219 tweets...' or '0-19 posts...'."""
        slug = m.get("slug", "")

        # Match pattern like "200-219", "0-19 posts", "20-39 Truth Social posts"
        match_range = re.search(r"(\d+)\s*[-–]\s*(\d+)\s*(?:[A-Za-z\s]*?)(?:tweets?|posts?)", q, re.IGNORECASE)
        if match_range:
            low = int(match_range.group(1))
            high = int(match_range.group(2))
            return BracketSpec(low=low, high=high, name=q, market_slug=slug)

        # Match pattern like "500+", "100+ Truth Social posts", "500 or more"
        match_plus = re.search(r"(\d+)\s*(?:\+|or more)\s*(?:[A-Za-z\s]*?)(?:tweets?|posts?)", q, re.IGNORECASE)
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
