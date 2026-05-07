from __future__ import annotations

from bot.models import ParsedMarket, Signal, TradeIdea


def build_trade_idea(parsed: ParsedMarket, signal: Signal) -> TradeIdea:
    return TradeIdea(
        market_id=parsed.market.market_id,
        title=parsed.market.title,
        action="PLACE_MAKER_ORDER",
        side=signal.side,
        target_price=signal.max_entry_price,
        net_edge=signal.net_edge,
        reasons=signal.reasons,
    )
