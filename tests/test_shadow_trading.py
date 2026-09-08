"""Unit tests for Shadow Paper Trading modules.

Tests persistence, execution simulation, settlement synchronization,
and statistical performance analytics using isolated temporary databases.
"""

import json
from datetime import datetime, timezone
import pytest

from bot.performance_analytics import PerformanceAnalytics
from bot.settlement_monitor import SettlementMonitor
from bot.shadow_engine import EngineConfig, ShadowEngine, TradeCandidate, candidates_from_scan_results
from bot.shadow_storage import ShadowOrder, ShadowStorage


@pytest.fixture
def temp_storage(tmp_path):
    db_file = tmp_path / "test_shadow.sqlite"
    return ShadowStorage(db_path=db_file)


def test_account_initialization_and_order_flow(temp_storage):
    account = temp_storage.get_account_summary()
    assert account.initial_bankroll == 1000.0
    assert account.cash_balance == 1000.0
    assert account.locked_collateral == 0.0

    order = ShadowOrder(
        order_id="test_ord_1",
        event_id="ev_1",
        event_slug="elon-tweets-march",
        market_id="mkt_1",
        condition_id="0xcond1",
        token_id="tok_1",
        bracket_name="140-159",
        side="BUY_YES",
        limit_price=0.20,
        size_shares=100.0,
        cost_usdc=20.0,
        placed_at_utc=datetime.now(timezone.utc).isoformat(),
        status="OPEN",
        fill_mode="MAKER_STRICT",
    )

    # 1. Place order
    assert temp_storage.create_order(order) is True
    acc2 = temp_storage.get_account_summary()
    assert acc2.cash_balance == 980.0
    assert acc2.locked_collateral == 20.0
    assert acc2.open_orders_count == 1

    # 2. Prevent duplicate active order
    assert temp_storage.has_active_order_for_bracket("mkt_1", "BUY_YES") is True
    assert temp_storage.create_order(order) is False

    # 3. Mark filled
    assert temp_storage.mark_order_filled("test_ord_1", fill_price=0.20) is True
    filled = temp_storage.get_filled_orders()
    assert len(filled) == 1
    assert filled[0].status == "FILLED"

    # 4. Settle as WINNER (terminal price = 1.0)
    pnl = temp_storage.settle_order("test_ord_1", terminal_price=1.0)
    assert pnl == pytest.approx(80.0)  # (1.0 - 0.20) * 100 = $80 profit

    acc3 = temp_storage.get_account_summary()
    assert acc3.cash_balance == pytest.approx(1080.0)  # 980 + 100 payout
    assert acc3.locked_collateral == pytest.approx(0.0)
    assert acc3.realized_pnl == pytest.approx(80.0)
    assert acc3.settled_orders_count == 1


def test_order_settlement_loser_and_cancellation(temp_storage):
    order = ShadowOrder(
        order_id="test_ord_2",
        event_id="ev_2",
        event_slug="elon-tweets-march",
        market_id="mkt_2",
        condition_id="0xcond2",
        token_id="tok_2",
        bracket_name="160-179",
        side="BUY_YES",
        limit_price=0.30,
        size_shares=50.0,
        cost_usdc=15.0,
        placed_at_utc=datetime.now(timezone.utc).isoformat(),
        status="OPEN",
        fill_mode="MAKER_STRICT",
    )
    temp_storage.create_order(order)
    temp_storage.mark_order_filled("test_ord_2", fill_price=0.30)

    # Settle as LOSER (terminal price = 0.0)
    pnl = temp_storage.settle_order("test_ord_2", terminal_price=0.0)
    assert pnl == pytest.approx(-15.0)

    acc = temp_storage.get_account_summary()
    assert acc.cash_balance == pytest.approx(985.0)
    assert acc.locked_collateral == pytest.approx(0.0)
    assert acc.realized_pnl == pytest.approx(-15.0)


def test_unfilled_order_cancellation_refund(temp_storage):
    order = ShadowOrder(
        order_id="test_ord_3",
        event_id="ev_3",
        event_slug="elon-tweets-march",
        market_id="mkt_3",
        condition_id="0xcond3",
        token_id="tok_3",
        bracket_name="180+",
        side="BUY_YES",
        limit_price=0.10,
        size_shares=100.0,
        cost_usdc=10.0,
        placed_at_utc=datetime.now(timezone.utc).isoformat(),
        status="OPEN",
        fill_mode="MAKER_STRICT",
    )
    temp_storage.create_order(order)
    acc1 = temp_storage.get_account_summary()
    assert acc1.cash_balance == 990.0

    # User or market resolution cancels unfilled order
    assert temp_storage.cancel_order("test_ord_3") is True
    acc2 = temp_storage.get_account_summary()
    assert acc2.cash_balance == 1000.0
    assert acc2.locked_collateral == 0.0


