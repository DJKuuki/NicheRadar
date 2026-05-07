"""Tests for historical_snapshot_builder.py and historical_storage.py"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone

from bot.historical_fetcher import HistoricalMarket, HistoricalPriceBar
from bot.historical_storage import HistoricalStore
from bot.historical_snapshot_builder import build_historical_snapshots, SYNTHETIC_HALF_SPREAD


def _make_market(
    market_id: str = "mkt1",
    question: str = "Will Taylor Swift release a new album by Dec 2024?",
    outcome_yes: float = 1.0,
    outcome_no: float = 0.0,
    end_date: datetime | None = None,
) -> HistoricalMarket:
    return HistoricalMarket(
        market_id=market_id,
        slug=f"slug-{market_id}",
        question=question,
        description="Test market description.",
        category="Entertainment",
        end_date=end_date or datetime(2024, 12, 31, tzinfo=timezone.utc),
        closed_time=datetime(2024, 12, 28, tzinfo=timezone.utc),
        volume=5000.0,
        yes_token_id=f"yes_tok_{market_id}",
        no_token_id=f"no_tok_{market_id}",
        outcome_yes=outcome_yes,
        outcome_no=outcome_no,
    )


def _make_price_bar(ts: datetime, close: float) -> HistoricalPriceBar:
    return HistoricalPriceBar(
        timestamp_utc=ts,
        open_price=close - 0.02,
        high_price=close + 0.05,
        low_price=close - 0.05,
        close_price=close,
    )


class TestHistoricalStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self.db_path = self.tmp.name
        self.store = HistoricalStore(self.db_path)

    def test_insert_and_load_markets(self):
        markets = [_make_market("1"), _make_market("2")]
        inserted = self.store.insert_markets(markets)
        self.assertGreaterEqual(inserted, 0)
        loaded = self.store.load_markets(with_outcome_only=True)
        self.assertEqual(len(loaded), 2)

    def test_insert_ignores_duplicates(self):
        market = _make_market("dup")
        self.store.insert_markets([market])
        self.store.insert_markets([market])
        loaded = self.store.load_markets()
        self.assertEqual(len(loaded), 1)

    def test_insert_and_load_prices(self):
        market = _make_market("m1")
        self.store.insert_markets([market])
        bars = [
            _make_price_bar(datetime(2024, 6, 1, tzinfo=timezone.utc), 0.45),
            _make_price_bar(datetime(2024, 6, 2, tzinfo=timezone.utc), 0.50),
        ]
        count = self.store.insert_prices("m1", "yes", bars)
        self.assertEqual(count, 2)
        loaded = self.store.load_prices("m1", "yes")
        self.assertEqual(len(loaded), 2)

    def test_load_price_pairs(self):
        market = _make_market("m2")
        self.store.insert_markets([market])
        ts = datetime(2024, 6, 1, tzinfo=timezone.utc)
        self.store.insert_prices("m2", "yes", [_make_price_bar(ts, 0.60)])
        self.store.insert_prices("m2", "no", [_make_price_bar(ts, 0.40)])
        pairs = self.store.load_price_pairs("m2")
        self.assertEqual(len(pairs), 1)
        _, yes_close, no_close = pairs[0]
        self.assertAlmostEqual(yes_close, 0.60)
        self.assertAlmostEqual(no_close, 0.40)

    def test_market_ids_with_prices(self):
        market = _make_market("m3")
        self.store.insert_markets([market])
        self.assertNotIn("m3", self.store.market_ids_with_prices())
        self.store.insert_prices("m3", "yes", [_make_price_bar(datetime(2024, 1, 1, tzinfo=timezone.utc), 0.5)])
        self.assertIn("m3", self.store.market_ids_with_prices())

    def test_insert_snapshots(self):
        snaps = [
            {
                "market_id": "m4",
                "slug": "slug-m4",
                "timestamp_utc": "2024-06-01T00:00:00+00:00",
                "days_before_close": 30.0,
                "event_type": "content_release",
                "platform": "streaming",
                "yes_mid": 0.60,
                "no_mid": 0.40,
                "yes_ask": 0.61,
                "no_ask": 0.41,
                "yes_bid": 0.59,
                "no_bid": 0.39,
                "spread": 0.02,
                "p_model": 0.55,
                "model_side": "BUY_YES",
                "net_edge": 0.03,
                "max_entry_price": 0.52,
                "market_ok": True,
                "signal_ok": True,
                "outcome_yes": 1.0,
                "outcome_no": 0.0,
            }
        ]
        count = self.store.insert_snapshots(snaps)
        self.assertGreaterEqual(count, 0)
        loaded = self.store.load_snapshots(with_outcome_only=True, market_ok_only=True)
        self.assertEqual(len(loaded), 1)
        self.assertAlmostEqual(loaded[0]["outcome_yes"], 1.0)


class TestBuildHistoricalSnapshots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self.db_path = self.tmp.name
        self.store = HistoricalStore(self.db_path)

    def _populate(self, market_id: str, question: str, close_prices: list[float]) -> None:
        market = _make_market(
            market_id=market_id,
            question=question,
            end_date=datetime(2024, 12, 31, tzinfo=timezone.utc),
        )
        self.store.insert_markets([market])
        for i, close in enumerate(close_prices):
            ts = datetime(2024, 6, i + 1, tzinfo=timezone.utc)
            self.store.insert_prices(market_id, "yes", [_make_price_bar(ts, close)])
            self.store.insert_prices(market_id, "no", [_make_price_bar(ts, max(0.01, 1.0 - close))])

    def test_snapshots_generated_for_matched_market(self):
        self._populate(
            "album_mkt",
            "Will Rihanna release a new album by Dec 2024?",
            [0.30, 0.35, 0.40, 0.45],
        )
        total = build_historical_snapshots(self.store, verbose=False)
        self.assertGreater(total, 0)
        snaps = self.store.load_snapshots(with_outcome_only=True, market_ok_only=False)
        self.assertGreater(len(snaps), 0)

    def test_no_snapshots_for_unmatched_market(self):
        # A market that won't match any SOCIAL_KEYWORDS
        self._populate(
            "weather_mkt",
            "Will it rain in London on Christmas?",
            [0.50, 0.55, 0.60],
        )
        total = build_historical_snapshots(self.store, verbose=False)
        self.assertEqual(total, 0)

    def test_snapshot_has_outcome(self):
        self._populate(
            "ipo_mkt",
            "Will OpenAI IPO by Dec 2024?",
            [0.20, 0.25, 0.30, 0.28],
        )
        build_historical_snapshots(self.store, verbose=False)
        snaps = self.store.load_snapshots(with_outcome_only=True, market_ok_only=False)
        for snap in snaps:
            self.assertIn("outcome_yes", snap)

    def test_bars_before_end_date_only(self):
        market = _make_market(
            "time_mkt",
            "Will Taylor Swift release an album?",
            end_date=datetime(2024, 6, 3, tzinfo=timezone.utc),
        )
        self.store.insert_markets([market])
        # Price bars: one before end_date, one on end_date, one after
        for day, close in [(1, 0.4), (3, 0.5), (5, 0.6)]:
            ts = datetime(2024, 6, day, tzinfo=timezone.utc)
            self.store.insert_prices("time_mkt", "yes", [_make_price_bar(ts, close)])
            self.store.insert_prices("time_mkt", "no", [_make_price_bar(ts, 1.0 - close)])
        build_historical_snapshots(self.store, verbose=False)
        snaps = self.store.load_snapshots(with_outcome_only=False, market_ok_only=False)
        # Only bar on day 1 (before end_date June 3) should create a snapshot
        timestamps = [s["timestamp_utc"] for s in snaps]
        self.assertTrue(any("2024-06-01" in t for t in timestamps))
        self.assertFalse(any("2024-06-05" in t for t in timestamps))

    def test_synthetic_spread(self):
        self._populate(
            "spread_mkt",
            "Will Rihanna release an album by Dec 2024?",
            [0.50, 0.55, 0.60, 0.65],
        )
        build_historical_snapshots(self.store, verbose=False)
        snaps = self.store.load_snapshots(with_outcome_only=False, market_ok_only=False)
        for snap in snaps:
            if snap.get("yes_ask") and snap.get("yes_bid"):
                spread = snap["yes_ask"] - snap["yes_bid"]
                self.assertAlmostEqual(spread, 2 * SYNTHETIC_HALF_SPREAD, places=3)


if __name__ == "__main__":
    unittest.main()
