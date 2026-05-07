"""Tests for historical_fetcher.py"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from bot.historical_fetcher import (
    HistoricalFetcher,
    HistoricalMarket,
    HistoricalPriceBar,
    _market_from_row,
    _parse_iso,
    _parse_unix_ts,
)


class TestMarketFromRow(unittest.TestCase):
    def _base_row(self, **kwargs) -> dict:
        row = {
            "id": "1234",
            "question": "Will Taylor Swift release an album by Dec 2024?",
            "endDate": "2024-12-31T00:00:00Z",
            "closedTime": "2024-12-15T10:00:00Z",
            "volumeNum": 5000.0,
            "category": "Pop-Culture",
            "slug": "taylor-swift-album-2024",
            "description": "Market on Taylor Swift album release.",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["1", "0"]',
            "clobTokenIds": '["token_yes_123", "token_no_456"]',
        }
        row.update(kwargs)
        return row

    def test_basic_parse(self):
        market = _market_from_row(self._base_row())
        self.assertIsNotNone(market)
        assert market is not None
        self.assertEqual(market.market_id, "1234")
        self.assertEqual(market.yes_token_id, "token_yes_123")
        self.assertEqual(market.no_token_id, "token_no_456")
        self.assertAlmostEqual(market.outcome_yes, 1.0)
        self.assertAlmostEqual(market.outcome_no, 0.0)

    def test_missing_question_returns_none(self):
        row = self._base_row(question="")
        self.assertIsNone(_market_from_row(row))

    def test_missing_end_date_returns_none(self):
        row = self._base_row(endDate=None)
        self.assertIsNone(_market_from_row(row))

    def test_unresolved_market_outcome_none(self):
        # Both outcome prices are 0 — unresolved/old AMM market
        row = self._base_row(outcomePrices='["0", "0"]')
        market = _market_from_row(row)
        self.assertIsNotNone(market)
        assert market is not None
        self.assertIsNone(market.outcome_yes)
        self.assertIsNone(market.outcome_no)

    def test_no_outcome_yes(self):
        row = self._base_row(outcomePrices='["0", "1"]')
        market = _market_from_row(row)
        self.assertIsNotNone(market)
        assert market is not None
        self.assertAlmostEqual(market.outcome_yes, 0.0)
        self.assertAlmostEqual(market.outcome_no, 1.0)

    def test_volume_parsed(self):
        market = _market_from_row(self._base_row(volumeNum=12345.67))
        self.assertIsNotNone(market)
        assert market is not None
        self.assertAlmostEqual(market.volume, 12345.67)


class TestParseIso(unittest.TestCase):
    def test_utc_z(self):
        dt = _parse_iso("2024-01-15T12:00:00Z")
        self.assertIsNotNone(dt)
        assert dt is not None
        self.assertEqual(dt.year, 2024)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_offset_plus_zero(self):
        dt = _parse_iso("2024-06-01T00:00:00+00:00")
        self.assertIsNotNone(dt)

    def test_empty_returns_none(self):
        self.assertIsNone(_parse_iso(""))


class TestParseUnixTs(unittest.TestCase):
    def test_valid_ts(self):
        dt = _parse_unix_ts(1704067200)  # 2024-01-01 00:00:00 UTC
        self.assertIsNotNone(dt)
        assert dt is not None
        self.assertEqual(dt.year, 2024)

    def test_none_returns_none(self):
        self.assertIsNone(_parse_unix_ts(None))

    def test_string_ts(self):
        dt = _parse_unix_ts("1704067200")
        self.assertIsNotNone(dt)


class TestFetchResolvedMarkets(unittest.TestCase):
    """Integration-style tests with mocked HTTP."""

    def _make_row(self, market_id: str, question: str, volume: float = 5000.0) -> dict:
        return {
            "id": market_id,
            "question": question,
            "endDate": "2024-06-30T00:00:00Z",
            "closedTime": "2024-06-28T00:00:00Z",
            "volumeNum": volume,
            "category": "Entertainment",
            "slug": f"slug-{market_id}",
            "description": "Test market",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["1", "0"]',
            "clobTokenIds": f'["yes_{market_id}", "no_{market_id}"]',
        }

    @patch.object(HistoricalFetcher, "_get_json")
    def test_keyword_filter(self, mock_get):
        rows = [
            self._make_row("1", "Will Taylor Swift release an album?"),
            self._make_row("2", "Will it rain tomorrow?"),  # no keyword match
            self._make_row("3", "New song by Drake?"),
        ]
        mock_get.return_value = rows

        fetcher = HistoricalFetcher()
        markets = fetcher.fetch_resolved_markets(
            keywords=["release", "album", "song"],
            min_volume=1000.0,
            start_date="2024-01-01",
            max_markets=10,
            batch_size=100,
        )
        market_ids = {m.market_id for m in markets}
        self.assertIn("1", market_ids)
        self.assertIn("3", market_ids)
        self.assertNotIn("2", market_ids)

    @patch.object(HistoricalFetcher, "_get_json")
    def test_volume_filter(self, mock_get):
        rows = [
            self._make_row("1", "Will a new album release?", volume=500.0),   # below min
            self._make_row("2", "New song announcement?", volume=5000.0),
        ]
        mock_get.return_value = rows

        fetcher = HistoricalFetcher()
        markets = fetcher.fetch_resolved_markets(
            min_volume=1000.0,
            start_date="2024-01-01",
        )
        market_ids = {m.market_id for m in markets}
        self.assertNotIn("1", market_ids)
        self.assertIn("2", market_ids)

    @patch.object(HistoricalFetcher, "_get_json")
    def test_empty_batch_stops_pagination(self, mock_get):
        mock_get.return_value = []
        fetcher = HistoricalFetcher()
        markets = fetcher.fetch_resolved_markets(start_date="2024-01-01")
        self.assertEqual(len(markets), 0)


class TestFetchPriceHistory(unittest.TestCase):
    @patch.object(HistoricalFetcher, "_get_json")
    def test_price_history_parsed(self, mock_get):
        mock_get.return_value = {
            "history": [
                {"t": 1704067200, "o": 0.4, "h": 0.6, "l": 0.3, "c": 0.55},
                {"t": 1704153600, "o": 0.55, "h": 0.65, "l": 0.50, "c": 0.60},
            ]
        }
        fetcher = HistoricalFetcher()
        market = HistoricalMarket(
            market_id="test",
            slug="test",
            question="Test?",
            description="",
            category="",
            end_date=datetime(2024, 2, 1, tzinfo=timezone.utc),
            closed_time=None,
            volume=5000.0,
            yes_token_id="yes_token",
            no_token_id="no_token",
            outcome_yes=1.0,
            outcome_no=0.0,
        )
        result = fetcher.fetch_price_history(market, fidelity_minutes=360)
        self.assertIn("yes", result)
        # Should have at least some bars (exact count depends on chunking)
        # Since we mock to return 2 bars per chunk, and we chunk 1-year into 15-day chunks
        # all chunks will return the same 2 mocked bars, deduplicated to 2
        self.assertGreaterEqual(len(result["yes"]), 2)
        self.assertAlmostEqual(result["yes"][0].close_price, 0.55)


if __name__ == "__main__":
    unittest.main()
