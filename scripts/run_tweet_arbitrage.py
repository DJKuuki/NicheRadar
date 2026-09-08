#!/usr/bin/env python3
"""Run Tweet Markets Quantitative Arbitrage Scanner.

Usage:
    python scripts/run_tweet_arbitrage.py --scan
    python scripts/run_tweet_arbitrage.py --slug elon-musk-of-tweets-september-1-september-8-2026
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Add project root to sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from bot.tweet_market_scanner import TweetMarketScanner
from bot.tweet_poisson_model import TweetProbabilityModel
from bot.xtracker_client import XTrackerClient


def format_table_row(cols: list[str], widths: list[int]) -> str:
    return " | ".join(c.ljust(w) for c, w in zip(cols, widths))


def run_scan(target_slug: str | None = None, json_out: str | None = None):
    print("=" * 80)
    print(">> NicheRadar -- Tweet Markets Statistical Arbitrage Engine")
    print("=" * 80)

    xtracker = XTrackerClient()
    model = TweetProbabilityModel(min_edge_threshold=0.03, kelly_scale=0.25)
    scanner = TweetMarketScanner(xtracker_client=xtracker, prob_model=model)

    print("\n[1/3] Fetching @elonmusk baseline parameters from XTracker...")
    user_info = xtracker.get_user_info("elonmusk")
    user_id = user_info.get("id")
    mean_d, var_d, _ = xtracker.calculate_historical_daily_stats(user_id)
    model.update_historical_parameters(mean_d, var_d)
    print(f"  -> Calibrated Daily Mean: {mean_d:.2f} posts/day")
    print(f"  -> Calibrated Daily Variance: {var_d:.2f} (Overdispersion index: {var_d / mean_d:.2f}x)")

    print("\n[2/3] Scanning active Polymarket Tweet Markets...")
    all_results = scanner.scan_and_evaluate("elonmusk")

    if target_slug:
        all_results = [r for r in all_results if target_slug in r[0].slug]

    print(f"  -> Found {len(all_results)} matching events.")

    export_data = []

    for ev, progress, evals in all_results:
        print("\n" + "-" * 80)
        print(f"[*] Event: {ev.title}")
        print(f"    Slug: {ev.slug}")
        print(f"    End Date: {ev.end_date.strftime('%Y-%m-%d %H:%M UTC')}")

        if progress:
            print(f"    Current Count: {progress.current_count} posts")
            print(f"    Progress: {progress.percent_time_elapsed:.1f}% elapsed ({progress.remaining_hours:.1f}h remaining)")
            print(f"    Live Pace: {progress.pace_per_day:.1f} posts/day")
        else:
            print("    [!] No matching XTracker session matched directly; using default progress.")

        headers = ["Bracket", "Fair Prob", "Market Ask", "Market Bid", "Edge (Maker)", "Signal", "Kelly Stake"]
        widths = [14, 10, 10, 10, 12, 12, 11]
        print("\n" + format_table_row(headers, widths))
        print("-" * (sum(widths) + 3 * (len(widths) - 1)))

        event_payload = {
            "title": ev.title,
            "slug": ev.slug,
            "end_date": ev.end_date.isoformat(),
            "current_count": progress.current_count if progress else 0,
            "remaining_hours": progress.remaining_hours if progress else 0,
            "brackets": [],
        }

        # Filter to show significant brackets (prob > 0.001 or market_ask > 0.01)
        interesting_evals = [e for e in evals if e.fair_prob >= 0.005 or (e.market_ask or 0) >= 0.01]
        if not interesting_evals:
            interesting_evals = evals[:8]

        for e in interesting_evals:
            b_name = f"{e.bracket.low}-{e.bracket.high or '+'}"
            fair_s = f"{e.fair_prob * 100:.1f}%"
            ask_s = f"{e.market_ask * 100:.1f}%" if e.market_ask is not None else "-"
            bid_s = f"{e.market_bid * 100:.1f}%" if e.market_bid is not None else "-"
            edge_s = f"{e.edge_maker * 100:+.1f}%" if e.edge_maker is not None else "-"
            sig = e.recommendation
            kelly_s = f"{e.kelly_fraction * 100:.1f}%" if e.kelly_fraction else "-"

            cols = [b_name, fair_s, ask_s, bid_s, edge_s, sig, kelly_s]
            print(format_table_row(cols, widths))

            event_payload["brackets"].append({
                "bracket": b_name,
                "fair_prob": e.fair_prob,
                "market_ask": e.market_ask,
                "market_bid": e.market_bid,
                "edge_maker": e.edge_maker,
                "recommendation": e.recommendation,
                "kelly_fraction": e.kelly_fraction,
            })

        export_data.append(event_payload)

    if json_out:
        with open(json_out, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)
        print(f"\n[+] Exported full analysis to {json_out}")

    print("\n" + "=" * 80)
    print("[+] Scan complete. Ready for live maker quote dispatch or shadow monitoring.")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="Run Tweet Markets Arbitrage Engine")
    parser.add_argument("--scan", action="store_true", help="Perform live market scan")
    parser.add_argument("--slug", type=str, default=None, help="Filter specific event slug")
    parser.add_argument("--json-out", type=str, default=None, help="Path to write output JSON")
    args = parser.parse_args()

    run_scan(target_slug=args.slug, json_out=args.json_out)


if __name__ == "__main__":
    main()
