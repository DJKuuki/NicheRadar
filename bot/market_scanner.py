from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from bot.api import GammaMarketQuery, PolymarketApiClient
from bot.models import Market


def load_sample_markets(path: str) -> list[Market]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    markets: list[Market] = []
    for item in payload:
        markets.append(
            Market(
                market_id=item["market_id"],
                title=item["title"],
                description=item["description"],
                rules=item["rules"],
                category=item["category"],
                closes_at=datetime.fromisoformat(item["closes_at"]),
                volume=float(item["volume"]),
                yes_bid=float(item["yes_bid"]),
                yes_ask=float(item["yes_ask"]),
                no_bid=float(item["no_bid"]),
                no_ask=float(item["no_ask"]),
                outcomes=item.get("outcomes", []),
                token_ids=item.get("token_ids", []),
                outcome_token_ids=item.get("outcome_token_ids", {}),
                metadata=item.get("metadata", {}),
            )
        )
    return markets


def load_live_markets(
    limit: int = 20,
    include_books: bool = True,
    cache_path: str | None = None,
    gamma_cache_seconds: float = 0,
    book_cache_seconds: float = 0,
    rate_limit_seconds: float = 0,
) -> list[Market]:
    client = PolymarketApiClient(
        cache_path=cache_path,
        gamma_cache_seconds=gamma_cache_seconds,
        book_cache_seconds=book_cache_seconds,
        rate_limit_seconds=rate_limit_seconds,
    )
    rows = client.list_markets(GammaMarketQuery(limit=limit, closed=False))
    return _rows_to_markets(client, rows, include_books=include_books)


def load_live_markets_by_slugs(
    slugs: list[str],
    include_books: bool = True,
    cache_path: str | None = None,
    gamma_cache_seconds: float = 0,
    book_cache_seconds: float = 0,
    rate_limit_seconds: float = 0,
) -> list[Market]:
    client = PolymarketApiClient(
        cache_path=cache_path,
        gamma_cache_seconds=gamma_cache_seconds,
        book_cache_seconds=book_cache_seconds,
        rate_limit_seconds=rate_limit_seconds,
    )
    rows: list[dict[str, object]] = []
    for slug in slugs:
        try:
            rows.extend(client.list_markets_by_params({"slug": slug, "closed": "false"}))
        except Exception as exc:
            print(f"market_fetch_failed slug={slug} error={type(exc).__name__}")
    return _rows_to_markets(client, rows, include_books=include_books)


def _rows_to_markets(client: PolymarketApiClient, rows: list[dict[str, object]], include_books: bool) -> list[Market]:
    markets: list[Market] = []
    for row in rows:
        market = _market_from_gamma(row)
        if market is None:
            continue
        if include_books and market.token_ids:
            try:
                market = _apply_book_snapshot(client, market)
            except Exception as exc:
                market.metadata["book_error"] = type(exc).__name__
        markets.append(market)
    return markets

def _market_from_gamma(row: dict[str, object]) -> Market | None:
    question = str(row.get("question") or "").strip()
    end_date = row.get("endDate")
    if not question or not end_date:
        return None

    outcomes = _parse_json_list(row.get("outcomes"))
    token_ids = _parse_json_list(row.get("clobTokenIds"))
    outcome_token_ids = _build_outcome_token_map(outcomes, token_ids)
    outcome_prices = _parse_json_list(row.get("outcomePrices"))

    yes_bid = _to_float(row.get("bestBid"))
    yes_ask = _to_float(row.get("bestAsk"))
    yes_mid = _outcome_price(outcomes, outcome_prices, "yes")
    no_mid = _outcome_price(outcomes, outcome_prices, "no")
    if not yes_bid and yes_mid > 0:
        yes_bid = yes_mid
    if not yes_ask and yes_mid > 0:
        yes_ask = yes_mid
    no_bid = no_mid if no_mid > 0 else max(0.0, round(1 - yes_ask, 4))
    no_ask = no_mid if no_mid > 0 else max(0.0, round(1 - yes_bid, 4))

    return Market(
        market_id=str(row.get("id")),
        title=question,
        description=str(row.get("description") or ""),
        rules=str(row.get("description") or ""),
        category=str(row.get("category") or ""),
        closes_at=datetime.fromisoformat(str(end_date).replace("Z", "+00:00")),
        volume=_to_float(row.get("volumeNum") or row.get("volume")),
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        outcomes=outcomes,
        token_ids=token_ids,
        outcome_token_ids=outcome_token_ids,
        metadata={
            "slug": row.get("slug"),
            "condition_id": row.get("conditionId"),
            "yes_token_id": outcome_token_ids.get("yes"),
            "no_token_id": outcome_token_ids.get("no"),
            "best_bid": row.get("bestBid"),
            "best_ask": row.get("bestAsk"),
            "last_trade_price": row.get("lastTradePrice"),
            "spread": row.get("spread"),
        },
    )


def _apply_book_snapshot(client: PolymarketApiClient, market: Market) -> Market:
    yes_token_id = market.outcome_token_ids.get("yes") or (market.token_ids[0] if market.token_ids else "")
    no_token_id = market.outcome_token_ids.get("no")

    if yes_token_id:
        yes_book = client.get_book(yes_token_id)
        best_yes_bid = _best_price(yes_book.get("bids"), choose=max)
        best_yes_ask = _best_price(yes_book.get("asks"), choose=min)

        if best_yes_bid > 0:
            market.yes_bid = best_yes_bid
        if best_yes_ask > 0:
            market.yes_ask = best_yes_ask

        market.metadata["yes_book_timestamp"] = yes_book.get("timestamp")
        market.metadata["yes_last_trade_price"] = yes_book.get("last_trade_price")
        market.metadata["tick_size"] = yes_book.get("tick_size")

    if no_token_id:
        no_book = client.get_book(no_token_id)
        best_no_bid = _best_price(no_book.get("bids"), choose=max)
        best_no_ask = _best_price(no_book.get("asks"), choose=min)

        if best_no_bid > 0:
            market.no_bid = best_no_bid
        if best_no_ask > 0:
            market.no_ask = best_no_ask

        market.metadata["no_book_timestamp"] = no_book.get("timestamp")
        market.metadata["no_last_trade_price"] = no_book.get("last_trade_price")
    else:
        market.no_bid = max(0.0, round(1 - market.yes_ask, 4))
        market.no_ask = max(0.0, round(1 - market.yes_bid, 4))
    return market


def _best_price(levels: object, choose) -> float:
    if not isinstance(levels, list) or not levels:
        return 0.0
    prices = [_to_float(level.get("price")) for level in levels if isinstance(level, dict)]
    prices = [price for price in prices if price > 0]
    return round(choose(prices), 4) if prices else 0.0


def _parse_json_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _build_outcome_token_map(outcomes: list[str], token_ids: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for outcome, token_id in zip(outcomes, token_ids):
        normalized = outcome.strip().lower()
        if normalized in {"yes", "no"}:
            mapping[normalized] = token_id
    return mapping


def _outcome_price(outcomes: list[str], prices: list[str], outcome_name: str) -> float:
    for outcome, price in zip(outcomes, prices):
        if outcome.strip().lower() == outcome_name:
            return _to_float(price)
    return 0.0


def _to_float(value: object) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
