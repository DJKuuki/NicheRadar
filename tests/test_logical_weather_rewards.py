"""Unit tests for Logical Dependency Arbitrage, Weather Temperature Markets,
Liquidity Rewards Analyzer, and XTracker hourly bucket freshness fallback.
"""

from datetime import date, datetime, timedelta, timezone
import json
from unittest.mock import Mock

import pytest

from bot.logical_arb_scanner import LogicalArbScanner
from bot.rewards_analyzer import RewardsAnalyzer
from bot.shadow_engine import EngineConfig, ShadowEngine, TradeCandidate
from bot.shadow_storage import ShadowStorage
from bot.weather_market_scanner import (
    ScannedWeatherEvent,
    TempBracketSpec,
    WeatherForecastSummary,
    WeatherMarketScanner,
    WeatherProbabilityModel,
    WEATHER_STATIONS,
    candidates_from_weather_results,
)
from bot.xtracker_client import UserTracking, XTrackerClient


def test_xtracker_hourly_bucket_freshness_fallback():
    now = datetime.now(timezone.utc)
    tracking = UserTracking(
        "track_1",
        "user_1",
        "Elon tweets",
        now - timedelta(days=2),
        now + timedelta(days=2),
        True,
        "https://polymarket.com/event/elon-test",
    )
    client = XTrackerClient()
    client.get_user_info = Mock(return_value={"id": "user_1"})
    client.list_trackings = Mock(return_value=[tracking])

    # Legacy user.lastSync is 2 days old, but stats.daily has current-hour bucket and recent activity
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    detail = {
        "id": "track_1",
        "userId": "user_1",
        "user": {
            "lastSync": (now - timedelta(days=2)).isoformat(),
            "updatedAt": (now - timedelta(minutes=2)).isoformat(),
        },
        "stats": {
            "total": 142,
            "daily": [
                {"date": (current_hour - timedelta(hours=2)).isoformat(), "count": 5, "cumulative": 137},
                {"date": (current_hour - timedelta(hours=1)).isoformat(), "count": 5, "cumulative": 142},
                {"date": current_hour.isoformat(), "count": 0, "cumulative": 142},
            ],
        },
    }
    client._get_json_with_retry = Mock(return_value={"data": detail})
    progress = client.get_tracking_progress("track_1", now=now)
    assert progress.current_count == 142

    # If daily buckets have zero activity across all 24h, it must still reject as stale
    for b in detail["stats"]["daily"]:
        b["count"] = 0
    with pytest.raises(ValueError, match="stale"):
        client.get_tracking_progress("track_1", now=now)


def test_parse_deadline_and_strike_questions():
    scanner = LogicalArbScanner()

    # 1. Deadline parsing
    parsed_d1 = scanner.parse_deadline_question("Will X launch a token by October 31, 2026?")
    parsed_d2 = scanner.parse_deadline_question("Will X launch a token by December 31, 2026?")
    assert parsed_d1 is not None and parsed_d2 is not None
    assert parsed_d1[0] == parsed_d2[0] == "will x launch a token"
    assert parsed_d1[1] < parsed_d2[1]

    # 2. Strike parsing: ensure 'by' after '$3.00' is NOT treated as 'b' (billion)
    s1 = scanner.parse_strike_question("Will XRP reach $2.60 by December 31, 2026?")
    s2 = scanner.parse_strike_question("Will XRP reach $3.00 by December 31, 2026?")
    assert s1 is not None and s2 is not None
    assert s1[0] == s2[0]
    assert s1[1] == "UP" and s2[1] == "UP"
    assert s1[2] == pytest.approx(2.60)
    assert s2[2] == pytest.approx(3.00)

    # 3. Strike parsing with explicit B/M suffixes
    s_m = scanner.parse_strike_question("Opensea FDV above $500M one day after launch?")
    s_b = scanner.parse_strike_question("Opensea FDV above $2B one day after launch?")
    assert s_m is not None and s_b is not None
    assert s_m[0] == s_b[0]
    assert s_m[2] == pytest.approx(5e8)
    assert s_b[2] == pytest.approx(2e9)

    # 4. Reject Over/Under sports totals and range questions
    assert scanner.parse_strike_question("Total Kills Over/Under 27.5 in Game 1?") is None
    assert scanner.parse_strike_question("Will BTC be between $90,000 and $95,000?") is None


