from __future__ import annotations

from datetime import datetime, timezone
import unittest

from bot.market_parser import parse_market
from bot.models import Market


class MarketParserTests(unittest.TestCase):
    def test_parses_openai_ipo_market(self) -> None:
        parsed = parse_market(
            Market(
                market_id="1",
                title="Will OpenAI not IPO by December 31, 2026?",
                description="",
                rules="",
                category="",
                closes_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
                volume=1000,
                yes_bid=0.68,
                yes_ask=0.70,
                no_bid=0.30,
                no_ask=0.32,
            ),
            datetime(2026, 4, 27, tzinfo=timezone.utc),
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.subject, "OpenAI")
        self.assertEqual(parsed.platform, "openai")
        self.assertEqual(parsed.event_type, "ipo_event")
        self.assertEqual(parsed.action, "not_ipo")

    def test_parses_tesla_product_release_market(self) -> None:
        parsed = parse_market(
            Market(
                market_id="2",
                title="Will Tesla release Optimus by June 30, 2026?",
                description="",
                rules="",
                category="",
                closes_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
                volume=1000,
                yes_bid=0.04,
                yes_ask=0.05,
                no_bid=0.95,
                no_ask=0.96,
            ),
            datetime(2026, 4, 27, tzinfo=timezone.utc),
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.subject, "Tesla")
        self.assertEqual(parsed.platform, "tesla")
        self.assertEqual(parsed.event_type, "content_release")

    def test_parses_on_x_without_matching_random_x_letters(self) -> None:
        parsed = parse_market(
            Market(
                market_id="3",
                title="Will ArtistA announce a new album on X before May 5?",
                description="",
                rules="",
                category="",
                closes_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
                volume=1000,
                yes_bid=0.40,
                yes_ask=0.42,
                no_bid=0.58,
                no_ask=0.60,
            ),
            datetime(2026, 4, 27, tzinfo=timezone.utc),
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.platform, "x")

        gpt_parsed = parse_market(
            Market(
                market_id="4",
                title="Will GPT-6 be released before GTA VI?",
                description="",
                rules="",
                category="",
                closes_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
                volume=1000,
                yes_bid=0.60,
                yes_ask=0.61,
                no_bid=0.39,
                no_ask=0.40,
            ),
            datetime(2026, 4, 27, tzinfo=timezone.utc),
        )

        self.assertIsNotNone(gpt_parsed)
        assert gpt_parsed is not None
        self.assertEqual(gpt_parsed.platform, "openai")


if __name__ == "__main__":
    unittest.main()
