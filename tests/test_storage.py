from __future__ import annotations

from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest

from bot.storage import WatchlistStore


class StorageTests(unittest.TestCase):
    def test_stores_evidence_runs_and_shadow_marks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "watchlist.sqlite")
            store = WatchlistStore(db_path)
            snapshot = {
                "timestamp_utc": "2026-04-27T00:00:00+00:00",
                "slug": "market",
                "market_id": "1",
                "subject": "Subject",
                "platform": "streaming",
                "event_type": "content_release",
                "preferred_side": "BUY_NO",
                "preferred_price": 0.45,
                "in_target_band": True,
                "yes_bid": 0.54,
                "yes_ask": 0.55,
                "yes_mid": 0.545,
                "no_bid": 0.45,
                "no_ask": 0.46,
                "no_mid": 0.455,
                "evidence_score": 0.1,
                "evidence_confidence": 0.7,
                "evidence_mode": "source",
                "evidence_source_url": "https://example.com/feed.xml",
                "evidence_source_type": "rss",
                "evidence_recent_entries_30d": 3,
                "evidence_keyword_hits_30d": 1,
                "evidence_latest_entry_age_days": 2.0,
                "evidence_preheat_score": 0.4,
                "evidence_cadence_score": 0.3,
                "evidence_partner_score": 0.2,
                "evidence_source_reliability": 0.7,
                "model_side": "BUY_NO",
                "p_model": 0.48,
                "p_mid": 0.545,
                "edge": 0.065,
                "net_edge": 0.015,
                "signal_ok": True,
                "market_ok": True,
            }
            fill = {
                "timestamp_utc": "2026-04-27T00:01:00+00:00",
                "slug": "market",
                "label": "Market",
                "market_id": "1",
                "side": "BUY_NO",
                "fill_price": 0.46,
                "max_entry_price": 0.47,
                "net_edge": 0.015,
                "reason": "ask_at_or_below_max_entry",
            }

            store.insert_snapshots([snapshot])
            store.insert_evidence_runs([snapshot])
            store.insert_shadow_fills([fill])
            self.assertEqual(store.filter_new_shadow_fills([fill]), [])
            marks = store.insert_shadow_marks([snapshot])

            self.assertEqual(marks, 1)
            with closing(sqlite3.connect(db_path)) as connection:
                evidence_count = connection.execute("SELECT COUNT(*) FROM evidence_runs").fetchone()[0]
                evidence_components = connection.execute(
                    "SELECT preheat_score, cadence_score, partner_score, source_reliability FROM evidence_runs"
                ).fetchone()
                mark = connection.execute(
                    "SELECT side, fill_price, mark_price, unrealized_pnl FROM shadow_marks"
                ).fetchone()

            self.assertEqual(evidence_count, 1)
            self.assertEqual(evidence_components, (0.4, 0.3, 0.2, 0.7))
            self.assertEqual(mark, ("BUY_NO", 0.46, 0.455, -0.005))


if __name__ == "__main__":
    unittest.main()
