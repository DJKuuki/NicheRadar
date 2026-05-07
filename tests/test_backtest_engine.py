from __future__ import annotations

import unittest

from bot.backtest_engine import BacktestStrategyParams, evaluate_shadow_entry


class BacktestEngineTests(unittest.TestCase):
    def test_replays_shadow_fill_entry_rule(self) -> None:
        entry = evaluate_shadow_entry(_snapshot(), BacktestStrategyParams(min_net_edge=0.01, max_spread=0.02))

        self.assertTrue(entry.eligible)
        self.assertEqual(entry.fill_price, 0.46)
        self.assertEqual(entry.reason, "ask_at_or_below_max_entry")

    def test_rejects_non_preferred_model_side(self) -> None:
        snapshot = _snapshot()
        snapshot["model_side"] = "BUY_YES"

        entry = evaluate_shadow_entry(snapshot)

        self.assertFalse(entry.eligible)
        self.assertEqual(entry.reason, "model_side_not_preferred")


def _snapshot() -> dict[str, object]:
    return {
        "market_ok": True,
        "signal_ok": True,
        "preferred_side": "BUY_NO",
        "model_side": "BUY_NO",
        "no_bid": 0.45,
        "no_ask": 0.46,
        "no_spread": 0.01,
        "max_entry_price": 0.48,
        "net_edge": 0.02,
        "event_type": "content_release",
        "platform": "streaming",
        "signal_reasons_detail": ["model_profile=music_release"],
    }


if __name__ == "__main__":
    unittest.main()
