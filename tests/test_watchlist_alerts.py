from __future__ import annotations

import unittest

from bot.watchlist import build_watchlist_alerts, load_watchlist


class WatchlistAlertTests(unittest.TestCase):
    def test_alerts_include_band_signal_and_evidence_changes(self) -> None:
        previous = {
            "market": {
                "slug": "market",
                "timestamp_utc": "old",
                "in_target_band": False,
                "signal_ok": False,
                "evidence_score": 0.10,
            }
        }
        current = [
            {
                "slug": "market",
                "timestamp_utc": "new",
                "in_target_band": True,
                "signal_ok": True,
                "evidence_score": 0.30,
            }
        ]

        alerts = build_watchlist_alerts(previous, current, evidence_jump_threshold=0.15)

        self.assertEqual(len(alerts), 1)
        self.assertEqual(
            alerts[0]["alert_reasons"],
            ["entered_target_band", "signal_turned_ok", "evidence_score_jump"],
        )

    def test_load_watchlist_rejects_invalid_side(self) -> None:
        path = "tests/invalid_watchlist.json"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                '[{"slug":"x","label":"X","preferred_side":"YES","entry_band_low":0.1,"entry_band_high":0.2,"note":""}]'
            )
        try:
            with self.assertRaises(ValueError):
                load_watchlist(path)
        finally:
            import os

            os.remove(path)


if __name__ == "__main__":
    unittest.main()
