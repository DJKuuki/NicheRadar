from __future__ import annotations

import json
from dataclasses import dataclass
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from bot.http_cache import HttpCache, RateLimiter


GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"


@dataclass(frozen=True)
class GammaMarketQuery:
    limit: int = 20
    closed: bool = False


class PolymarketApiClient:
    def __init__(
        self,
        timeout: int = 15,
        retries: int = 3,
        retry_backoff: float = 0.75,
        cache_path: str | None = None,
        gamma_cache_seconds: float = 0,
        book_cache_seconds: float = 0,
        rate_limit_seconds: float = 0,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.retry_backoff = retry_backoff
        self.gamma_cache_seconds = gamma_cache_seconds
        self.book_cache_seconds = book_cache_seconds
        self.cache = HttpCache(cache_path) if cache_path else None
        self.rate_limiter = RateLimiter(rate_limit_seconds)

    def list_markets(self, query: GammaMarketQuery) -> list[dict[str, Any]]:
        return self.list_markets_by_params(
            {
            "limit": query.limit,
            "closed": str(query.closed).lower(),
            }
        )

    def list_markets_by_params(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        url = f"{GAMMA_BASE_URL}/markets?{urlencode(params)}"
        payload = self._get_json(url, cache_seconds=self.gamma_cache_seconds)
        if not isinstance(payload, list):
            raise ValueError("Unexpected Gamma API response format.")
        return payload

    def get_book(self, token_id: str) -> dict[str, Any]:
        url = f"{CLOB_BASE_URL}/book?{urlencode({'token_id': token_id})}"
        payload = self._get_json(url, cache_seconds=self.book_cache_seconds)
        if not isinstance(payload, dict):
            raise ValueError("Unexpected CLOB book response format.")
        return payload

    def _get_json(self, url: str, cache_seconds: float = 0) -> Any:
        if self.cache is not None:
            cached = self.cache.get(url)
            if cached is not None:
                return json.loads(cached)

        request = Request(url, headers={"User-Agent": "PolyMarketShadowBot/0.1"})
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                self.rate_limiter.wait()
                with urlopen(request, timeout=self.timeout) as response:
                    response_text = response.read().decode("utf-8")
                if self.cache is not None:
                    self.cache.set(url, response_text, cache_seconds)
                return json.loads(response_text)
            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(self.retry_backoff * attempt)
        assert last_error is not None
        raise last_error
