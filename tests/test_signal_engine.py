from __future__ import annotations

from datetime import datetime, timezone
import unittest

from bot.config import BotConfig
from bot.models import Evidence, Market, ParsedMarket
from bot.signal_engine import build_signal


class SignalEngineTests(unittest.TestCase):
    def _market(self, title: str, yes_bid: float = 0.50, yes_ask: float = 0.52) -> Market:
        return Market(
            market_id=title,
            title=title,
            description="",
            rules="",
            category="",
            closes_at=datetime.now(timezone.utc),
            volume=1000,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=round(1 - yes_ask, 4),
            no_ask=round(1 - yes_bid, 4),
        )

    def test_buy_no_edge_is_calculated_against_no_mid(self) -> None:
        market = Market(
            market_id="1",
            title="Will Example be released?",
            description="",
            rules="",
            category="",
            closes_at=datetime.now(timezone.utc),
            volume=1000,
            yes_bid=0.70,
            yes_ask=0.72,
            no_bid=0.28,
            no_ask=0.30,
        )
        parsed = ParsedMarket(
            market=market,
            event_type="content_release",
            subject="Example",
            platform="openai",
            action="release",
            days_to_expiry=5,
        )
        evidence = Evidence(score=-1.0, confidence=0.9, reasons=[])

        signal = build_signal(parsed, evidence, BotConfig())

        self.assertEqual(signal.side, "BUY_NO")
        self.assertGreater(signal.edge, 0)
        self.assertGreater(signal.net_edge, 0)
        self.assertGreater(signal.max_entry_price, market.no_mid_probability)

    def test_music_release_uses_music_profile(self) -> None:
        parsed = ParsedMarket(
            market=self._market("Will Artist release a new album?"),
            event_type="content_release",
            subject="Artist",
            platform="streaming",
            action="release",
            days_to_expiry=5,
        )
        evidence = Evidence(
            score=0.4,
            confidence=0.8,
            reasons=[],
            preheat_score=0.8,
            cadence_score=0.4,
            partner_score=0.2,
        )

        signal = build_signal(parsed, evidence, BotConfig())

        self.assertIn("model_profile=music_release", signal.reasons)

    def test_ai_model_release_uses_default_content_profile(self) -> None:
        parsed = ParsedMarket(
            market=self._market("Will GPT-6 be released before GTA VI?"),
            event_type="content_release",
            subject="GPT-6",
            platform="openai",
            action="release",
            days_to_expiry=90,
        )
        evidence = Evidence(
            score=0.2,
            confidence=0.8,
            reasons=[],
            preheat_score=0.0,
            cadence_score=0.9,
            partner_score=0.1,
        )

        signal = build_signal(parsed, evidence, BotConfig())

        self.assertIn("model_profile=default_content", signal.reasons)

    def test_product_release_is_more_conservative_than_music_release(self) -> None:
        evidence = Evidence(
            score=0.4,
            confidence=0.8,
            reasons=[],
            preheat_score=0.8,
            cadence_score=0.4,
            partner_score=0.2,
        )
        product = build_signal(
            ParsedMarket(
                market=self._market("Will Tesla release Optimus?"),
                event_type="content_release",
                subject="Tesla",
                platform="tesla",
                action="release",
                days_to_expiry=5,
            ),
            evidence,
            BotConfig(),
        )
        music = build_signal(
            ParsedMarket(
                market=self._market("Will Artist release a new album?"),
                event_type="content_release",
                subject="Artist",
                platform="streaming",
                action="release",
                days_to_expiry=5,
            ),
            evidence,
            BotConfig(),
        )

        self.assertIn("model_profile=product_release", product.reasons)
        self.assertLess(product.p_model, music.p_model)

    def test_not_ipo_inverts_ipo_evidence(self) -> None:
        parsed = ParsedMarket(
            market=self._market("Will OpenAI not IPO by December 31, 2026?", yes_bid=0.67, yes_ask=0.69),
            event_type="ipo_event",
            subject="OpenAI",
            platform="openai",
            action="not_ipo",
            days_to_expiry=60,
        )
        evidence = Evidence(
            score=0.7,
            confidence=0.8,
            reasons=[],
            preheat_score=0.7,
            cadence_score=0.6,
            partner_score=0.1,
        )

        signal = build_signal(parsed, evidence, BotConfig())

        self.assertIn("model_profile=ipo_event", signal.reasons)
        self.assertEqual(signal.side, "BUY_NO")


if __name__ == "__main__":
    unittest.main()
