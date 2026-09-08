import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

# Ensure project root is in sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from bot.tweet_market_scanner import TweetMarketScanner
from bot.tweet_poisson_model import BracketSpec, TweetProbabilityModel
from bot.xtracker_client import TrackingProgress, UserTracking, XTrackerClient


class TestTweetProbabilityModel(unittest.TestCase):
    def setUp(self):
        self.model = TweetProbabilityModel(
            mean_daily_rate=30.0,
            var_daily_rate=90.0,  # overdispersed: 3x
            min_edge_threshold=0.04,
            kelly_scale=0.25,
        )

    def test_forecast_pmf_zero_hours(self):
        pmf = self.model.forecast_pmf_remaining(remaining_hours=0.0, max_x=100)
        self.assertEqual(pmf[0], 1.0)
        self.assertEqual(sum(pmf), 1.0)

    def test_forecast_pmf_positive_hours_sums_to_one(self):
        pmf = self.model.forecast_pmf_remaining(remaining_hours=24.0, max_x=200)
        total_prob = sum(pmf)
        # Should sum to very close to 1.0 (within tail truncation error)
        self.assertAlmostEqual(total_prob, 1.0, places=3)

    def test_bracket_already_busted(self):
        bracket = BracketSpec(low=100, high=119, name="100-119 tweets")
        # Current count is already 150 -> cannot land in 100-119
        evaluation = self.model.evaluate_bracket(
            bracket=bracket,
            current_count=150,
            remaining_hours=10.0,
            market_ask=0.05,
            market_bid=0.01,
        )
        self.assertEqual(evaluation.fair_prob, 0.0)
        self.assertEqual(evaluation.recommendation, "PASS")

    def test_open_bracket_already_won(self):
        bracket = BracketSpec(low=200, high=None, name="200+ tweets")
        # Current count is already 250 -> guaranteed to land in 200+
        evaluation = self.model.evaluate_bracket(
            bracket=bracket,
            current_count=250,
            remaining_hours=12.0,
            market_ask=0.98,
            market_bid=0.95,
        )
        self.assertEqual(evaluation.fair_prob, 1.0)

    def test_maker_buy_signal_on_positive_edge(self):
        bracket = BracketSpec(low=200, high=219, name="200-219 tweets")
        # Set mock where fair prob is high (~0.80) and market ask is 0.50, bid is 0.45
        with patch.object(self.model, "forecast_pmf_remaining") as mock_pmf:
            # 10 remaining posts needed (current=200, need in [0, 19])
            pmf = [0.0] * 50
            for i in range(15):
                pmf[i] = 1.0 / 15.0  # uniformly in bracket
            mock_pmf.return_value = pmf

            eval_res = self.model.evaluate_bracket(
                bracket=bracket,
                current_count=200,
                remaining_hours=2.0,
                market_ask=0.50,
                market_bid=0.45,
            )
            self.assertGreaterEqual(eval_res.fair_prob, 0.90)
            self.assertEqual(eval_res.recommendation, "MAKER_BUY")
            self.assertIsNotNone(eval_res.kelly_fraction)
            self.assertGreater(eval_res.kelly_fraction, 0.0)


class TestBracketParsing(unittest.TestCase):
    def test_parse_range_bracket(self):
        q = "Will Elon Musk post 200-219 tweets from September 1 to September 8, 2026?"
        spec = TweetMarketScanner._parse_bracket_question(q, {"slug": "test-slug"})
        self.assertIsNotNone(spec)
        self.assertEqual(spec.low, 200)
        self.assertEqual(spec.high, 219)

    def test_parse_plus_bracket(self):
        q = "Will Elon Musk post 500+ tweets from September 1 to September 8, 2026?"
        spec = TweetMarketScanner._parse_bracket_question(q, {"slug": "test-plus-slug"})
        self.assertIsNotNone(spec)
        self.assertEqual(spec.low, 500)
        self.assertIsNone(spec.high)

    def test_parse_invalid_question(self):
        q = "Will Donald Trump visit Europe in 2026?"
        spec = TweetMarketScanner._parse_bracket_question(q, {"slug": "other"})
        self.assertIsNone(spec)


if __name__ == "__main__":
    unittest.main()