def test_shadow_engine_order_placement_and_fill_simulation(temp_storage):
    cands = [
        TradeCandidate(
            event_id="ev_1",
            event_slug="elon-tweets-w10",
            bracket_name="120-139",
            market_id="mkt_101",
            condition_id="0xcond101",
            clob_token_id="tok_101",
            side="BUY_YES",
            model_prob=0.35,
            market_prob=0.20,
            net_edge=0.15,
            kelly_fraction=0.10,
            target_price=0.20,
        )
    ]

    # Mock orderbook: best ask is 0.19 (seller matches our 0.20 bid)
    mock_book = {
        "bids": [{"price": "0.18", "size": "50"}],
        "asks": [{"price": "0.19", "size": "100"}],
    }

    engine = ShadowEngine(
        storage=temp_storage,
        config=EngineConfig(min_edge=0.05, default_fill_mode="MAKER_STRICT"),
        book_fetcher=lambda token_id: mock_book,
        trades_fetcher=lambda cond_id: [],
    )

    placed = engine.evaluate_and_place_orders(cands)
    assert len(placed) == 1
    assert placed[0].limit_price == 0.20
    assert placed[0].side == "BUY_YES"

    # Check fill
    fills = engine.check_and_update_fills()
    assert len(fills) == 1
    assert fills[0].status == "FILLED"
    assert fills[0].fill_price == 0.20


def test_shadow_engine_trades_tape_fill_simulation(temp_storage):
    order = ShadowOrder(
        order_id="test_ord_strict",
        event_id="ev_strict",
        event_slug="elon-tweets-strict",
        market_id="mkt_strict",
        condition_id="0xcond_strict",
        token_id="tok_strict",
        bracket_name="140-159",
        side="BUY_YES",
        limit_price=0.25,
        size_shares=50.0,
        cost_usdc=12.5,
        placed_at_utc=datetime.now(timezone.utc).isoformat(),
        status="OPEN",
        fill_mode="MAKER_STRICT",
    )
    temp_storage.create_order(order)

    # Orderbook ask is high (0.30), but Data API shows someone traded at 0.24 (trade-through!)
    mock_book = {"asks": [{"price": "0.30", "size": "50"}]}
    mock_trades = [
        {"price": 0.24, "size": 100.0, "asset": "tok_strict", "timestamp": 1788880000}
    ]

    engine = ShadowEngine(
        storage=temp_storage,
        book_fetcher=lambda token_id: mock_book,
        trades_fetcher=lambda cond_id: mock_trades,
    )

    fills = engine.check_and_update_fills()
    assert len(fills) == 1
    assert fills[0].order_id == "test_ord_strict"
    assert fills[0].status == "FILLED"


def test_settlement_monitor_and_resolution(temp_storage):
    order = ShadowOrder(
        order_id="test_ord_settle",
        event_id="ev_settle",
        event_slug="elon-tweets-settle",
        market_id="mkt_settle",
        condition_id="0xcond_settle",
        token_id="tok_settle",
        bracket_name="140-159",
        side="BUY_YES",
        limit_price=0.20,
        size_shares=50.0,
        cost_usdc=10.0,
        placed_at_utc=datetime.now(timezone.utc).isoformat(),
        status="OPEN",
        fill_mode="MAKER_STRICT",
    )
    temp_storage.create_order(order)
    temp_storage.mark_order_filled("test_ord_settle", fill_price=0.20)

    # Mock Gamma market resolution where YES won: outcomePrices = ["1", "0"]
    mock_market = {
        "closed": True,
        "resolved": True,
        "outcomePrices": ["1.0", "0.0"],
    }

    monitor = SettlementMonitor(
        storage=temp_storage,
        gamma_fetcher=lambda m_id: mock_market,
    )

    results = monitor.check_and_sync_settlements()
    assert len(results) == 1
    assert results[0].realized_pnl == pytest.approx(40.0)  # (1.0 - 0.20) * 50 = $40 profit

    acc = temp_storage.get_account_summary()
    assert acc.realized_pnl == pytest.approx(40.0)


