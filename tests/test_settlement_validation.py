from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from bot.settlement_validation import format_settlement_validation, validate_settlements
from bot.shadow_replay import Settlement
from bot.storage import WatchlistStore


class SettlementValidationTests(unittest.TestCase):
    def test_reports_coverage_and_unsettled_fills(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "watchlist.sqlite")
            store = WatchlistStore(db_path)
            store.insert_shadow_fills([_fill("market-a", "BUY_YES"), _fill("market-b", "BUY_NO")])

            report = validate_settlements(
                db_path,
                [
                    Settlement(
                        slug="market-a",
                        side="BUY_YES",
                        status="settled",
                        close_price=None,
                        winning_side="BUY_YES",
                        timestamp_utc="2026-06-30T00:00:00+00:00",
                        note=None,
                    )
                ],
            )

        self.assertTrue(report["valid"])
        self.assertEqual(report["covered_fill_count"], 1)
        self.assertEqual(report["unsettled_fill_count"], 1)
        self.assertEqual(report["coverage_pct"], 0.5)
        self.assertTrue(any(line.startswith("settlement_validation") for line in format_settlement_validation(report)))

    def test_detects_duplicates_unknown_slug_and_price_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "watchlist.sqlite")
            store = WatchlistStore(db_path)
            store.insert_shadow_fills([_fill("market-a", "BUY_YES")])

            report = validate_settlements(
                db_path,
                [
                    Settlement(
                        slug="market-a",
                        side="BUY_YES",
                        status="settled",
                        close_price=0.0,
                        winning_side="BUY_YES",
                        timestamp_utc=None,
                        note=None,
                    ),
                    Settlement(
                        slug="market-a",
                        side="BUY_YES",
                        status="settled",
                        close_price=None,
                        winning_side="BUY_YES",
                        timestamp_utc=None,
                        note=None,
                    ),
                    Settlement(
                        slug="unknown-market",
                        side=None,
                        status="settled",
                        close_price=None,
                        winning_side="BUY_NO",
                        timestamp_utc=None,
                        note=None,
                    ),
                ],
            )

        self.assertFalse(report["valid"])
        self.assertEqual({issue["code"] for issue in report["errors"]}, {"winning_side_close_price_conflict", "duplicate_settlement"})
        self.assertEqual(report["warnings"][0]["code"], "unknown_slug")


def _fill(slug: str, side: str) -> dict[str, object]:
    return {
        "timestamp_utc": "2026-04-27T00:01:00+00:00",
        "slug": slug,
        "label": slug,
        "market_id": slug,
        "side": side,
        "fill_price": 0.46,
        "max_entry_price": 0.48,
        "net_edge": 0.02,
        "reason": "ask_at_or_below_max_entry",
        "snapshot_timestamp_utc": "2026-04-27T00:00:00+00:00",
    }


if __name__ == "__main__":
    unittest.main()
