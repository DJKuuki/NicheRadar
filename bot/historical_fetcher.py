"""
historical_fetcher.py
---------------------
Fetches resolved (closed) markets from the Polymarket Gamma API and
retrieves their historical price series from the CLOB prices-history endpoint.

No authentication is required; both endpoints are public read-only APIs.

Usage example
-------------
from bot.historical_fetcher import HistoricalFetcher

fetcher = HistoricalFetcher(rate_limit_seconds=0.15)
markets = fetcher.fetch_resolved_markets(
    keywords=["release", "album", "ipo", "tweet", "song"],
    min_volume=1000.0,
    start_date="2023-01-01",
    max_markets=2000,
)
for market in markets:
    prices = fetcher.fetch_price_history(market, fidelity_minutes=360)
    ...
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"

# Keywords used by market_parser that we want to capture in history
DEFAULT_KEYWORDS = [
    "release",
    "released",
    "album",
    "song",
    "ipo",
    "tweet",
    "post on",
    "announce",
    "upload",
    "trailer",
    "gpt",
    "chatgpt",
    "optimus",
    "macbook",
    "cellular",
    "market cap",
]

# Chunk size in days when requesting CLOB price history to avoid timeouts
_PRICE_HISTORY_CHUNK_DAYS = 15


@dataclass
class HistoricalPriceBar:
    timestamp_utc: datetime
    open_price: float
    high_price: float
    low_price: float
    close_price: float


@dataclass
class HistoricalMarket:
    market_id: str
    slug: str
    question: str
    description: str
    category: str
    end_date: datetime
    closed_time: datetime | None
    volume: float
    yes_token_id: str
    no_token_id: str
    # Final settlement: 1.0 if YES won, 0.0 if NO won, None if ambiguous
    outcome_yes: float | None
    outcome_no: float | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class HistoricalFetcher:
    def __init__(
        self,
        timeout: int = 20,
        retries: int = 3,
        retry_backoff: float = 1.0,
        rate_limit_seconds: float = 0.15,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.retry_backoff = retry_backoff
        self.rate_limit_seconds = rate_limit_seconds
        self._last_request_time: float = 0.0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def fetch_resolved_markets(
        self,
        keywords: list[str] | None = None,
        min_volume: float = 1000.0,
        start_date: str = "2023-01-01",
        max_markets: int = 5000,
        batch_size: int = 100,
    ) -> list[HistoricalMarket]:
        """
        Paginate through closed=true markets and return those whose title
        contains at least one keyword from *keywords* and whose volume
        is at least *min_volume*.  Only markets that closed on or after
        *start_date* are included.
        """
        kws = [k.lower() for k in (keywords or DEFAULT_KEYWORDS)]
        cutoff = _parse_date(start_date)
        results: list[HistoricalMarket] = []
        offset = 0

        print(f"historical_fetch start keywords={len(kws)} min_volume={min_volume} start_date={start_date}")

        while len(results) < max_markets:
            batch = self._list_closed_markets(offset=offset, limit=batch_size)
            if not batch:
                break

            for row in batch:
                market = _market_from_row(row)
                if market is None:
                    continue
                if market.volume < min_volume:
                    continue
                if market.closed_time is not None and market.closed_time < cutoff:
                    continue
                title_lower = market.question.lower()
                if not any(kw in title_lower for kw in kws):
                    continue
                results.append(market)
                if len(results) >= max_markets:
                    break

            print(f"historical_fetch offset={offset} batch={len(batch)} matched={len(results)}")
            offset += batch_size

            # Stop if we got fewer rows than the batch size (last page)
            if len(batch) < batch_size:
                break

        print(f"historical_fetch done total_matched={len(results)}")
        return results

    def fetch_price_history(
        self,
        market: HistoricalMarket,
        fidelity_minutes: int = 360,
    ) -> dict[str, list[HistoricalPriceBar]]:
        """
        Fetch CLOB price history for both YES and NO tokens of *market*.
        Returns a dict with keys 'yes' and 'no', each containing a list
        of HistoricalPriceBar sorted by timestamp ascending.

        Requests are chunked into _PRICE_HISTORY_CHUNK_DAYS-day windows
        to avoid API timeouts on long-running markets.
        """
        results: dict[str, list[HistoricalPriceBar]] = {"yes": [], "no": []}

        start_ts = int(market.end_date.timestamp()) - 365 * 86400  # up to 1 year before end
        end_ts = int(market.end_date.timestamp())

        for side, token_id in [("yes", market.yes_token_id), ("no", market.no_token_id)]:
            if not token_id:
                continue
            bars = self._fetch_price_history_chunked(token_id, start_ts, end_ts, fidelity_minutes)
            results[side] = bars

        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _list_closed_markets(self, offset: int, limit: int) -> list[dict[str, Any]]:
        params = {"closed": "true", "limit": limit, "offset": offset}
        url = f"{GAMMA_BASE_URL}/markets?{urlencode(params)}"
        try:
            payload = self._get_json(url)
            return payload if isinstance(payload, list) else []
        except Exception as exc:
            print(f"historical_fetch_error offset={offset} error={type(exc).__name__}: {exc}")
            return []

    def _fetch_price_history_chunked(
        self,
        token_id: str,
        start_ts: int,
        end_ts: int,
        fidelity_minutes: int,
    ) -> list[HistoricalPriceBar]:
        chunk_seconds = _PRICE_HISTORY_CHUNK_DAYS * 86400
        all_bars: list[HistoricalPriceBar] = []
        chunk_start = start_ts

        while chunk_start < end_ts:
            chunk_end = min(chunk_start + chunk_seconds, end_ts)
            bars = self._fetch_price_history_single(token_id, chunk_start, chunk_end, fidelity_minutes)
            all_bars.extend(bars)
            chunk_start = chunk_end

        # Deduplicate by timestamp and sort
        seen: set[datetime] = set()
        deduped: list[HistoricalPriceBar] = []
        for bar in sorted(all_bars, key=lambda b: b.timestamp_utc):
            if bar.timestamp_utc not in seen:
                seen.add(bar.timestamp_utc)
                deduped.append(bar)
        return deduped

    def _fetch_price_history_single(
        self,
        token_id: str,
        start_ts: int,
        end_ts: int,
        fidelity_minutes: int,
    ) -> list[HistoricalPriceBar]:
        params = {
            "market": token_id,
            "startTs": start_ts,
            "endTs": end_ts,
            "fidelity": fidelity_minutes,
        }
        url = f"{CLOB_BASE_URL}/prices-history?{urlencode(params)}"
        try:
            payload = self._get_json(url)
            history = payload.get("history") if isinstance(payload, dict) else None
            if not isinstance(history, list):
                return []
            bars: list[HistoricalPriceBar] = []
            for item in history:
                if not isinstance(item, dict):
                    continue
                ts = _parse_unix_ts(item.get("t"))
                if ts is None:
                    continue
                bars.append(
                    HistoricalPriceBar(
                        timestamp_utc=ts,
                        open_price=_float(item.get("o")),
                        high_price=_float(item.get("h")),
                        low_price=_float(item.get("l")),
                        close_price=_float(item.get("c")),
                    )
                )
            return bars
        except Exception as exc:
            print(f"price_history_error token={token_id[:12]}... error={type(exc).__name__}: {exc}")
            return []

    def _get_json(self, url: str) -> Any:
        request = Request(url, headers={"User-Agent": "PolyMarketHistoricalFetcher/0.1"})
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._rate_limit()
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(self.retry_backoff * attempt)
        assert last_error is not None
        raise last_error

    def _rate_limit(self) -> None:
        if self.rate_limit_seconds <= 0:
            return
        elapsed = time.monotonic() - self._last_request_time
        wait = self.rate_limit_seconds - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_time = time.monotonic()


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _market_from_row(row: dict[str, Any]) -> HistoricalMarket | None:
    question = str(row.get("question") or "").strip()
    end_date_raw = row.get("endDate")
    if not question or not end_date_raw:
        return None

    end_date = _parse_iso(str(end_date_raw))
    if end_date is None:
        return None

    closed_time_raw = row.get("closedTime")
    closed_time = _parse_iso(str(closed_time_raw).replace(" ", "T")) if closed_time_raw else None

    outcomes = _parse_json_list(row.get("outcomes"))
    outcome_prices = _parse_json_list(row.get("outcomePrices"))
    token_ids = _parse_json_list(row.get("clobTokenIds"))

    yes_token_id = ""
    no_token_id = ""
    outcome_yes: float | None = None
    outcome_no: float | None = None

    for i, outcome in enumerate(outcomes):
        normalized = outcome.strip().lower()
        price = _float(outcome_prices[i]) if i < len(outcome_prices) else None
        token = token_ids[i] if i < len(token_ids) else ""
        if normalized == "yes":
            yes_token_id = token
            outcome_yes = price
        elif normalized == "no":
            no_token_id = token
            outcome_no = price

    # Skip markets with no token mapping
    if not yes_token_id and not no_token_id:
        return None

    # Validate outcome: for a resolved binary market, one side should be ~1.0 and the other ~0.0
    # Markets with outcomePrices still at 0/0 (unresolved or old AMM markets) are skipped
    if outcome_yes is not None and outcome_no is not None:
        total = outcome_yes + outcome_no
        if total < 0.5:
            # Likely unresolved or data issue
            outcome_yes = None
            outcome_no = None

    return HistoricalMarket(
        market_id=str(row.get("id") or ""),
        slug=str(row.get("slug") or ""),
        question=question,
        description=str(row.get("description") or ""),
        category=str(row.get("category") or ""),
        end_date=end_date,
        closed_time=closed_time,
        volume=_float(row.get("volumeNum") or row.get("volume")),
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        outcome_yes=outcome_yes,
        outcome_no=outcome_no,
        raw=row,
    )


def _parse_json_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _float(value: object) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_iso(raw: str) -> datetime | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            dt = datetime.strptime(raw.replace("+00:00", "Z"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    # Try fromisoformat as fallback
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _parse_unix_ts(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _parse_date(date_str: str) -> datetime:
    """Parse a YYYY-MM-DD string into a UTC-aware datetime at midnight."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return dt.replace(tzinfo=timezone.utc)
