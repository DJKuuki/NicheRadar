from __future__ import annotations

import unittest

from bot.backtest_dataset import BacktestSample
from bot.backtest_metrics import build_backtest_metrics, calibration_bins, reliability_status


class BacktestMetricsTests(unittest.TestCase):
    def test_calibration_bins_and_pnl_summary(self) -> None:
        samples = [
            _sample("a", p_model=0.2, target_yes=0.0, pnl=-0.1),
            _sample("b", p_model=0.8, target_yes=1.0, pnl=0.2),
        ]

        metrics = build_backtest_metrics(samples, min_samples=2)
        bins = calibration_bins(samples, bins=5)

        self.assertEqual(metrics["summary"]["shadow_fills"], 2)
        self.assertEqual(metrics["summary"]["total_pnl"], 0.1)
        self.assertEqual(metrics["summary"]["win_rate"], 0.5)
        self.assertEqual(bins[1]["count"], 1)
        self.assertEqual(bins[4]["count"], 1)

    def test_sample_insufficiency_status(self) -> None:
        self.assertEqual(reliability_status([_sample("a")], min_samples=20), "insufficient")


def _sample(
    slug: str,
    p_model: float = 0.5,
    target_yes: float = 1.0,
    pnl: float = 0.1,
) -> BacktestSample:
    return BacktestSample(
        timestamp_utc="2026-04-27T00:00:00+00:00",
        slug=slug,
        market_id="1",
        event_type="content_release",
        platform="streaming",
        model_profile="music_release",
        preferred_side="BUY_YES",
        model_side="BUY_YES",
        p_model=p_model,
        p_mid=0.5,
        net_edge=0.02,
        evidence_score=0.1,
        preheat_score=0.1,
        cadence_score=0.1,
        partner_score=0.1,
        market_price=0.5,
        fill_eligible=True,
        fill_price=0.5,
        target_price=0.5 + pnl,
        target_yes_probability=target_yes,
        target_source="settlement_file",
        realized_pnl=pnl,
    )


if __name__ == "__main__":
    unittest.main()