def test_date_and_strike_monotonicity_arbitrage():
    scanner = LogicalArbScanner()
    event = {
        "id": "ev_xrp",
        "slug": "xrp-ladder-test",
        "title": "XRP Price Ladder",
        "negRisk": False,
        "markets": [
            {
                "id": "m_260",
                "question": "Will XRP reach $2.60 by December 31, 2026?",
                "active": True,
                "closed": False,
                "bestBid": "0.030",
                "bestAsk": "0.034",
                "clobTokenIds": '["yes_260", "no_260"]',
                "outcomes": '["Yes", "No"]',
            },
            {
                "id": "m_300",
                "question": "Will XRP reach $3.00 by December 31, 2026?",
                "active": True,
                "closed": False,
                "bestBid": "0.045",
                "bestAsk": "0.048",
                "clobTokenIds": '["yes_300", "no_300"]',
                "outcomes": '["Yes", "No"]',
            },
            {
                "id": "m_oct",
                "question": "Will Project X launch by October 31, 2026?",
                "active": True,
                "closed": False,
                "bestBid": "0.40",
                "bestAsk": "0.42",
                "clobTokenIds": '["yes_oct", "no_oct"]',
                "outcomes": '["Yes", "No"]',
            },
            {
                "id": "m_dec",
                "question": "Will Project X launch by December 31, 2026?",
                "active": True,
                "closed": False,
                "bestBid": "0.35",
                "bestAsk": "0.37",
                "clobTokenIds": '["yes_dec", "no_dec"]',
                "outcomes": '["Yes", "No"]',
            },
        ],
    }

    opps, invs = scanner.scan_event_for_monotonicity(event, min_edge=0.005, verify_clob=False)
    assert len(opps) == 2
    assert len(invs) == 2

    by_type = {o.arb_type: o for o in opps}
    assert "STRIKE_MONOTONICITY" in by_type
    assert "DATE_MONOTONICITY" in by_type

    # For XRP: NO($3.00) costs 1 - 0.045 = 0.955, YES($2.60) costs 0.034 -> total = 0.989 < 1.00
    strike_opp = by_type["STRIKE_MONOTONICITY"]
    assert strike_opp.total_cost_per_unit == pytest.approx(0.989)
    assert strike_opp.guaranteed_payout == 1.0
    assert strike_opp.max_payout == 2.0

    # For Project X: NO(Oct) costs 1 - 0.40 = 0.60, YES(Dec) costs 0.37 -> total = 0.97 < 1.00
    date_opp = by_type["DATE_MONOTONICITY"]
    assert date_opp.total_cost_per_unit == pytest.approx(0.97)


def test_mutex_no_and_exhaustive_yes_bundles():
    scanner = LogicalArbScanner()

    # Non-exhaustive candidate list ("Who will Trump endorse?"): sum(YES ask) = 0.30 < 1.00
    # Must NOT trigger EXHAUSTIVE_YES_BUNDLE because "Nobody" is unlisted!
    non_exhaustive_event = {
        "id": "ev_non_ex",
        "slug": "endorsement-test",
        "title": "Endorsement",
        "negRisk": True,
        "markets": [
            {"id": "1", "question": "Will candidate A be endorsed?", "active": True, "closed": False,
             "bestBid": "0.08", "bestAsk": "0.10", "clobTokenIds": '["y1","n1"]', "outcomes": '["Yes","No"]'},
            {"id": "2", "question": "Will candidate B be endorsed?", "active": True, "closed": False,
             "bestBid": "0.08", "bestAsk": "0.10", "clobTokenIds": '["y2","n2"]', "outcomes": '["Yes","No"]'},
            {"id": "3", "question": "Will candidate C be endorsed?", "active": True, "closed": False,
             "bestBid": "0.08", "bestAsk": "0.10", "clobTokenIds": '["y3","n3"]', "outcomes": '["Yes","No"]'},
        ],
    }
    assert scanner.scan_event_for_mutex_bundles(non_exhaustive_event, verify_clob=False) == []

    # Complete numerical partition with 0-19, 20-39, 40+ where sum(YES ask) = 0.97 < 1.00
    exhaustive_event = {
        "id": "ev_ex",
        "slug": "complete-range-test",
        "title": "Complete Range",
        "negRisk": True,
        "markets": [
            {"id": "1", "question": "Will count be 0-19?", "active": True, "closed": False,
             "bestBid": "0.30", "bestAsk": "0.32", "clobTokenIds": '["y1","n1"]', "outcomes": '["Yes","No"]'},
            {"id": "2", "question": "Will count be 20-39?", "active": True, "closed": False,
             "bestBid": "0.30", "bestAsk": "0.32", "clobTokenIds": '["y2","n2"]', "outcomes": '["Yes","No"]'},
            {"id": "3", "question": "Will count be 40+?", "active": True, "closed": False,
             "bestBid": "0.30", "bestAsk": "0.33", "clobTokenIds": '["y3","n3"]', "outcomes": '["Yes","No"]'},
        ],
    }
    ex_opps = scanner.scan_event_for_mutex_bundles(exhaustive_event, verify_clob=False)
    assert len(ex_opps) == 1
    assert ex_opps[0].arb_type == "EXHAUSTIVE_YES_BUNDLE"
    assert ex_opps[0].total_cost_per_unit == pytest.approx(0.97)


