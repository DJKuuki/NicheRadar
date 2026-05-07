from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from bot.reporting import (
    build_dashboard_report,
    format_dashboard_report,
    write_dashboard_html,
    write_dashboard_markdown,
)
from bot.storage import WatchlistStore


class ReportingTests(unittest.TestCase):
    def test_dashboard_report_summarizes_edges_alerts_and_shadow_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "watchlist.sqlite")
            report_path = str(Path(directory) / "dashboard.md")
            html_path = str(Path(directory) / "dashboard.html")
            store = WatchlistStore(db_path)
            snapshot = _snapshot()
            store.insert_snapshots([snapshot])
            store.insert_alerts(
                [
                    {
                        "timestamp_utc": "2026-04-27T00:02:00+00:00",
                        "slug": "market",
                        "label": "Market",
                        "market_id": "1",
                        "title": "Market",
                        "alert_reasons": ["signal_turned_ok", "evidence_score_jump"],
                        "previous_timestamp_utc": "2026-04-27T00:00:00+00:00",
                        "current_timestamp_utc": "2026-04-27T00:01:00+00:00",
                    }
                ]
            )
            store.insert_shadow_fills([_fill()])
            store.insert_shadow_marks([snapshot])

            report = build_dashboard_report(db_path, limit=5)
            write_dashboard_markdown(report_path, report)
            write_dashboard_html(html_path, report)
            report_text = Path(report_path).read_text(encoding="utf-8")
            html_text = Path(html_path).read_text(encoding="utf-8")

            self.assertEqual(report["counts"]["snapshots"], 1)
            self.assertEqual(report["edge_by_event_type"][0]["event_type"], "content_release")
            self.assertEqual(report["edge_by_event_type"][0]["avg_net_edge"], 0.02)
            self.assertEqual(report["alert_summary"][0]["reason"], "signal_turned_ok")
            self.assertEqual(report["shadow_summary_by_event_type"][0]["unrealized_pnl"], -0.005)
            self.assertTrue(report_text.startswith("# PolyMarket Shadow Report"))
            self.assertIn("<title>PolyMarket Shadow Dashboard</title>", html_text)
            self.assertIn("Portfolio Risk", html_text)
            self.assertIn("Latest Markets", html_text)
            self.assertIn("Market", html_text)
            self.assertTrue(any(line.startswith("edge_summary") for line in format_dashboard_report(report)))


def _snapshot() -> dict[str, object]:
    return {
        "timestamp_utc": "2026-04-27T00:01:00+00:00",
        "slug": "market",
        "label": "Market",
        "market_id": "1",
        "title": "New Artist Album before GTA VI?",
        "subject": "Artist",
        "platform": "streaming",
        "event_type": "content_release",
        "preferred_side": "BUY_NO",
        "preferred_price": 0.455,
        "in_target_band": True,
        "yes_bid": 0.54,
        "yes_ask": 0.55,
        "yes_mid": 0.545,
        "no_bid": 0.45,
        "no_ask": 0.46,
        "no_mid": 0.455,
        "evidence_score": 0.1,
        "evidence_confidence": 0.7,
        "model_side": "BUY_NO",
        "p_model": 0.45,
        "p_mid": 0.545,
        "edge": 0.07,
        "net_edge": 0.02,
        "signal_ok": True,
        "market_ok": True,
    }


def _fill() -> dict[str, object]:
    return {
        "timestamp_utc": "2026-04-27T00:00:30+00:00",
        "slug": "market",
        "label": "Market",
        "market_id": "1",
        "side": "BUY_NO",
        "fill_price": 0.46,
        "max_entry_price": 0.48,
        "net_edge": 0.02,
        "reason": "ask_at_or_below_max_entry",
        "snapshot_timestamp_utc": "2026-04-27T00:01:00+00:00",
    }


if __name__ == "__main__":
    unittest.main()
