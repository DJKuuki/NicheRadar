"""XTracker Client - Connects to Polymarket's official post tracking API (xtracker.polymarket.com).

Extracts real-time post tracking metrics, historical daily counts, and tracking session states
for Tweet Markets (such as @elonmusk post count markets).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://xtracker.polymarket.com/api"


@dataclass(frozen=True)
class UserTracking:
    id: str
    user_id: str
    title: str
    start_date: datetime
    end_date: datetime
    is_active: bool
    market_link: str | None = None
    target: int | None = None


@dataclass
class TrackingProgress:
    tracking: UserTracking
    current_count: int
    elapsed_hours: float
    total_hours: float
    remaining_hours: float
    percent_time_elapsed: float
    pace_per_day: float


class XTrackerClient:
    def __init__(self, base_url: str = BASE_URL, timeout_sec: int = 15):
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NicheRadar-XTracker/1.0",
            "Accept": "application/json",
        })
        self._user_info_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._user_metrics_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._stats_cache: dict[str, tuple[float, tuple[float, float, list[int]]]] = {}

    def _get_json_with_retry(
        self, url: str, max_retries: int = 3, backoff_sec: float = 1.5
    ) -> dict[str, Any]:
        """Performs GET request with retries and exponential backoff for 5xx errors."""
        last_error: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = self.session.get(url, timeout=self.timeout_sec)
                # If 5xx server error, wait and retry
                if 500 <= resp.status_code < 600:
                    logger.warning("XTracker 5xx error (attempt %d/%d) at %s: %s", attempt, max_retries, url, resp.status_code)
                    if attempt < max_retries:
                        time.sleep(backoff_sec * (2 ** (attempt - 1)))
                        continue
                resp.raise_for_status()
                payload = resp.json()
                if not payload.get("success"):
                    raise ValueError(f"XTracker API error at {url}: {payload}")
                return payload
            except (requests.exceptions.RequestException, ValueError) as exc:
                last_error = exc
                if attempt < max_retries:
                    time.sleep(backoff_sec * (2 ** (attempt - 1)))
                else:
                    logger.error("XTracker request failed after %d attempts at %s: %s", max_retries, url, exc)

        assert last_error is not None
        raise last_error

    def get_user_info(self, handle: str = "elonmusk", ttl_sec: float = 60.0) -> dict[str, Any]:
        """Fetch user profile and active trackings list with TTL caching and stale fallback."""
        now = time.monotonic()
        if handle in self._user_info_cache:
            cached_time, cached_val = self._user_info_cache[handle]
            if now - cached_time < ttl_sec:
                return cached_val

        url = f"{self.base_url}/users/{handle}?includeStats=true"
        try:
            payload = self._get_json_with_retry(url)
            data = payload.get("data", {})
            self._user_info_cache[handle] = (now, data)
            return data
        except Exception as exc:
            if handle in self._user_info_cache:
                logger.warning("Using stale cached user info for %s after error: %s", handle, exc)
                return self._user_info_cache[handle][1]
            raise exc

    def get_user_metrics(self, user_id: str, ttl_sec: float = 300.0) -> list[dict[str, Any]]:
        """Fetch full historical daily metrics for user with TTL caching and stale fallback."""
        now = time.monotonic()
        if user_id in self._user_metrics_cache:
            cached_time, cached_val = self._user_metrics_cache[user_id]
            if now - cached_time < ttl_sec:
                return cached_val

        url = f"{self.base_url}/metrics/{user_id}"
        try:
            payload = self._get_json_with_retry(url)
            data = payload.get("data", [])
            self._user_metrics_cache[user_id] = (now, data)
            return data
        except Exception as exc:
            if user_id in self._user_metrics_cache:
                logger.warning("Using stale cached metrics for %s after error: %s", user_id, exc)
                return self._user_metrics_cache[user_id][1]
            raise exc

    def list_trackings(self, handle: str = "elonmusk") -> list[UserTracking]:
        """List all tracking sessions for user as structured objects."""
        data = self.get_user_info(handle)
        raw_trackings = data.get("trackings", [])
        result = []
        for t in raw_trackings:
            start_dt = self._parse_iso(t.get("startDate"))
            end_dt = self._parse_iso(t.get("endDate"))
            if not start_dt or not end_dt:
                continue
            result.append(UserTracking(
                id=t["id"],
                user_id=t["userId"],
                title=t.get("title", ""),
                start_date=start_dt,
                end_date=end_dt,
                is_active=bool(t.get("isActive")),
                market_link=t.get("marketLink"),
                target=t.get("target"),
            ))
        return result

    def get_tracking_progress(
        self, tracking_id: str, handle: str = "elonmusk", now: datetime | None = None
    ) -> TrackingProgress:
        """Calculate current cumulative count and time progress for a specific tracking."""
        user_info = self.get_user_info(handle)
        user_id = user_info.get("id")
        if not user_id:
            raise ValueError(f"User ID not found for {handle}")

        trackings = self.list_trackings(handle)
        target_tracking = next((t for t in trackings if t.id == tracking_id), None)
        if not target_tracking:
            raise ValueError(f"Tracking ID {tracking_id} not found for {handle}")

        if now is None:
            now = datetime.now(timezone.utc)

        metrics = self.get_user_metrics(user_id)
        matching_items = [
            m for m in metrics if m.get("data", {}).get("trackingId") == tracking_id
        ]
        if matching_items:
            matching_items.sort(key=lambda x: x.get("date", ""))
            latest = matching_items[-1]
            cum = latest.get("data", {}).get("cumulative")
            count = latest.get("data", {}).get("count", 0)
            current_count = int(cum) if cum is not None and cum > 0 else int(count)
        else:
            current_count = 0

        start = target_tracking.start_date
        end = target_tracking.end_date
        total_seconds = max(1.0, (end - start).total_seconds())
        elapsed_seconds = max(0.0, min(total_seconds, (now - start).total_seconds()))
        remaining_seconds = max(0.0, (end - now).total_seconds())

        total_hours = total_seconds / 3600.0
        elapsed_hours = elapsed_seconds / 3600.0
        remaining_hours = remaining_seconds / 3600.0

        percent_elapsed = (elapsed_seconds / total_seconds) * 100.0
        pace_per_day = (current_count / (elapsed_hours / 24.0)) if elapsed_hours >= 1.0 else 0.0

        return TrackingProgress(
            tracking=target_tracking,
            current_count=current_count,
            elapsed_hours=elapsed_hours,
            total_hours=total_hours,
            remaining_hours=remaining_hours,
            percent_time_elapsed=percent_elapsed,
            pace_per_day=pace_per_day,
        )

    def calculate_historical_daily_stats(
        self, user_id: str, lookback_days: int = 90, ttl_sec: float = 3600.0
    ) -> tuple[float, float, list[int]]:
        """Calculate mean and variance of daily post counts across historical data with caching."""
        cache_key = f"{user_id}_{lookback_days}"
        now = time.monotonic()
        if cache_key in self._stats_cache:
            cached_time, cached_val = self._stats_cache[cache_key]
            if now - cached_time < ttl_sec:
                return cached_val

        metrics = self.get_user_metrics(user_id)
        # Sort by date descending
        daily_items = [m for m in metrics if m.get("type") == "daily"]
        daily_items.sort(key=lambda x: x.get("date", ""), reverse=True)

        counts = []
        for m in daily_items[:lookback_days]:
            c = m.get("data", {}).get("count")
            if c is not None and isinstance(c, (int, float)):
                counts.append(int(c))

        if not counts:
            return 30.0, 100.0, []  # default fallback

        n = len(counts)
        mean_c = sum(counts) / n
        var_c = sum((x - mean_c) ** 2 for x in counts) / (n - 1) if n > 1 else mean_c
        res = (mean_c, var_c, counts)
        self._stats_cache[cache_key] = (now, res)
        return res

    @staticmethod
    def _parse_iso(s: str | None) -> datetime | None:
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
