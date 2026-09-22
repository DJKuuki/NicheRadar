"""Tweet Market Scanner - Scans Polymarket Gamma API and CLOB for Tweet/Post Markets,

correlates them with XTracker live feeds, and runs quantitative edge evaluation
across multiple targets (e.g. Elon Musk, Donald Trump, CZ, White House).
"""

from __future__ import annotations

import json
import logging
import math
import re
from urllib.parse import urlparse
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

    def fetch_active_tweet_events(
        self, handle: str | None = None, max_tenor_days: float | None = 8.0
    ) -> list[ScannedTweetEvent]:
        """Fetch all active tweet or social post count markets from Gamma API,
        optionally filtered by maximum remaining tenor in days (default: 8.0).
        """
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
        now = datetime.now(timezone.utc)

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

            start_dt = self._parse_iso(e.get("startDate")) or now
            end_dt = self._parse_iso(e.get("endDate")) or now

            # Tenor filter: avoid capital lockup in multi-week/monthly markets
            if max_tenor_days is not None:
                tenor_days = (end_dt - now).total_seconds() / 86400.0
                if tenor_days > max_tenor_days or tenor_days <= 0:
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

        # Gamma startDate is the listing date. Only an exact event link can
        # establish the authoritative counting window; never match by end alone.
        matches = [t for t in trackings if t.market_link and
                   urlparse(t.market_link).path.rstrip("/") == f"/event/{event.slug}"]
        if len(matches) != 1:
            logger.warning("No unique exact tracking match for %s", event.slug)
            return None
        tracking = matches[0]
        if abs((tracking.end_date - event.end_date).total_seconds()) > 60:
            logger.warning("Tracking end disagrees with market for %s", event.slug)
            return None
        try:
            progress = self.xtracker.get_tracking_progress(tracking.id, handle=handle)
            event.matched_tracking_id = tracking.id
            return progress
        except Exception as exc:
            logger.warning("Invalid live tracking for %s: %s", event.slug, exc)
            return None

    def fetch_market_quote(self, market: dict[str, Any]) -> tuple[float, float] | None:
        """Read executable YES quotes; no synthetic spread from Gamma prices."""
        try:
            tokens = market.get("clobTokenIds", [])
            outcomes = market.get("outcomes", ["Yes", "No"])
            tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
            outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
            token = tokens[[str(x).lower() for x in outcomes].index("yes")]
            resp = self.session.get("https://clob.polymarket.com/book",
                                    params={"token_id": token}, timeout=self.timeout_sec)
            resp.raise_for_status()
            book = resp.json()
            asks = [float(x["price"]) for x in book.get("asks", [])
                    if float(x["size"]) > 0 and 0 < float(x["price"]) < 1]
            bids = [float(x["price"]) for x in book.get("bids", [])
                    if float(x["size"]) > 0 and 0 < float(x["price"]) < 1]
            if not asks or not bids or max(bids) >= min(asks):
                return None
            market["_yes_token_id"] = str(token)
            market["_tick_size"] = float(book.get("tick_size", "0.01"))
            if not math.isfinite(market["_tick_size"]) or not 0 < market["_tick_size"] < 1:
                return None
            market["_quote_at_utc"] = datetime.now(timezone.utc).isoformat()
            return min(asks), max(bids)
        except (requests.RequestException, ValueError, TypeError, KeyError, IndexError):
            return None

    def scan_and_evaluate(
        self,
        handles: list[str] | str | None = None,
        max_tenor_days: float | None = 8.0,
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
                if not user_id:
                    raise ValueError("Missing user identity")
                mean_d, var_d, _ = self.xtracker.calculate_historical_daily_stats(user_id)
                model.update_historical_parameters(mean_d, var_d)
            except Exception as exc:
                logger.warning("Skipping %s: invalid historical stats: %s", handle, exc)
                continue

            try:
                events = self.fetch_active_tweet_events(handle=handle, max_tenor_days=max_tenor_days)
            except Exception as exc:
                logger.error("Failed to fetch active events for %s from Gamma: %s", handle, exc)
                continue

            for ev in events:
                try:
                    progress = self.match_with_xtracker(ev, handle=handle)
                except Exception as exc:
                    logger.warning("Failed to match XTracker progress for %s: %s", ev.slug, exc)
                    progress = None

                if progress is None:
                    continue
                current_count = progress.current_count
                now = datetime.now(timezone.utc)
                remaining_hours = max(0.0, (progress.tracking.end_date -
                    max(now, progress.tracking.start_date)).total_seconds() / 3600.0)
                if remaining_hours <= 0:
                    continue
                evaluations = []
                for market in ev.markets:
                    if market.get("closed") or market.get("acceptingOrders") is False:
                        continue
                    bracket = next((b for b in ev.brackets if b.name == market.get("question")), None)
                    if bracket is None:
                        continue
                    quote = self.fetch_market_quote(market)
                    if quote is None:
                        continue
                    ask, bid = quote
                    evaluation = model.evaluate_bracket(
                        bracket, current_count, remaining_hours, ask, bid,
                        tick_size=market["_tick_size"],
                    )
                    market["_model_context"] = {
                        "tracking_id": progress.tracking.id,
                        "window_start": progress.tracking.start_date.isoformat(),
                        "window_end": progress.tracking.end_date.isoformat(),
                        "current_count": current_count,
                        "remaining_hours": remaining_hours,
                        "count_observed_at": progress.observed_at_utc,
                        "mean_daily": model.mean_daily,
                        "var_daily": model.var_daily,
                        "quote_at": market["_quote_at_utc"],
                        "quote_ask": ask, "quote_bid": bid,
                    }
                    evaluations.append(evaluation)
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