def test_performance_analytics_and_brier_calibration(temp_storage):
    # Setup 3 settled orders: 2 wins, 1 loss
    # Order 1: Model 0.40, Market 0.20, Won (y=1) -> Model Error = (0.4-1)^2 = 0.36; Market Error = (0.2-1)^2 = 0.64
    o1 = ShadowOrder(
        order_id="o1",
        event_id="e1",
        event_slug="s1",
        market_id="m1",
        condition_id="c1",
        token_id="t1",
        bracket_name="b1",
        side="BUY_YES",
        limit_price=0.20,
        size_shares=50.0,
        cost_usdc=10.0,
        placed_at_utc="2026-09-01T00:00:00Z",
        status="OPEN",
        metadata_json=json.dumps({"model_prob": 0.40, "market_prob": 0.20}),
    )
    temp_storage.create_order(o1)
    temp_storage.mark_order_filled("o1", fill_price=0.20)
    temp_storage.settle_order("o1", terminal_price=1.0)

    # Order 2: Model 0.10, Market 0.30, Lost (y=0) -> Model Error = (0.1-0)^2 = 0.01; Market Error = (0.3-0)^2 = 0.09
    o2 = ShadowOrder(
        order_id="o2",
        event_id="e2",
        event_slug="s2",
        market_id="m2",
        condition_id="c2",
        token_id="t2",
        bracket_name="b2",
        side="BUY_YES",
        limit_price=0.30,
        size_shares=50.0,
        cost_usdc=15.0,
        placed_at_utc="2026-09-02T00:00:00Z",
        status="OPEN",
        metadata_json=json.dumps({"model_prob": 0.10, "market_prob": 0.30}),
    )
    temp_storage.create_order(o2)
    temp_storage.mark_order_filled("o2", fill_price=0.30)
    temp_storage.settle_order("o2", terminal_price=0.0)

    analytics = PerformanceAnalytics(temp_storage)
    metrics = analytics.evaluate()

    assert metrics.settled_trades_count == 2
    assert metrics.winning_trades == 1
    assert metrics.losing_trades == 1
    assert metrics.win_rate_pct == 50.0
    assert metrics.total_realized_pnl == pytest.approx(40.0 - 15.0)  # +$25

    # Check Brier Score
    # Model Brier = (0.36 + 0.01) / 2 = 0.185
    # Market Brier = (0.64 + 0.09) / 2 = 0.365
    assert metrics.model_brier_score == pytest.approx(0.185, abs=0.001)
    assert metrics.market_brier_score == pytest.approx(0.365, abs=0.001)
    # BSS = 1 - (0.185 / 0.365) = +0.4932 > 0 (Model beats market!)
    assert metrics.brier_skill_score is not None
    assert metrics.brier_skill_score > 0


def test_candidates_from_scan_results():
    from bot.tweet_poisson_model import BracketEvaluation, BracketSpec
    from bot.tweet_market_scanner import ScannedTweetEvent

    spec = BracketSpec(low=100, high=119, name="100-119 tweets", market_slug="mkt-100-119")
    b_eval = BracketEvaluation(
        bracket=spec,
        fair_prob=0.30,
        market_ask=0.15,
        market_bid=0.12,
        edge_buy=0.15,
        edge_maker=0.18,
        recommendation="MAKER_BUY_YES",
        target_maker_price=0.13,
        kelly_fraction=0.08,
    )

    ev = ScannedTweetEvent(
        event_id="ev_test",
        title="Elon tweets this week",
        slug="elon-tweets-test",
        start_date=datetime.now(timezone.utc),
        end_date=datetime.now(timezone.utc),
        active=True,
        markets=[
            {
                "id": "mkt_1",
                "slug": "mkt-100-119",
                "conditionId": "0xcond_abc",
                "clobTokenIds": json.dumps(["tok_yes_1", "tok_no_1"]),
                "question": "Will Elon tweet 100-119...",
            }
        ],
        brackets=[spec],
    )

    candidates = candidates_from_scan_results([(ev, None, [b_eval])], min_edge=0.05)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.market_id == "mkt_1"
    assert c.condition_id == "0xcond_abc"
    assert c.clob_token_id == "tok_yes_1"
    assert c.model_prob == 0.30
    assert c.target_price == 0.13
    assert c.net_edge == 0.18

