from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from bot.calibration import build_calibration_report, format_calibration_report, load_calibration_samples
from bot.shadow_replay import Settlement
from bot.storage import WatchlistStore


class CalibrationTests(unittest.TestCase):
    def test_builds_profile_calibration_from_shadow_settlements(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "watchlist.sqlite")
            store = WatchlistStore(db_path)
            snapshots = [_snapshot(index, p_model=0.35 + index * 0.03) for index in range(6)]
            fills = [_fill(index) for index in range(6)]
            settlements = [
                Settlement(
                    slug=f"market-{index}",
                    side=None,
                    status="settled",
                    close_price=None,
                    winning_side="BUY_YES" if index >= 3 else "BUY_NO",
                    timestamp_utc="2026-07-31T00:00:00+00:00",
                    note=None,
                )
                for index in range(6)
            ]

            store.insert_snapshots(snapshots)
            store.insert_shadow_fills(fills)

            report = build_calibration_report(db_path, settlements, min_samples=3)
            profile = report["profiles"][0]

            self.assertEqual(report["sample_count"], 6)
            self.assertEqual(profile["profile"], "music_release")
            self.assertEqual(profile["status"], "ok")
            self.assertIn("base_logit", profile["suggested_profile"])
            self.assertIn("evidence_weight", profile["suggested_profile"])
            self.assertTrue(any(line.startswith("calibration_profile") for line in format_calibration_report(report)))

    def test_mark_fallback_converts_buy_no_price_to_yes_probability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "watchlist.sqlite")
            store = WatchlistStore(db_path)
            snapshot = _snapshot(0, p_model=0.4, no_mid=0.72)
            store.insert_snapshots([snapshot])
            store.insert_shadow_fills([_fill(0, side="BUY_NO")])
            store.insert_shadow_marks([snapshot])

            samples = load_calibration_samples(db_path, [])

            self.assertEqual(len(samples), 1)
            self.assertEqual(samples[0].target_source, "latest_mark")
            self.assertAlmostEqual(samples[0].target_yes_probability, 0.28)


def _snapshot(index: int, p_model: float, no_mid: float = 0.6) -> dict[str, object]:
    yes_mid = round(1 - no_mid, 4)
    return {
        "timestamp_utc": f"2026-04-27T00:0{index}:00+00:00",
        "slug": f"market-{index}",
        "label": f"Market {index}",
        "market_id": str(index),
        "title": f"Will Artist {index} release a new album?",
        "subject": f"Artist {index}",
        "platform": "streaming",
        "event_type": "content_release",
        "preferred_side": "BUY_YES",
        "preferred_price": yes_mid,
        "in_target_band": True,
        "yes_bid": round(yes_mid - 0.005, 4),
        "yes_ask": round(yes_mid + 0.005, 4),
        "yes_mid": yes_mid,
        "no_bid": round(no_mid - 0.005, 4),
        "no_ask": round(no_mid + 0.005, 4),
        "no_mid": no_mid,
        "spread": 0.01,
        "evidence_score": 0.1 + index * 0.1,
        "evidence_confidence": 0.8,
        "evidence_preheat_score": 0.2 + index * 0.1,
        "evidence_cadence_score": 0.3,
        "evidence_partner_score": 0.1,
        "model_side": "BUY_YES",
        "p_model": p_model,
        "p_mid": yes_mid,
        "edge": 0.02,
        "net_edge": 0.01,
        "signal_ok": True,
        "market_ok": True,
        "days_to_expiry": 7.0,
        "signal_reasons_detail": [
            "model_profile=music_release",
            "event_type=content_release",
            "platform=streaming",
            "action=release",
        ],
    }


def _fill(index: int, side: str = "BUY_YES") -> dict[str, object]:
    return {
        "timestamp_utc": f"2026-04-27T00:0{index}:30+00:00",
        "slug": f"market-{index}",
        "label": f"Market {index}",
        "market_id": str(index),
        "side": side,
        "fill_price": 0.4,
        "max_entry_price": 0.45,
        "net_edge": 0.01,
        "reason": "ask_at_or_below_max_entry",
        "snapshot_timestamp_utc": f"2026-04-27T00:0{index}:00+00:00",
    }


if __name__ == "__main__":
    unittest.main()