def test_weather_scanner_and_probability_model():
    scanner = WeatherMarketScanner()
    model = WeatherProbabilityModel(min_edge_threshold=0.05)

    p_nyc = scanner.parse_weather_event_title("Highest temperature in NYC on October 26?")
    assert p_nyc is not None
    assert p_nyc[0].station_code == "KLGA"
    assert p_nyc[1] == date(2026, 10, 26)
    assert p_nyc[2] == "max"

    p_paris = scanner.parse_weather_event_title("Lowest temperature in Paris on October 26?")
    assert p_paris is not None
    assert p_paris[0].station_code == "LFPG"
    assert p_paris[2] == "min"

    # Contiguous integer partition: <= 59°F, 60-61°F, 62-63°F, 64°F or higher
    b1 = scanner.parse_temperature_bracket("Will the high be 59°F or below?")
    b2 = scanner.parse_temperature_bracket("Will the high be between 60-61°F?")
    b3 = scanner.parse_temperature_bracket("62-63°F")
    b4 = scanner.parse_temperature_bracket("64°F or higher")
    assert all(b is not None for b in (b1, b2, b3, b4))

    probs = [model.bracket_probability(b, mean_temp=61.2, std_temp=2.0) for b in (b1, b2, b3, b4)]
    assert sum(probs) == pytest.approx(1.0, abs=1e-6)

    # Candidate generation test
    now_iso = datetime.now(timezone.utc).isoformat()
    end_dt = datetime.now(timezone.utc) + timedelta(hours=12)
    mkt = {
        "id": "m_w1",
        "slug": "nyc-60-61",
        "question": b2.name,
        "conditionId": "0xweather",
        "_yes_token_id": "tok_w1",
        "_model_context": {
            "quote_at": now_iso,
            "window_end": end_dt.isoformat(),
            "count_observed_at": now_iso,
            "quote_bid": 0.22,
            "quote_ask": 0.25,
            "rewardsMinSize": 20,
            "rewardsMaxSpread": 4.5,
        },
    }
    ev = ScannedWeatherEvent(
        event_id="ev_w1",
        title="Highest temperature in NYC on October 26?",
        slug="highest-temperature-in-nyc-on-october-26",
        station=WEATHER_STATIONS["nyc"],
        target_date=date(2026, 10, 26),
        end_date=end_dt,
        markets=[mkt],
        brackets=[b2],
    )
    forecast = WeatherForecastSummary(WEATHER_STATIONS["nyc"], date(2026, 10, 26), "max", 61.0, 2.0, [61.0])
    ev_res = model.evaluate_bracket(b2, 61.0, 2.0, market_ask=0.25, market_bid=0.22, tick_size=0.01)
    cands = candidates_from_weather_results([(ev, forecast, [ev_res])], min_edge=0.05)
    assert len(cands) == 1
    assert cands[0].target_price == pytest.approx(0.23)
    assert cands[0].net_edge > 0.10


def test_rewards_analyzer_and_shadow_engine_integration( tmp_path ):
    storage = ShadowStorage(tmp_path / "rewards_test.sqlite")
    engine = ShadowEngine(storage=storage, config=EngineConfig(min_edge=0.05))

    now = datetime.now(timezone.utc)
    # Candidate where Quarter-Kelly gives ~45 shares at $0.25 ($11.25), while rewardsMinSize is 50 shares ($12.50)
    cand = TradeCandidate(
        event_id="ev_rw",
        event_slug="highest-temperature-in-nyc-test",
        market_id="mkt_rw",
        condition_id="0xcond_rw",
        clob_token_id="tok_rw",
        bracket_name="60-61°F",
        side="BUY_YES",
        model_prob=0.40,
        market_prob=0.25,
        net_edge=0.15,
        kelly_fraction=0.01125,  # $1000 * 0.01125 = $11.25 -> 45 shares before reward bump
        target_price=0.25,
        metadata={
            "quote_at": now.isoformat(),
            "window_end": (now + timedelta(hours=12)).isoformat(),
            "count_observed_at": now.isoformat(),
            "quote_bid": 0.24,
            "quote_ask": 0.26,
            "rewardsMinSize": 50,
            "rewardsMaxSpread": 4.5,
            "holdingRewardsEnabled": True,
            "feesEnabled": True,
            "feeType": "finance_prices_fees",
            "feeSchedule": {"exponent": 1, "rate": 0.04, "takerOnly": True, "rebateRate": 0.25},
        },
    )
    placed = engine.evaluate_and_place_orders([cand])
    assert len(placed) == 1
    order = placed[0]
    # Bumped from 45 shares to 50 shares to qualify for Polymarket Liquidity Rewards!
    assert order.size_shares == 50.0
    meta = json.loads(order.metadata_json)
    assert meta["liquidity_reward_eligible"] is True
    assert meta["size_bumped_for_reward"] is True
    assert meta["reward_q_score"] == pytest.approx(1.0)
    assert meta["maker_rebate_usdc"] > 0
