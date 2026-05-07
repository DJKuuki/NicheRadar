from __future__ import annotations

import unittest

from bot.shadow import build_shadow_fills


class ShadowFillTests(unittest.TestCase):
    def test_builds_shadow_fill_when_ask_is_below_max_entry(self) -> None:
        fills = build_shadow_fills(
            [
                {
                    "timestamp_utc": "snapshot-time",
                    "slug": "market",
                    "label": "Market",
                    "market_id": "1",
                    "preferred_side": "BUY_NO",
                    "model_side": "BUY_NO",
                    "market_ok": True,
                    "signal_ok": True,
                    "no_ask": 0.45,
                    "max_entry_price": 0.50,
                    "net_edge": 0.05,
                }
            ]
        )

        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["side"], "BUY_NO")
        self.assertEqual(fills[0]["fill_price"], 0.45)

    def test_skips_shadow_fill_when_signal_is_filtered(self) -> None:
        fills = build_shadow_fills(
            [
                {
                    "slug": "market",
                    "preferred_side": "BUY_YES",
                    "model_side": "BUY_YES",
                    "market_ok": True,
                    "signal_ok": False,
                    "yes_ask": 0.45,
                    "max_entry_price": 0.50,
                }
            ]
        )

        self.assertEqual(fills, [])

    def test_skips_shadow_fill_when_market_is_filtered(self) -> None:
        fills = build_shadow_fills(
            [
                {
                    "slug": "market",
                    "preferred_side": "BUY_YES",
                    "model_side": "BUY_YES",
                    "market_ok": False,
                    "signal_ok": True,
                    "yes_ask": 0.45,
                    "max_entry_price": 0.50,
                }
            ]
        )

        self.assertEqual(fills, [])

    def test_skips_shadow_fill_when_model_side_differs_from_preferred_side(self) -> None:
        fills = build_shadow_fills(
            [
                {
                    "slug": "market",
                    "preferred_side": "BUY_NO",
                    "model_side": "BUY_YES",
                    "market_ok": True,
                    "signal_ok": True,
                    "yes_ask": 0.03,
                    "max_entry_price": 0.50,
                }
            ]
        )

        self.assertEqual(fills, [])


if __name__ == "__main__":
    unittest.main()
