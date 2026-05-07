from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from bot.shadow_replay import Settlement, replay_shadow_pnl
from bot.storage import WatchlistStore


class ShadowReplayTests(unittest.TestCase):
    def test_replays_open_marked_position_by_event_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "watchlist.sqlite")
            store = WatchlistStore(db_path)
            snapshot = _snapshot(no_mid=0.45)
            fill = _fill()

            store.insert_snapshots([snapshot])
            store.insert_shadow_fills([fill])
            store.insert_shadow_marks([_snapshot(no_mid=0.50)])

            replay = replay_shadow_pnl(db_path)

        records = replay["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "open_marked")
        self.assertEqual(records[0]["event_type"], "content_release")
        self.assertEqual(records[0]["pnl"], 0.04)
        self.assertEqual(replay["summary_by_event_type"][0]["unrealized_pnl"], 0.04)

    def test_settlement_closes_position_and_sets_realized_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "watchlist.sqlite")
            store = WatchlistStore(db_path)
            store.insert_snapshots([_snapshot(no_mid=0.45)])
            store.insert_shadow_fills([_fill()])

            replay = replay_shadow_pnl(
                db_path,
                [
                    Settlement(
                        slug="market",
                        side=None,
                        status="settled",
                        close_price=None,
                        winning_side="BUY_NO",
                        timestamp_utc="2026-06-30T00:00:00+00:00",
                        note="resolved no",
                    )
                ],
            )

        record = replay["records"][0]
        summary = replay["summary_by_event_type"][0]
        self.assertEqual(record["status"], "settled")
        self.assertEqual(record["current_price"], 1.0)
        self.assertEqual(record["pnl"], 0.54)
        self.assertEqual(summary["closed_count"], 1)
        self.assertEqual(summary["realized_pnl"], 0.54)


def _snapshot(no_mid: float) -> dict[str, object]:
    return {
        "timestamp_utc": "2026-04-27T00:00:00+00:00",
        "slug": "market",
        "label": "Market",
        "market_id": "1",
        "title": "New Artist Album before GTA VI?",
        "subject": "Artist",
        "platform": "streaming",
        "event_type": "content_release",
        "preferred_side": "BUY_NO",
        "preferred_price": no_mid,
        "in_target_band": True,
        "yes_bid": round(1 - no_mid - 0.005, 4),
        "yes_ask": round(1 - no_mid + 0.005, 4),
        "yes_mid": round(1 - no_mid, 4),
        "no_bid": round(no_mid - 0.005, 4),
        "no_ask": round(no_mid + 0.005, 4),
        "no_mid": no_mid,
        "evidence_score": 0.1,
        "evidence_confidence": 0.7,
        "model_side": "BUY_NO",
        "p_model": 0.45,
        "p_mid": round(1 - no_mid, 4),
        "edge": 0.04,
        "net_edge": 0.02,
        "signal_ok": True,
        "market_ok": True,
    }


def _fill() -> dict[str, object]:
    return {
        "timestamp_utc": "2026-04-27T00:01:00+00:00",
        "slug": "market",
        "label": "Market",
        "market_id": "1",
        "side": "BUY_NO",
        "fill_price": 0.46,
        "max_entry_price": 0.48,
        "net_edge": 0.02,
        "reason": "ask_at_or_below_max_entry",
        "snapshot_timestamp_utc": "2026-04-27T00:00:00+00:00",
    }


if __name__ == "__main__":
    unittest.main()
