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

    def get_user_info(self, handle: str = "elonmusk") -> dict[str, Any]:
        """Fetch user profile and active trackings list."""
        url = f"{self.base_url}/users/{handle}?includeStats=true"
        resp = self.session.get(url, timeout=self.timeout_sec)
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("success"):
            raise ValueError(f"XTracker API error for user {handle}: {payload}")
        return payload.get("data", {})

    def get_user_metrics(self, user_id: str) -> list[dict[str, Any]]:
        """Fetch full historical daily metrics for user."""
        url = f"{self.base_url}/metrics/{user_id}"
        resp = self.session.get(url, timeout=self.timeout_sec)
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("success"):
            raise ValueError(f"XTracker API error for metrics {user_id}: {payload}")
        return payload.get("data", [])

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
        self, user_id: str, lookback_days: int = 90
    ) -> tuple[float, float, list[int]]:
        """Calculate mean and variance of daily post counts across historical data.

        Returns (mean_daily, variance_daily, daily_counts_list).
        """
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
        return mean_c, var_c, counts

    @staticmethod
    def _parse_iso(s: str | None) -> datetime | None:
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
