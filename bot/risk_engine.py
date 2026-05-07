from __future__ import annotations

from bot.config import BotConfig
from bot.models import ParsedMarket, Signal


def allow_market(parsed: ParsedMarket, config: BotConfig) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    market = parsed.market

    if parsed.days_to_expiry < config.min_days_to_expiry or parsed.days_to_expiry > config.max_days_to_expiry:
        reasons.append("expiry_out_of_range")
    if market.volume < config.min_volume:
        reasons.append("volume_too_low")
    if max(market.spread, market.no_spread) > config.max_spread:
        reasons.append("spread_too_wide")
    if parsed.platform == "unknown":
        reasons.append("unsupported_platform")

    return (len(reasons) == 0, reasons)


def allow_signal(signal: Signal, config: BotConfig) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if signal.net_edge < config.min_net_edge:
        reasons.append("net_edge_too_small")
    if signal.confidence < 0.55:
        reasons.append("confidence_too_low")
    if signal.max_entry_price <= 0.01:
        reasons.append("invalid_entry_price")
    return (len(reasons) == 0, reasons)
