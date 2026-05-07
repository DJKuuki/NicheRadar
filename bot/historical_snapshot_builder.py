"""
historical_snapshot_builder.py
-------------------------------
Generates virtual watchlist-compatible snapshots from historical CLOB price
data.  For each resolved market and each price bar timestamp, this module:

1. Reconstructs a synthetic Market object from the historical price.
2. Runs market_parser.parse_market() to classify event_type / platform.
3. Runs signal_engine.build_signal() with a *fallback* Evidence object
   (evidence_score=0 / confidence=0.5) so that historical calibration
   targets the base model without confounding from stale RSS data.
4. Runs risk_engine.allow_market() and allow_signal() filters.
5. Emits a snapshot dict that is structurally compatible with the
   watchlist_snapshots table (but stored in historical_snapshots).

Design decisions
----------------
- Evidence is intentionally zeroed out (method A from the plan) so that
  the resulting calibration isolates base_logit and time_weight accuracy.
- bid/ask are synthesised as close_price ± SYNTHETIC_HALF_SPREAD.
  This spread is kept small (0.01) to ensure most market_ok checks pass;
  the filter is applied anyway so analysts can see how many samples would
  survive real spread constraints.
- Only one snapshot per price bar is generated (not one per day). Use
  --history-fidelity to control granularity during the fetch step.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bot.config import BotConfig
from bot.historical_storage import HistoricalStore
from bot.market_parser import parse_market, utc_now
from bot.models import Evidence, Market
from bot.risk_engine import allow_market, allow_signal
from bot.signal_engine import build_signal

# Synthetic half-spread used when constructing bid/ask from a single close price.
# Keep small so that spread-based filters reflect real market conditions roughly.
SYNTHETIC_HALF_SPREAD = 0.01

# Minimum number of price bars a market must have before we bother building snapshots.
MIN_PRICE_BARS = 3

# Fallback evidence used for all historical snapshots (method A: ignore RSS evidence).
_ZERO_EVIDENCE = Evidence(
    score=0.0,
    confidence=0.5,
    reasons=["evidence_mode=historical_zero"],
    mode="historical_zero",
    preheat_score=0.0,
    cadence_score=0.0,
    partner_score=0.0,
    source_reliability=0.5,
)


def build_historical_snapshots(
    store: HistoricalStore,
    config: BotConfig | None = None,
    batch_size: int = 200,
    verbose: bool = True,
) -> int:
    """
    Iterate over all historical markets with price data in *store*, generate
    virtual snapshots, and persist them back into the historical_snapshots table.

    Returns the total number of snapshots inserted.
    """
    cfg = config or BotConfig(
        # Use relaxed constraints so more historical samples survive filtering.
        # The actual filter outcome (market_ok / signal_ok) is still recorded.
        max_days_to_expiry=365.0,
        min_days_to_expiry=0.0,
        min_volume=0.0,
        max_spread=0.99,
    )

    markets = store.load_markets(with_outcome_only=True)
    if verbose:
        print(f"historical_snapshot_builder start markets={len(markets)}")

    total_inserted = 0
    skipped_no_prices = 0
    skipped_no_parse = 0

    for i, market_row in enumerate(markets):
        market_id = str(market_row["market_id"])

        # Load price pairs (timestamps where both YES and NO data exist)
        pairs = store.load_price_pairs(market_id)
        if not pairs:
            # Fall back to YES-only if no NO prices
            yes_only = store.load_yes_prices(market_id)
            if len(yes_only) < MIN_PRICE_BARS:
                skipped_no_prices += 1
                continue
            # Synthesise NO from YES (1 - yes_close approximation)
            pairs = [(ts, yc, max(0.01, min(0.99, 1.0 - yc))) for ts, yc in yes_only]
        elif len(pairs) < MIN_PRICE_BARS:
            skipped_no_prices += 1
            continue

        end_date = _parse_iso(str(market_row.get("end_date") or ""))
        if end_date is None:
            skipped_no_prices += 1
            continue

        outcome_yes = market_row.get("outcome_yes")
        outcome_no = market_row.get("outcome_no")

        snapshots: list[dict[str, Any]] = []

        for ts_str, yes_close, no_close in pairs:
            ts = _parse_iso(ts_str)
            if ts is None:
                continue
            if ts >= end_date:
                continue  # Skip bars at or after market close

            snap = _build_snapshot(
                market_row=market_row,
                ts=ts,
                end_date=end_date,
                yes_close=yes_close,
                no_close=no_close,
                outcome_yes=outcome_yes,
                outcome_no=outcome_no,
                cfg=cfg,
            )
            if snap is None:
                skipped_no_parse += 1
                continue
            snapshots.append(snap)

        if snapshots:
            inserted = store.insert_snapshots(snapshots)
            total_inserted += inserted

        if verbose and (i + 1) % 50 == 0:
            print(
                f"historical_snapshot_builder progress={i+1}/{len(markets)} "
                f"inserted_so_far={total_inserted}"
            )

    if verbose:
        print(
            f"historical_snapshot_builder done "
            f"total_inserted={total_inserted} "
            f"skipped_no_prices={skipped_no_prices} "
            f"skipped_no_parse={skipped_no_parse}"
        )
    return total_inserted


def _build_snapshot(
    market_row: dict[str, Any],
    ts: datetime,
    end_date: datetime,
    yes_close: float,
    no_close: float,
    outcome_yes: float | None,
    outcome_no: float | None,
    cfg: BotConfig,
) -> dict[str, Any] | None:
    """
    Build a single virtual snapshot dict for the given price-bar timestamp.
    Returns None if market_parser cannot classify the market.
    """
    yes_bid = max(0.01, yes_close - SYNTHETIC_HALF_SPREAD)
    yes_ask = min(0.99, yes_close + SYNTHETIC_HALF_SPREAD)
    no_bid = max(0.01, no_close - SYNTHETIC_HALF_SPREAD)
    no_ask = min(0.99, no_close + SYNTHETIC_HALF_SPREAD)
    volume = float(market_row.get("volume") or 0.0)

    market = Market(
        market_id=str(market_row["market_id"]),
        title=str(market_row.get("question") or ""),
        description=str(market_row.get("description") or ""),
        rules=str(market_row.get("description") or ""),
        category=str(market_row.get("category") or ""),
        closes_at=end_date,
        volume=volume,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        metadata={
            "slug": market_row.get("slug"),
            "source": "historical",
        },
    )

    parsed = parse_market(market, ts)
    if parsed is None:
        return None

    signal = build_signal(parsed, _ZERO_EVIDENCE, cfg)
    market_ok, market_reasons = allow_market(parsed, cfg)
    signal_ok, signal_reasons = allow_signal(signal, cfg)

    days_before_close = (end_date - ts).total_seconds() / 86400.0

    return {
        "market_id": market.market_id,
        "slug": market_row.get("slug") or market.market_id,
        "timestamp_utc": ts.isoformat(),
        "days_before_close": round(days_before_close, 2),
        "days_to_expiry": round(parsed.days_to_expiry, 2),
        "event_type": parsed.event_type,
        "platform": parsed.platform,
        "action": parsed.action,
        "title": market.title,
        "yes_mid": round(market.mid_probability, 4),
        "no_mid": round(market.no_mid_probability, 4),
        "yes_ask": round(yes_ask, 4),
        "no_ask": round(no_ask, 4),
        "yes_bid": round(yes_bid, 4),
        "no_bid": round(no_bid, 4),
        "yes_spread": round(market.spread, 4),
        "no_spread": round(market.no_spread, 4),
        "spread": round(market.spread, 4),
        "volume": volume,
        "p_model": signal.p_model,
        "model_side": signal.side,
        "net_edge": signal.net_edge,
        "max_entry_price": signal.max_entry_price,
        "edge": signal.edge,
        "confidence": signal.confidence,
        "preferred_side": signal.side,  # use model side as preferred_side for backtest compat
        "market_ok": market_ok,
        "market_reasons": market_reasons,
        "signal_ok": signal_ok,
        "signal_reasons": signal_reasons,
        "signal_reasons_detail": signal.reasons,
        "evidence_score": 0.0,
        "evidence_mode": "historical_zero",
        "outcome_yes": outcome_yes,
        "outcome_no": outcome_no,
        "snapshot_source": "historical",
    }


def _parse_iso(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None
