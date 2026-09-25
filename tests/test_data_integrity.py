from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
import requests

from bot.tweet_poisson_model import BracketSpec, TweetProbabilityModel
from bot.tweet_market_scanner import ScannedTweetEvent, TweetMarketScanner
from bot.xtracker_client import XTrackerClient, UserTracking
from bot.shadow_engine import ShadowEngine
from bot.shadow_storage import ShadowStorage, ShadowOrder
from bot.settlement_monitor import SettlementMonitor


def test_open_tail_includes_terms_above_600():
    model = TweetProbabilityModel(25, 200)
    result = model.evaluate_bracket(BracketSpec(1000, None, "1000+"), 0, 31 * 24)
    # Independently sum the upper tail, rather than repeating the CDF code.
    tail = sum(model.forecast_pmf_remaining(31 * 24, 3000)[1000:])
    assert result.fair_prob == pytest.approx(tail, abs=0.00005)
    assert result.fair_prob < 0.01


def tracker_fixture(start_offset=-1, total=43):
    now = datetime.now(timezone.utc)
    tracking = UserTracking("track", "user", "event", now + timedelta(days=start_offset),
                            now + timedelta(days=start_offset + 2), True,
                            "https://polymarket.com/event/test-event")
    client = XTrackerClient()
    client.get_user_info = Mock(return_value={"id": "user"})
    client.list_trackings = Mock(return_value=[tracking])
    detail = {"id": "track", "userId": "user", "user": {"lastSync": now.isoformat()},
              "stats": {"total": total}}
    client._get_json_with_retry = Mock(return_value={"data": detail})
    return client, tracking, now, detail


def test_future_window_is_two_days_not_five():
    client, _, now, _ = tracker_fixture(3, 0)
    progress = client.get_tracking_progress("track", now=now)
    assert progress.current_count == 0
    assert progress.remaining_hours == 48


@pytest.mark.parametrize("count", [None, -1, True, float("nan"), 1.5])
def test_invalid_count_is_not_zero(count):
    client, _, now, _ = tracker_fixture(total=count)
    with pytest.raises(ValueError):
        client.get_tracking_progress("track", now=now)


def test_real_zero_and_stale_count():
    client, _, now, detail = tracker_fixture(total=0)
    assert client.get_tracking_progress("track", now=now).current_count == 0
    detail["user"]["lastSync"] = (now - timedelta(hours=1)).isoformat()
    with pytest.raises(ValueError, match="stale"):
        client.get_tracking_progress("track", now=now)


def test_same_end_date_cannot_select_wrong_tracking():
    client, tracking, now, _ = tracker_fixture()
    event = ScannedTweetEvent("event", "event", "different-event", now,
                              tracking.end_date, True, [], [])
    assert TweetMarketScanner(client).match_with_xtracker(event) is None
    client._get_json_with_retry.assert_not_called()
    event.slug = "test-event"
    assert TweetMarketScanner(client).match_with_xtracker(event).current_count == 43


def test_history_ignores_today_duplicates_and_missing_defaults():
    client = XTrackerClient()
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = [{"type": "daily", "date": (today - timedelta(days=i)).isoformat(),
             "data": {"count": 30}} for i in range(1, 22)]
    rows += [rows[0], {"type": "daily", "date": today.isoformat(), "data": {"count": 9000}}]
    client.get_user_metrics = Mock(return_value=rows)
    mean, _, counts = client.calculate_historical_daily_stats("u")
    assert mean == 30 and len(counts) == 21
    client.get_user_metrics.return_value = []
    with pytest.raises(ValueError):
        client.calculate_historical_daily_stats("missing")


def test_quote_uses_book_and_yes_outcome_mapping():
    scanner = TweetMarketScanner()
    response = Mock()
    response.json.return_value = {"asks": [{"price": "0.4", "size": "10"}],
                                  "bids": [{"price": "0.3", "size": "20"}], "tick_size": "0.01"}
    scanner.session.get = Mock(return_value=response)
    market = {"outcomes": '["No","Yes"]', "clobTokenIds": '["no","yes"]',
              "outcomePrices": '["0.01","0.99"]'}
    assert scanner.fetch_market_quote(market) == (0.4, 0.3)
    assert scanner.session.get.call_args.kwargs["params"] == {"token_id": "yes"}
    response.json.return_value = {"asks": [], "bids": []}
    assert scanner.fetch_market_quote(market) is None


