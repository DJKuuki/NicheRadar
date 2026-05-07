from __future__ import annotations

from datetime import datetime, timezone
import unittest

from bot.config import BotConfig
from bot.models import Market, ParsedMarket
from bot.risk_engine import allow_market


class RiskEngineTests(unittest.TestCase):
    def test_rejects_wide_no_side_spread(self) -> None:
        parsed = ParsedMarket(
            market=Market(
                market_id="1",
                title="Will Example happen?",
                description="",
                rules="",
                category="",
                closes_at=datetime.now(timezone.utc),
                volume=1000,
                yes_bid=0.40,
                yes_ask=0.41,
                no_bid=0.40,
                no_ask=0.60,
            ),
            event_type="content_release",
            subject="Example",
            platform="streaming",
            action="release",
            days_to_expiry=5,
        )

        allowed, reasons = allow_market(parsed, BotConfig(max_spread=0.12))

        self.assertFalse(allowed)
        self.assertIn("spread_too_wide", reasons)


if __name__ == "__main__":
    unittest.main()
