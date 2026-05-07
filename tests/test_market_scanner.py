from __future__ import annotations

import unittest

from bot.market_scanner import _apply_book_snapshot, _market_from_gamma


class FakeClient:
    def get_book(self, token_id: str) -> dict[str, object]:
        books = {
            "yes-token": {
                "bids": [{"price": "0.41"}],
                "asks": [{"price": "0.43"}],
                "timestamp": "yes-ts",
                "last_trade_price": "0.42",
                "tick_size": "0.01",
            },
            "no-token": {
                "bids": [{"price": "0.57"}],
                "asks": [{"price": "0.59"}],
                "timestamp": "no-ts",
                "last_trade_price": "0.58",
                "tick_size": "0.01",
            },
        }
        return books[token_id]


class MarketScannerTests(unittest.TestCase):
    def test_outcome_tokens_are_mapped_by_outcome_name(self) -> None:
        market = _market_from_gamma(
            {
                "id": "1",
                "question": "Will Example happen?",
                "endDate": "2026-05-01T00:00:00Z",
                "volumeNum": "1000",
                "outcomes": '["No", "Yes"]',
                "clobTokenIds": '["no-token", "yes-token"]',
                "outcomePrices": '["0.58", "0.42"]',
            }
        )

        self.assertIsNotNone(market)
        assert market is not None
        self.assertEqual(market.outcome_token_ids["yes"], "yes-token")
        self.assertEqual(market.outcome_token_ids["no"], "no-token")
        self.assertEqual(market.yes_bid, 0.42)
        self.assertEqual(market.no_bid, 0.58)

    def test_book_snapshot_reads_yes_and_no_books(self) -> None:
        market = _market_from_gamma(
            {
                "id": "1",
                "question": "Will Example happen?",
                "endDate": "2026-05-01T00:00:00Z",
                "volumeNum": "1000",
                "outcomes": '["No", "Yes"]',
                "clobTokenIds": '["no-token", "yes-token"]',
                "outcomePrices": '["0.58", "0.42"]',
            }
        )
        assert market is not None

        updated = _apply_book_snapshot(FakeClient(), market)

        self.assertEqual(updated.yes_bid, 0.41)
        self.assertEqual(updated.yes_ask, 0.43)
        self.assertEqual(updated.no_bid, 0.57)
        self.assertEqual(updated.no_ask, 0.59)
        self.assertEqual(updated.metadata["yes_token_id"], "yes-token")
        self.assertEqual(updated.metadata["no_token_id"], "no-token")


if __name__ == "__main__":
    unittest.main()
