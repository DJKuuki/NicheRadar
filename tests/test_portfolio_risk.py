from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from bot.config import BotConfig
from bot.portfolio_risk import filter_shadow_fills_for_portfolio, load_portfolio_risk_state
from bot.storage import WatchlistStore


class PortfolioRiskTests(unittest.TestCase):
    def test_blocks_candidate_when_event_type_exposure_limit_is_reached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "watchlist.sqlite")
            store = WatchlistStore(db_path)
            store.insert_snapshots([_snapshot("existing", "content_release", 0.45)])
            store.insert_shadow_fills([_fill("existing", "content_release", 20.0)])
            config = BotConfig(shadow_bankroll=1000, shadow_position_risk_pct=0.02, max_event_type_risk_pct=0.03)

            accepted, state = filter_shadow_fills_for_portfolio(
                db_path,
                [_fill("candidate", "content_release", 20.0)],
                config,
            )

        self.assertEqual(state.total_exposure, 20.0)
        self.assertEqual(accepted, [])

    def test_circuit_breaker_blocks_new_candidates_after_portfolio_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "watchlist.sqlite")
            store = WatchlistStore(db_path)
            store.insert_snapshots([_snapshot("existing", "ipo_event", 0.01)])
            store.insert_shadow_fills([_fill("existing", "ipo_event", 20.0)])
            store.insert_shadow_marks([_snapshot("existing", "ipo_event", 0.01)])
            config = BotConfig(shadow_bankroll=1000, shadow_position_risk_pct=0.02, circuit_breaker_loss_pct=0.01)

            state = load_portfolio_risk_state(db_path, config)
            accepted, _ = filter_shadow_fills_for_portfolio(
                db_path,
                [_fill("candidate", "content_release", 20.0)],
                config,
            )

        self.assertTrue(state.circuit_breaker_active)
        self.assertIn("portfolio_loss_limit", state.circuit_breaker_reasons)
        self.assertEqual(accepted, [])


def _snapshot(slug: str, event_type: str, no_mid: float) -> dict[str, object]:
    return {
        "timestamp_utc": "2026-04-27T00:00:00+00:00",
        "slug": slug,
        "label": slug,
        "market_id": slug,
        "title": slug,
        "subject": slug,
        "platform": "streaming",
        "event_type": event_type,
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
        "p_model": round(1 - no_mid, 4),
        "p_mid": round(1 - no_mid, 4),
        "edge": 0.02,
        "net_edge": 0.02,
        "signal_ok": True,
        "market_ok": True,
    }


def _fill(slug: str, event_type: str, risk_amount: float) -> dict[str, object]:
    return {
        "timestamp_utc": "2026-04-27T00:01:00+00:00",
        "slug": slug,
        "label": slug,
        "market_id": slug,
        "title": slug,
        "event_type": event_type,
        "platform": "streaming",
        "side": "BUY_NO",
        "fill_price": 0.46,
        "share_quantity": round(risk_amount / 0.46, 4),
        "risk_amount": risk_amount,
        "max_entry_price": 0.48,
        "net_edge": 0.02,
        "reason": "ask_at_or_below_max_entry",
        "snapshot_timestamp_utc": "2026-04-27T00:00:00+00:00",
    }


if __name__ == "__main__":
    unittest.main()