def make_order(storage, mode="MAKER_STRICT"):
    order = ShadowOrder("o", "e", "slug", "m", "condition", "token", "bracket", "BUY_YES",
                        0.25, 50, 12.5, (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
                        fill_mode=mode)
    storage.create_order(order)
    return order


@pytest.mark.parametrize("change", [
    {"timestamp": 1}, {"side": "BUY"}, {"asset": "other"}, {"asset": ""},
    {"size": 1}, {"price": 0.25}, {"timestamp": None}, {"price": float("nan")},
])
def test_invalid_or_old_tape_cannot_fill(tmp_path, change):
    storage = ShadowStorage(tmp_path / "test.sqlite")
    make_order(storage)
    trade = {"timestamp": int(datetime.now(timezone.utc).timestamp()), "price": 0.24,
             "size": 50, "asset": "token", "side": "SELL"}
    trade.update(change)
    engine = ShadowEngine(storage, book_fetcher=lambda _: {}, trades_fetcher=lambda _: [trade])
    assert engine.check_and_update_fills() == []
    assert len(storage.get_open_orders()) == 1


def test_shallow_book_and_duplicate_tape_cannot_fill(tmp_path):
    storage = ShadowStorage(tmp_path / "test.sqlite")
    make_order(storage)
    trade = {"timestamp": int(datetime.now(timezone.utc).timestamp()), "price": 0.24,
             "size": 30, "asset": "token", "side": "SELL", "transactionHash": "tx"}
    engine = ShadowEngine(storage, book_fetcher=lambda _: {"asks": [{"price": ".24", "size": "1"}]},
                          trades_fetcher=lambda _: [trade, trade])
    assert engine.check_and_update_fills() == []
    assert engine.check_and_update_fills() == []


def test_taker_depth_vwap_and_cash_reconcile(tmp_path):
    storage = ShadowStorage(tmp_path / "test.sqlite")
    make_order(storage, "TAKER")
    engine = ShadowEngine(storage, book_fetcher=lambda _: {"asks": [
        {"price": ".20", "size": "20"}, {"price": ".25", "size": "30"}]})
    assert engine.check_and_update_fills()[0].fill_price == pytest.approx(.23)
    assert storage.get_account_summary().locked_collateral == 11.5
    assert storage.get_account_summary().cash_balance == 988.5
    assert storage.settle_order("o", 1) == 38.5
    assert storage.get_account_summary().total_equity == 1038.5


def test_closed_or_near_terminal_is_not_resolution(tmp_path):
    monitor = SettlementMonitor(ShadowStorage(tmp_path / "test.sqlite"))
    assert monitor._check_market_resolution({"closed": True, "outcomePrices": [1, 0]}) is None
    assert monitor._check_market_resolution({"resolved": True, "outcomePrices": [.95, .05]}) is None
    assert monitor._check_market_resolution({"umaResolutionStatus": "resolved", "outcomePrices": [1, 0]})["yes_won"]


def test_scan_uses_tracking_window_and_skips_missing_progress():
    client, tracking, now, _ = tracker_fixture(3, 0)
    client.calculate_historical_daily_stats = Mock(return_value=(25, 200, [25] * 30))
    scanner = TweetMarketScanner(client)
    market = {"question": "100-119 tweets", "slug": "bracket", "_tick_size": .01,
              "_quote_at_utc": now.isoformat()}
    event = ScannedTweetEvent("e", "title", "test-event", now - timedelta(days=1),
        tracking.end_date, True, [market], [BracketSpec(100, 119, market["question"])])
    scanner.fetch_active_tweet_events = Mock(return_value=[event])
    scanner.fetch_market_quote = Mock(return_value=(.2, .18))
    results = scanner.scan_and_evaluate(["elonmusk"])
    assert len(results) == 1
    assert market["_model_context"]["remaining_hours"] == 48
    scanner.match_with_xtracker = Mock(return_value=None)
    assert scanner.scan_and_evaluate(["elonmusk"]) == []
    client.calculate_historical_daily_stats.side_effect = ValueError("missing")
    assert scanner.scan_and_evaluate(["elonmusk"]) == []


def test_bid_depth_valuation_does_not_invent_zero_pnl(tmp_path):
    storage = ShadowStorage(tmp_path / "test.sqlite")
    make_order(storage)
    storage.mark_order_filled("o", .25)
    engine = ShadowEngine(storage, book_fetcher=lambda _: {"bids": [{"price": ".1", "size": "50"}]})
    assert engine.liquidation_values() == {"o": 5}
    engine._custom_book_fetcher = lambda _: {"bids": [{"price": ".1", "size": "1"}]}
    assert engine.liquidation_values() == {}


def test_expired_order_cannot_fill_between_scans(tmp_path):
    storage = ShadowStorage(tmp_path / "test.sqlite")
    order = make_order(storage)
    import sqlite3
    with sqlite3.connect(storage.db_path) as conn:
        conn.execute("UPDATE shadow_orders SET placed_at_utc = ?", ((datetime.now(timezone.utc) - timedelta(hours=13)).isoformat(),))
    engine = ShadowEngine(storage, book_fetcher=lambda _: {"asks": [{"price": ".1", "size": "1000"}]})
    assert engine.check_and_update_fills() == []
    assert storage.get_account_summary().cash_balance == 1000


def test_version_reports_are_separate(tmp_path):
    import json
    from bot.performance_analytics import PerformanceAnalytics
    storage = ShadowStorage(tmp_path / "test.sqlite")
    make_order(storage)
    storage.mark_order_filled("o", .25)
    storage.settle_order("o", 0)
    new = ShadowOrder("new", "e2", "slug", "m2", "c2", "t2", "b", "BUY_YES", .25, 50, 12.5,
                      datetime.now(timezone.utc).isoformat(), metadata_json=json.dumps({"strategy_version": "v3-data-integrity"}))
    storage.create_order(new)
    storage.mark_order_filled("new", .25)
    analytics = PerformanceAnalytics(storage)
    assert analytics.evaluate("legacy").total_realized_pnl == -12.5
    assert analytics.evaluate("v3-data-integrity").settled_trades_count == 0
    assert analytics.evaluate("v3-data-integrity").filled_positions_count == 1


def test_xtracker_stale_cache_fallback():
    client = XTrackerClient()
    client._user_info_cache["testuser"] = (0.0, {"id": "u123", "trackings": []})
    client._user_metrics_cache["u123"] = (0.0, [{"type": "daily", "date": "2026-09-01T00:00:00Z", "data": {"count": 10}}])
    client._stats_cache["u123_90"] = (0.0, (10.0, 5.0, [10]))

    client._get_json_with_retry = Mock(side_effect=requests.RequestException("Connection reset"))

    assert client.get_user_info("testuser", ttl_sec=0.1) == {"id": "u123", "trackings": []}
    assert client.get_user_metrics("u123", ttl_sec=0.1) == [{"type": "daily", "date": "2026-09-01T00:00:00Z", "data": {"count": 10}}]
    assert client.calculate_historical_daily_stats("u123", lookback_days=90, ttl_sec=0.1) == (10.0, 5.0, [10])

